from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import test_run_agent

GATE = test_run_agent.RUNNER.bundled_module("gate")
SCRIPT = Path(GATE.__file__)

HEAVY = [
    "yarn test",
    "./node_modules/.bin/vitest run",
    "npx jest",
    "npx jest -t 'renders'",  # a name filter still loads every test file
    "tsc --noEmit",
    "yarn tsc -p tsconfig.json",
    "./node_modules/.bin/eslint .",
    "yarn lint",
    "yarn build",
    "vite build",
    "pytest",
    "python3 -m pytest -k foo",
    "python3 -m unittest discover -s tests",
    "cd pkg && FOO=1 yarn test > log 2>&1; echo done",
    "bash -lc 'vitest run'",
    "time nice -n 5 yarn test",
    "timeout 600 npx vitest run",
    "ork gate --heavy -- yarn test && yarn build",
    'cat <<<"x"\nyarn build',
    "yarn --cwd packages/web test",
    "yarn workspace @app/web test",
    "pnpm --filter @app/web run lint",
    "npm --prefix packages/web test",
    "npm -w @app/web run build",
]
NOT_HEAVY = [
    "npm test -- src/a.test.ts",
    "yarn test src/a.test.ts",
    "yarn --cwd packages/web test src/a.test.ts",
    "yarn workspace @app/web test src/a.test.ts",
    "pnpm --filter @app/web test src/a.test.ts",
    "./node_modules/.bin/vitest run src/x.test.tsx",
    "vitest related src/a.ts",
    "yarn jest src/a",
    "./node_modules/.bin/eslint src/a.ts src/b.tsx",
    "vite",
    "next dev",
    "pytest tests/test_a.py",
    "python3 -m unittest tests.test_a",
    "ork gate --heavy -- yarn test",
    "grep -r vitest .",
    "cat jest.config.js",
    "command -v vitest",
    "git commit -F - <<'EOF'\nyarn build\nEOF",
    "yarn install",
    "vitest --help",
    "yarn test:changed",
    "echo hi # yarn build",
    "node tools/run-related-tests.mjs",
]


class ClassificationTests(unittest.TestCase):
    def test_whole_runs_are_heavy_and_focused_ones_are_not(self):
        for line in HEAVY:
            with self.subTest(line=line):
                self.assertTrue(GATE.heavy_runs(line))
        for line in NOT_HEAVY:
            with self.subTest(line=line):
                self.assertFalse(GATE.heavy_runs(line))

    def test_many_files_make_type_aware_lint_heavy(self):
        files = " ".join(f"src/f{index}.ts" for index in range(GATE.MANY_FILES + 1))
        self.assertTrue(GATE.heavy_runs(f"eslint {files}"))
        self.assertTrue(GATE.heavy_runs(f"vitest related {files}"))
        self.assertTrue(GATE.heavy_runs(f"jest --findRelatedTests {files}"))
        self.assertTrue(GATE.heavy_runs(f"yarn test {files}"))

    def test_jest_gets_a_worker_cap_unless_it_sets_one(self):
        self.assertEqual(GATE.capped(["npx", "jest", "a"], 2), ["npx", "jest", "a", "--maxWorkers=2"])
        self.assertEqual(
            GATE.capped(["yarn", "--cwd", "packages/web", "jest", "a"], 2),
            ["yarn", "--cwd", "packages/web", "jest", "a", "--maxWorkers=2"],
        )
        self.assertEqual(
            GATE.capped(["npm", "-w", "@app/web", "jest", "a"], 2),
            ["npm", "-w", "@app/web", "jest", "a", "--maxWorkers=2"],
        )
        self.assertEqual(GATE.capped(["npx", "jest", "-w", "1"], 2), ["npx", "jest", "-w", "1"])
        self.assertEqual(GATE.capped(["npx", "jest", "--runInBand"], 2), ["npx", "jest", "--runInBand"])
        self.assertEqual(GATE.capped(["vitest", "run"], 2), ["vitest", "run"])
        self.assertEqual(GATE.capped(["yarn", "test", "packages/jest"], 2), ["yarn", "test", "packages/jest"])
        self.assertEqual(GATE.capped(["vitest", "run", "src/jest"], 2), ["vitest", "run", "src/jest"])

    def test_the_gate_runs_repository_check_scripts_but_nothing_arbitrary(self):
        for command in (
            ["bash", "scripts/test.sh"],
            ["node", "scripts/check.js"],
            ["yarn", "lint"],
            ["yarn", "--cwd", "packages/web", "test", "src/a.test.ts"],
            ["pnpm", "--filter", "@app/web", "test", "src/a.test.ts"],
        ):
            self.assertTrue(GATE.runnable(command), command)
        for command in (["bash", "-c", "rm -rf x"], ["sh", "deploy.sh"], ["rm", "-rf", "x"], ["node", "-e", "1"]):
            self.assertFalse(GATE.runnable(command), command)


class HookTests(unittest.TestCase):
    def call(self, payload, harness):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "hook", "--harness", harness],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_each_harness_gets_a_deny_for_a_heavy_run_outside_the_gate(self):
        claude = self.call({"tool_name": "Bash", "tool_input": {"command": "yarn test"}}, "claude")
        decision = claude["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("--heavy -- yarn test", decision["permissionDecisionReason"])
        codex = self.call({"tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", "npx vitest run"]}}, "codex")
        self.assertEqual(codex["hookSpecificOutput"]["permissionDecision"], "deny")
        gemini = self.call({"toolCall": {"name": "run_command", "args": {"CommandLine": "tsc --noEmit"}}}, "gemini")
        self.assertEqual(gemini["decision"], "deny")
        self.assertIn("tsc --noEmit", gemini["reason"])

    def test_everything_else_is_left_to_the_harness(self):
        for payload, harness in (
            ({"tool_name": "Bash", "tool_input": {"command": "vitest run src/a.test.ts"}}, "claude"),
            ({"tool_name": "Bash", "tool_input": {"command": "ork gate --heavy -- yarn test"}}, "codex"),
            ({"toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}}}, "gemini"),
            ({"tool_name": "Read", "tool_input": {"file_path": "/x"}}, "claude"),
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(self.call(payload, harness))

    def test_an_unreadable_payload_never_breaks_the_turn(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "hook"], input="{not json", capture_output=True, text=True, timeout=20
        )
        self.assertEqual((result.returncode, result.stdout), (0, ""))


PROBE = """
import os, sys, time, unittest
from pathlib import Path

class Probe(unittest.TestCase):
    def test_probe(self):
        out = Path(os.environ["PROBE_OUT"])
        with out.open("a") as handle:
            handle.write(f"start {time.time()} {os.environ.get('VITEST_MAX_WORKERS')} {os.getpriority(os.PRIO_PROCESS, 0)}\\n")
        touch = os.environ.get("PROBE_TOUCH")
        if touch:
            Path(touch).write_text(f"touched {time.time()}\\n")
        sig = os.environ.get("PROBE_SIGNAL")
        if sig:
            os.kill(os.getpid(), int(sig))
        time.sleep(float(os.environ.get("PROBE_SLEEP", "0")))
        with out.open("a") as handle:
            handle.write(f"end {time.time()}\\n")
        self.assertNotEqual(os.environ.get("PROBE_FAIL"), "1")
"""


def init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (path / "tracked.txt").write_text("v1\n")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path.resolve()


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        (self.directory / "probe_test.py").write_text(textwrap.dedent(PROBE))
        self.env = {
            **os.environ,
            "AGENT_GATE_DIR": str(self.directory / "gate"),
            "PROBE_OUT": str(self.directory / "probe.out"),
        }
        for variable in GATE.SESSION_VARIABLES:
            self.env.pop(variable, None)

    def tearDown(self):
        self.temporary.cleanup()

    def gate(self, *args, env=None, wait=True):
        process = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=self.directory,
            env={**self.env, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if not wait:
            return process
        stdout, stderr = process.communicate(timeout=60)
        return process.returncode, stdout, stderr

    def probe_lines(self):
        return (self.directory / "probe.out").read_text().splitlines()

    def wait_for_start(self, count=1):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            out = self.directory / "probe.out"
            if out.exists() and sum(line.startswith("start") for line in out.read_text().splitlines()) >= count:
                return
            time.sleep(0.05)
        self.fail("the probe never started")

    def test_a_run_gets_capped_workers_lower_priority_and_its_exit_code(self):
        code, _, stderr = self.gate("--workers", "3", "--", sys.executable, "-m", "unittest", "probe_test")
        self.assertEqual(code, 0, stderr)
        _, _, workers, nice = self.probe_lines()[0].split()
        self.assertEqual(workers, "3")
        self.assertGreaterEqual(int(nice), GATE.NICE)
        self.assertNotIn("full output saved", stderr)
        self.assertEqual(list((self.directory / "gate").glob("*.log")), [])

    def test_a_failed_run_keeps_its_full_output(self):
        code, _, stderr = self.gate("--", sys.executable, "-m", "unittest", "probe_test", env={"PROBE_FAIL": "1"})
        self.assertEqual(code, 1)
        self.assertIn("full output saved to", stderr)
        (log,) = (self.directory / "gate").glob("*.log")
        self.assertIn("FAILED", log.read_text())

    def test_a_heavy_run_waits_for_a_focused_one_and_says_who_holds_the_gate(self):
        focused = self.gate(
            "--label", "probe one", "--", sys.executable, "-m", "unittest", "probe_test",
            env={"PROBE_SLEEP": "1.5"}, wait=False,
        )  # fmt: skip
        self.wait_for_start()
        status = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "status", "--json"], env=self.env, capture_output=True, text=True
        )
        self.assertIn("probe one", [holder["label"] for holder in json.loads(status.stdout)["holders"]])
        code, _, stderr = self.gate("--heavy", "--", sys.executable, "-m", "unittest", "discover", "-p", "probe_*.py")
        focused.communicate(timeout=30)
        self.assertEqual(code, 0, stderr)
        self.assertIn("gate: waiting for", stderr)
        self.assertIn("`probe one`", stderr)
        first_end = float(self.probe_lines()[1].split()[1])
        second_start = float(self.probe_lines()[2].split()[1])
        self.assertGreaterEqual(second_start, first_end)

    def test_two_focused_runs_share_the_gate(self):
        first = self.gate("--", sys.executable, "-m", "unittest", "probe_test", env={"PROBE_SLEEP": "1.5"}, wait=False)
        self.wait_for_start()
        code, _, stderr = self.gate("--", sys.executable, "-m", "unittest", "probe_test")
        first.communicate(timeout=30)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("waiting", stderr)

    def test_a_killed_run_frees_its_slot(self):
        held = self.gate(
            "--heavy", "--", sys.executable, "-m", "unittest", "probe_test", env={"PROBE_SLEEP": "30"}, wait=False
        )
        self.wait_for_start()
        os.killpg(held.pid, signal.SIGKILL)  # the gate and its workload together
        held.communicate(timeout=10)
        code, _, stderr = self.gate("--heavy", "--", sys.executable, "-m", "unittest", "probe_test")
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("waiting", stderr)

    def test_the_gate_refuses_what_is_not_a_check(self):
        code, _, stderr = self.gate("--", "rm", "-rf", "nothing")
        self.assertEqual(code, 2)
        self.assertIn("test, build, typecheck and lint commands only", stderr)

    def test_an_unusable_gate_directory_still_runs_the_command(self):
        blocker = self.directory / "file"
        blocker.write_text("")
        code, _, stderr = self.gate(
            "--", sys.executable, "-m", "unittest", "probe_test", env={"AGENT_GATE_DIR": str(blocker / "gate")}
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("running without the gate", stderr)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.gate_dir = self.root / "gate"
        self.repo_a = init_git_repo(self.root / "repo_a")
        self.repo_b = init_git_repo(self.root / "repo_b")
        for repo in (self.repo_a, self.repo_b):
            (repo / "probe_test.py").write_text(textwrap.dedent(PROBE))
            subprocess.run(["git", "add", "probe_test.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "add probe"], cwd=repo, check=True)
        self.env = {
            **os.environ,
            "AGENT_GATE_DIR": str(self.gate_dir),
            "PROBE_OUT": str(self.root / "probe.out"),
        }
        for variable in GATE.SESSION_VARIABLES:
            self.env.pop(variable, None)

    def tearDown(self):
        self.temporary.cleanup()

    def gate(self, *args, cwd=None, env=None, wait=True):
        process = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=cwd or self.repo_a,
            env={**self.env, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if not wait:
            return process
        stdout, stderr = process.communicate(timeout=60)
        return process.returncode, stdout, stderr

    def test_ledger_records_runs_and_scopes_status_by_checkout(self):
        code, _, _ = self.gate("--label", "repo-a-unit", "--", sys.executable, "-m", "unittest", "probe_test")
        self.assertEqual(code, 0)
        code, _, _ = self.gate(
            "--label", "repo-b-unit", "--", sys.executable, "-m", "unittest", "probe_test", cwd=self.repo_b
        )
        self.assertEqual(code, 0)
        _, out_a, _ = self.gate("status", "--json", cwd=self.repo_a)
        data_a = json.loads(out_a)
        self.assertEqual(data_a["checkout"], str(self.repo_a))
        self.assertEqual([r["label"] for r in data_a["runs"]], ["repo-a-unit"])
        self.assertEqual(data_a["runs"][0]["sourceState"], "source metadata matched")
        self.assertEqual(data_a["runs"][0]["outcome"], "passed")
        self.assertIsNotNone(data_a["runs"][0]["queuedAt"])
        self.assertIsNotNone(data_a["runs"][0]["startedAt"])
        self.assertIsNotNone(data_a["runs"][0]["finishedAt"])
        _, out_all, _ = self.gate("status", "--all", "--json", cwd=self.repo_a)
        self.assertEqual({r["label"] for r in json.loads(out_all)["runs"]}, {"repo-a-unit", "repo-b-unit"})
        _, text_a, _ = self.gate("status", cwd=self.repo_a)
        self.assertIn("gate: recent:", text_a)
        self.assertIn("`repo-a-unit` passed", text_a)
        self.assertNotIn("repo-b-unit", text_a)

    def test_source_metadata_fingerprint_tracks_dirty_untracked_and_mid_run_edits(self):
        self.gate("--", sys.executable, "-m", "unittest", "probe_test")
        _, second_err = self.gate("--", sys.executable, "-m", "unittest", "probe_test")[1:]
        self.assertIn("previous run of this command:", second_err)
        self.assertIn("source metadata matched", second_err)

        # Modify a tracked file (dirty #1), run, then edit that same dirty file again (dirty #2).
        tracked = self.repo_a / "tracked.txt"
        tracked.write_text("dirty-one\n")
        self.gate("--", sys.executable, "-m", "unittest", "probe_test")
        tracked.write_text("dirty-two-longer\n")
        _, dirty_err = self.gate("--", sys.executable, "-m", "unittest", "probe_test")[1:]
        self.assertIn("source metadata changed", dirty_err)

        # Add an untracked file inside a new subdirectory.
        subdir = self.repo_a / "nested/dir"
        subdir.mkdir(parents=True)
        (subdir / "new.txt").write_text("untracked\n")
        _, untracked_err = self.gate("--", sys.executable, "-m", "unittest", "probe_test")[1:]
        self.assertIn("source metadata changed", untracked_err)

        # Modify a file during the run itself.
        self.gate(
            "--",
            sys.executable,
            "-m",
            "unittest",
            "probe_test",
            env={"PROBE_TOUCH": str(self.repo_a / "tracked.txt")},
        )
        _, status_out, _ = self.gate("status", "--json")
        latest = json.loads(status_out)["runs"][0]
        self.assertTrue(latest["sourceChangedDuringRun"])
        self.assertEqual(latest["sourceState"], "source changed during the run")

    def test_exact_match_requires_same_subdir_and_workers_and_refreshes_after_wait(self):
        self.gate("--workers", "2", "--", sys.executable, "-m", "unittest", "probe_test")
        _, diff_workers_err = self.gate("--workers", "4", "--", sys.executable, "-m", "unittest", "probe_test")[1:]
        self.assertNotIn("previous run of this command:", diff_workers_err)

        sub = self.repo_a / "sub"
        sub.mkdir()
        (sub / "probe_test.py").write_text(textwrap.dedent(PROBE))
        _, diff_cwd_err = self.gate("--workers", "2", "--", sys.executable, "-m", "unittest", "probe_test", cwd=sub)[1:]
        self.assertNotIn("previous run of this command:", diff_cwd_err)

        # Non-git directory records source metadata unknown and never claims exact match.
        outside = self.root / "plain"
        outside.mkdir()
        (outside / "probe_test.py").write_text(textwrap.dedent(PROBE))
        self.gate("--", sys.executable, "-m", "unittest", "probe_test", cwd=outside)
        _, outside_err = self.gate("--", sys.executable, "-m", "unittest", "probe_test", cwd=outside)[1:]
        self.assertNotIn("previous run of this command:", outside_err)
        _, outside_status, _ = self.gate("status", "--json", cwd=outside)
        self.assertEqual(json.loads(outside_status)["runs"][0]["sourceState"], "source metadata unknown")

        # Post-wait refresh when a heavy run finishes while a waiter is queued.
        first = self.gate(
            "--heavy",
            "--",
            sys.executable,
            "-m",
            "unittest",
            "probe_test",
            env={"PROBE_SLEEP": "1.2"},
            wait=False,
        )
        time.sleep(0.2)
        _, _, waited_err = self.gate("--heavy", "--", sys.executable, "-m", "unittest", "probe_test")
        first.communicate(timeout=30)
        self.assertIn("gate: after waiting, previous run of this command:", waited_err)

    def test_heavy_holder_is_deduplicated_and_runs_lock_is_ignored(self):
        heavy = self.gate(
            "--heavy",
            "--label",
            "single-heavy",
            "--",
            sys.executable,
            "-m",
            "unittest",
            "probe_test",
            env={"PROBE_SLEEP": "1.5"},
            wait=False,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                active = GATE.holders(self.gate_dir)
                if active:
                    break
                time.sleep(0.05)
            with GATE.runs_locked(self.gate_dir):
                active = GATE.holders(self.gate_dir)
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["label"], "single-heavy")
        finally:
            heavy.communicate(timeout=30)

    def test_hook_refusal_enriches_from_payload_cwd_and_handles_cd_and_package_flags(self):
        heavy_cmd = f"{sys.executable} -m unittest discover -p probe_*.py"
        self.gate("--heavy", "--", sys.executable, "-m", "unittest", "discover", "-p", "probe_*.py")
        with mock.patch.dict(os.environ, {"AGENT_GATE_DIR": str(self.gate_dir)}):
            # Even when the hook process's own cwd is outside the repo, payload cwd resolves the repo.
            current_cwd = os.getcwd()
            try:
                os.chdir(self.root)
                decision = GATE.hook(
                    {"tool_name": "Bash", "cwd": str(self.repo_a), "tool_input": {"command": heavy_cmd}},
                    "claude",
                )
            finally:
                os.chdir(current_cwd)
            reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("Last matching run in this checkout:", reason)
            self.assertIn("source metadata matched", reason)
            self.assertIn("status` to see active and recent runs.", reason)

            # Env-prefixed or different commands do not falsely claim an exact match.
            prefixed = GATE.hook(
                {"tool_name": "Bash", "cwd": str(self.repo_a), "tool_input": {"command": f"FOO=1 {heavy_cmd}"}},
                "claude",
            )
            prefixed_reason = prefixed["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertNotIn("Last matching run in this checkout:", prefixed_reason)
            self.assertIn("status` to see active and recent runs.", prefixed_reason)

            # Antigravity workspacePaths fallback works when cwd is absent.
            gemini = GATE.hook(
                {
                    "workspacePaths": [str(self.repo_a)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": heavy_cmd}},
                },
                "gemini",
            )
            self.assertIn("Last matching run in this checkout:", gemini["reason"])

            # cd outside the checkout suppresses repo_a's ledger history.
            escaped = GATE.hook(
                {
                    "tool_name": "Bash",
                    "cwd": str(self.repo_a),
                    "tool_input": {"command": f"cd {self.repo_b} && {heavy_cmd}"},
                },
                "claude",
            )
            self.assertNotIn(
                "Last matching run in this checkout:", escaped["hookSpecificOutput"]["permissionDecisionReason"]
            )
            self.assertNotIn(
                "status` to see active and recent runs.", escaped["hookSpecificOutput"]["permissionDecisionReason"]
            )

    def test_launch_error_signal_corrupt_ledger_and_log_pruning(self):
        # Corrupt line in runs.jsonl is tolerated.
        self.gate_dir.mkdir(parents=True, exist_ok=True)
        (self.gate_dir / "runs.jsonl").write_text("{corrupt json\n")

        # Signal death is recorded as signaled.
        code, _, _ = self.gate(
            "--", sys.executable, "-m", "unittest", "probe_test", env={"PROBE_SIGNAL": str(signal.SIGTERM)}
        )
        self.assertEqual(code, 128 + signal.SIGTERM)
        runs = GATE.load_runs(self.gate_dir)
        self.assertEqual(runs[-1]["outcome"], "signaled")
        self.assertEqual(runs[-1]["signal"], signal.SIGTERM)

        # Launch error (missing executable) is recorded as launch_error and exits 2.
        code, _, err = self.gate("--", "pytest", "tests/test_a.py", env={"PATH": "/nonexistent"})
        self.assertEqual(code, 2)
        self.assertIn("cannot run pytest", err)
        runs = GATE.load_runs(self.gate_dir)
        self.assertEqual(runs[-1]["outcome"], "launch_error")

        # Log pruning protects active holder logs, keeps LOGS_KEPT, and marks missing logs expired.
        for existing in self.gate_dir.glob("*.log"):
            existing.unlink()
        active_log = self.gate_dir / "active.log"
        active_log.write_text("in-flight\n")
        old_log = self.gate_dir / "old.log"
        old_log.write_text("old-output\n")
        newest_log = self.gate_dir / "newest.log"
        newest_log.write_text("new-output\n")
        os.utime(active_log, (500, 500))
        os.utime(old_log, (1000, 1000))
        os.utime(newest_log, (2000, 2000))
        with (
            mock.patch.object(GATE, "LOGS_KEPT", 1),
            mock.patch.object(
                GATE,
                "holders",
                return_value=[{"runId": "live", "pid": os.getpid() + 99999, "logPath": str(active_log)}],
            ),
        ):
            GATE.record_run(
                self.gate_dir,
                {
                    "runId": "old-run",
                    "agent": "tester",
                    "heavy": True,
                    "label": "old",
                    "outcome": "failed",
                    "exitCode": 1,
                    "durationSeconds": 1.0,
                    "logPath": str(old_log),
                },
            )
        self.assertTrue(active_log.exists())
        self.assertTrue(newest_log.exists())
        self.assertFalse(old_log.exists())
        loaded = [r for r in GATE.load_runs(self.gate_dir) if r["runId"] == "old-run"][0]
        self.assertIsNone(loaded["logPath"])
        self.assertTrue(loaded["logExpired"])
        self.assertIn("; log expired", GATE.describe_run(loaded))

        # A ledger recording failure prints a warning and never raises.
        import io

        err_buf = io.StringIO()
        with (
            mock.patch.object(GATE, "record_run", side_effect=RuntimeError("disk full")),
            mock.patch.object(sys, "stderr", err_buf),
        ):
            GATE.safe_record_run(self.gate_dir, {"runId": "x"})
        self.assertIn("gate: could not record the run: disk full", err_buf.getvalue())


class InstallTests(unittest.TestCase):
    def test_install_adds_one_hook_per_harness_and_uninstall_removes_only_it(self):
        with tempfile.TemporaryDirectory() as home_name:
            home = Path(home_name)
            (home / ".claude").mkdir()
            claude = home / ".claude/settings.json"
            other = {"type": "command", "command": "other-hook"}
            claude.write_text(
                json.dumps({"model": "x", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}})
            )
            gemini = home / ".gemini/config/hooks.json"
            gemini.parent.mkdir(parents=True)
            gemini.write_text(json.dumps({"peer-mailbox": {"Stop": []}}))
            with (
                mock.patch.object(GATE.shutil, "which", side_effect=lambda name: f"/bin/{name}"),
                mock.patch.dict(os.environ, {"CODEX_HOME": str(home / ".codex")}),
            ):
                GATE.install(home=home)
                GATE.install(home=home)  # again: nothing changes
                installed = json.loads(claude.read_text())
                commands = [h["command"] for g in installed["hooks"]["PreToolUse"] for h in g["hooks"]]
                self.assertEqual(len(commands), 2)
                self.assertIn("other-hook", commands)
                self.assertEqual(installed["model"], "x")
                codex = json.loads((home / ".codex/hooks.json").read_text())["hooks"]["PreToolUse"][0]
                self.assertIn("Bash", codex["matcher"])
                self.assertIn("--harness codex", codex["hooks"][0]["command"])
                agy = json.loads(gemini.read_text())
                self.assertEqual(agy["agent-gate"]["PreToolUse"][0]["matcher"], "run_command")
                self.assertIn("peer-mailbox", agy)
                GATE.install(remove=True, home=home)
                self.assertEqual(json.loads(claude.read_text())["hooks"]["PreToolUse"][0]["hooks"], [other])
                self.assertNotIn("agent-gate", json.loads(gemini.read_text()))
                self.assertNotIn("hooks", json.loads((home / ".codex/hooks.json").read_text()))


if __name__ == "__main__":
    unittest.main()
