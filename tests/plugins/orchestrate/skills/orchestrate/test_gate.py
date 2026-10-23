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
]
NOT_HEAVY = [
    "npm test -- src/a.test.ts",
    "yarn test src/a.test.ts",
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

    def test_jest_gets_a_worker_cap_unless_it_sets_one(self):
        self.assertEqual(GATE.capped(["npx", "jest", "a"], 2), ["npx", "jest", "a", "--maxWorkers=2"])
        self.assertEqual(GATE.capped(["npx", "jest", "-w", "1"], 2), ["npx", "jest", "-w", "1"])
        self.assertEqual(GATE.capped(["npx", "jest", "--runInBand"], 2), ["npx", "jest", "--runInBand"])
        self.assertEqual(GATE.capped(["vitest", "run"], 2), ["vitest", "run"])

    def test_the_gate_runs_repository_check_scripts_but_nothing_arbitrary(self):
        for command in (["bash", "scripts/test.sh"], ["node", "scripts/check.js"], ["yarn", "lint"]):
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
        time.sleep(float(os.environ.get("PROBE_SLEEP", "0")))
        with out.open("a") as handle:
            handle.write(f"end {time.time()}\\n")
        self.assertNotEqual(os.environ.get("PROBE_FAIL"), "1")
"""


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
