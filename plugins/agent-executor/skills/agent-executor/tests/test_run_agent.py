from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_agent.py"
# Runner tests must never query real subscription accounts through ai-usage; test_quota.py sets
# its own fake per test.
os.environ["AI_USAGE_BIN"] = str(Path(__file__).parent / "no-ai-usage")
SPEC = importlib.util.spec_from_file_location("agent_executor_runner", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class TemporaryGitRepository:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="agy-runner-test-")
        self.path = Path(self._temporary.name)
        self.git("init", "-q")
        self.git("config", "user.email", "agy-runner@example.test")
        self.git("config", "user.name", "Agy Runner Test")
        self.write("tracked.txt", "initial\n")
        self.write(".gitignore", "plans/\n")
        self.git("add", "tracked.txt", ".gitignore")
        self.git("commit", "-qm", "initial")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def write(self, relative: str, content: str) -> None:
        destination = self.path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    def close(self) -> None:
        self._temporary.cleanup()


class PathScopeTests(unittest.TestCase):
    def test_normalizes_and_deduplicates_repository_paths(self) -> None:
        self.assertEqual(
            RUNNER.normalize_repo_paths(
                ["src/jobs", "src/jobs", "README.md"], option="--allow-path"
            ),
            ["src/jobs", "README.md"],
        )

    def test_rejects_root_absolute_and_parent_paths(self) -> None:
        for invalid in (
            ".",
            "",
            "/tmp/out",
            "../outside",
            "src/../../outside",
            ".git",
            ".git/config",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RUNNER.RunnerError):
                    RUNNER.normalize_repo_paths([invalid], option="--track-path")

    def test_scope_violations_respect_directory_prefixes(self) -> None:
        delta = {
            "changed_paths": ["README.md", "src/jobs/a.ts", "tests/a-test.ts"],
            "tracked_paths_changed": [],
        }
        self.assertEqual(
            RUNNER.find_scope_violations(delta, ["README.md", "src/jobs"]),
            ["tests/a-test.ts"],
        )

    def test_no_allowed_paths_enforces_a_read_only_run(self) -> None:
        delta = {
            "changed_paths": ["unexpected.txt"],
            "tracked_paths_changed": [],
        }
        self.assertEqual(RUNNER.find_scope_violations(delta, []), ["unexpected.txt"])


class GitStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = TemporaryGitRepository()

    def tearDown(self) -> None:
        self.repository.close()

    def test_detects_changes_to_a_preexisting_dirty_file(self) -> None:
        self.repository.write("tracked.txt", "user change\n")
        before = RUNNER.git_state(self.repository.path)
        self.repository.write("tracked.txt", "executor change\n")
        after = RUNNER.git_state(self.repository.path)
        self.assertEqual(
            RUNNER.state_delta(before, after)["changed_paths"], ["tracked.txt"]
        )

    def test_tracks_an_ignored_deliverable(self) -> None:
        tracked = ["plans/feedback.md"]
        before = RUNNER.git_state(self.repository.path, tracked)
        self.repository.write("plans/feedback.md", "feedback\n")
        after = RUNNER.git_state(self.repository.path, tracked)
        delta = RUNNER.state_delta(before, after)
        self.assertEqual(delta["tracked_paths_changed"], tracked)
        self.assertIn("plans/feedback.md", delta["changed_paths"])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_detects_staging_a_preexisting_dirty_file(self) -> None:
        self.repository.write("tracked.txt", "user change\n")
        before = RUNNER.git_state(self.repository.path)
        self.repository.git("add", "tracked.txt")
        after = RUNNER.git_state(self.repository.path)
        self.assertEqual(
            RUNNER.state_delta(before, after)["changed_paths"], ["tracked.txt"]
        )

    def test_detects_git_history_changes(self) -> None:
        before = RUNNER.git_identity(self.repository.path)
        self.repository.write("tracked.txt", "committed by executor\n")
        self.repository.git("add", "tracked.txt")
        self.repository.git("commit", "-qm", "executor commit")
        after = RUNNER.git_identity(self.repository.path)
        violations = RUNNER.find_history_violations(before, after)
        self.assertIn("head", violations)
        self.assertIn("head_reflog_sha256", violations)
        self.assertIn("branch_ref", violations)

    def test_sibling_worktree_activity_is_not_a_history_violation(self) -> None:
        before, activity_before = RUNNER.git_identity(self.repository.path), RUNNER.git_activity(self.repository.path)
        sibling = self.repository.path.parent / (self.repository.path.name + "-sibling")
        self.repository.git("worktree", "add", "-q", "-b", "sibling-work", str(sibling))
        (sibling / "sibling.txt").write_text("parallel work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(sibling), "add", "sibling.txt"], check=True)
        subprocess.run(["git", "-C", str(sibling), "commit", "-qm", "sibling commit"], check=True)
        after, activity_after = RUNNER.git_identity(self.repository.path), RUNNER.git_activity(self.repository.path)
        self.assertEqual(RUNNER.find_history_violations(before, after), [])
        self.assertEqual(
            RUNNER.find_history_violations(activity_before, activity_after),
            ["local_ref_count", "local_ref_names_sha256", "worktree_count", "worktree_paths_sha256"],
        )

    def test_detects_branch_switches_and_stashes(self) -> None:
        before_branch = RUNNER.git_identity(self.repository.path)
        self.repository.git("switch", "-qc", "executor-branch")
        after_branch = RUNNER.git_identity(self.repository.path)
        self.assertIn(
            "branch", RUNNER.find_history_violations(before_branch, after_branch)
        )

        self.repository.write("tracked.txt", "stashed by executor\n")
        before_stash = RUNNER.git_identity(self.repository.path)
        self.repository.git("stash", "push", "-qm", "executor stash")
        after_stash = RUNNER.git_identity(self.repository.path)
        stash_violations = RUNNER.find_history_violations(before_stash, after_stash)
        self.assertIn("stash_sha256", stash_violations)


class ReportingAndArtifactTests(unittest.TestCase):
    def test_brief_preflight_accepts_absent_or_exact_contract_and_rejects_malformed(self) -> None:
        self.assertEqual(
            RUNNER.brief_report_contract_violations("# Objective\n\nDo the work.\n"),
            [],
        )
        exact = (
            "# Final response\n\n"
            "STATUS\nFILES CHANGED\nCOMMANDS RUN\nVERIFICATION\nRISKS OR BLOCKERS\n"
        )
        self.assertEqual(RUNNER.brief_report_contract_violations(exact), [])
        malformed = "# Final response\n\nSTATUS\nFILES CHANGED\nVERIFICATION\n"
        violations = RUNNER.brief_report_contract_violations(malformed)
        self.assertIn("brief_missing_heading:COMMANDS RUN", violations)
        self.assertIn("brief_missing_heading:RISKS OR BLOCKERS", violations)
        with self.assertRaises(RUNNER.RunnerError):
            RUNNER.validate_brief_contract(malformed)

    def test_extracts_only_the_last_structured_report(self) -> None:
        stdout = (
            "I will inspect files.\n"
            "STATUS mentioned in prose is not a heading.\n"
            "### STATUS\ncompleted\n\n### FILES CHANGED\n- a.py\n"
        )
        report, extracted = RUNNER.extract_final_report(stdout)
        self.assertTrue(extracted)
        self.assertTrue(report.startswith("### STATUS\ncompleted"))
        self.assertNotIn("I will inspect", report)

    def test_malformed_report_returns_only_a_bounded_tail(self) -> None:
        report, extracted = RUNNER.extract_final_report("x" * 5000)
        self.assertFalse(extracted)
        self.assertEqual(len(report), 4000)

    def test_parses_canonical_and_legacy_reported_outcomes(self) -> None:
        for status, expected in (
            ("COMPLETE", "complete"),
            ("completed", "complete"),
            ("BLOCKED", "blocked"),
            ("BLOCKER (missing authority)", "blocked"),
            ("FAILED", "failed"),
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    RUNNER.parse_reported_outcome(
                        f"### STATUS\n{status}\n\n### FILES CHANGED\n- none",
                        True,
                    ),
                    expected,
                )
        self.assertEqual(
            RUNNER.parse_reported_outcome(
                "### STATUS\nMostly done\n\n### FILES CHANGED\n- none",
                True,
            ),
            "unknown",
        )
        self.assertEqual(
            RUNNER.parse_reported_outcome(
                "### STATUS\nCOMPLETE - except for one blocker",
                True,
            ),
            "unknown",
        )

    def test_requires_the_complete_ordered_report_contract(self) -> None:
        valid_report = (
            "### STATUS\nCOMPLETE\n"
            "### FILES CHANGED\n- none\n"
            "### COMMANDS RUN\n- none\n"
            "### VERIFICATION\n- none\n"
            "### RISKS OR BLOCKERS\n- none\n"
        )
        self.assertEqual(
            RUNNER.report_contract_violations(valid_report, True),
            [],
        )
        self.assertIn(
            "missing_heading:COMMANDS RUN",
            RUNNER.report_contract_violations(
                "### STATUS\nCOMPLETE\n### FILES CHANGED\n- none",
                True,
            ),
        )
        self.assertEqual(
            RUNNER.report_contract_violations(
                valid_report.replace(
                    "### FILES CHANGED\n- none\n### COMMANDS RUN\n- none\n",
                    "### COMMANDS RUN\n- none\n### FILES CHANGED\n- none\n",
                ),
                True,
            ),
            ["heading_order"],
        )

    def test_extracts_jsonl_event_errors(self) -> None:
        stdout = json.dumps(
            {
                "type": "error",
                "error": {"data": {"message": "Insufficient credits"}},
            }
        )
        self.assertEqual(RUNNER.parse_event_errors(stdout), ["Insufficient credits"])

    def test_private_artifacts_use_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-artifact-test-") as directory:
            target = Path(directory) / "result.json"
            RUNNER.write_private_text(target, "{}\n")
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_verification_tail_lines_are_bounded(self) -> None:
        self.assertEqual(
            len(RUNNER.nonempty_tail("x" * 2000)[0]),
            RUNNER.MAX_VERIFICATION_TAIL_LINE_CHARS,
        )

    def test_json_artifacts_escape_surrogate_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-artifact-test-") as directory:
            target = Path(directory) / "result.json"
            RUNNER.write_json(target, {"changed_paths": ["bad\udcff-name.txt"]})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["changed_paths"],
                ["bad\udcff-name.txt"],
            )

    def test_requested_artifact_directory_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agy-artifact-parent-") as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            requested = Path(directory) / "run"
            created = RUNNER.prepare_out_dir(requested, repository)
            mode = stat.S_IMODE(created.stat().st_mode)
            self.assertEqual(mode, 0o700)

    def test_rejects_artifact_directory_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-artifact-parent-") as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            with self.assertRaises(RUNNER.RunnerError):
                RUNNER.prepare_out_dir(repository / "artifacts", repository)

    def test_completion_event_is_private_listable_and_acknowledgeable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-event-test-") as directory:
            root = Path(directory)
            result_path = root / "result.json"
            RUNNER.write_json(result_path, {"status": "completed"})
            environment = {"XDG_CACHE_HOME": str(root / "cache")}
            with mock.patch.dict(os.environ, environment):
                event_path = RUNNER.prepare_completion_event_path()
                event = RUNNER.publish_completion_event(
                    result={
                        "status": "completed",
                        "exit_code": 0,
                        "engine": "agy",
                        "model": "gemini-test",
                        "variant": None,
                        "repository": str(root),
                        "scope_violations": [],
                        "history_violations": [],
                    },
                    result_path=result_path,
                    event_path=event_path,
                    notification_mode="none",
                    completion_hook=None,
                )
                events, errors = RUNNER.completion_events(
                    repository=root,
                    include_acknowledged=False,
                )
                self.assertEqual(errors, [])
                self.assertEqual([item["event_id"] for item in events], [event["event_id"]])
                self.assertEqual(stat.S_IMODE(event_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(event_path.parent.stat().st_mode), 0o700)

                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        RUNNER.events_main(["--ack", event["event_id"]]),
                        0,
                    )
                self.assertIn("AGENT_EVENT_ACKNOWLEDGED=", output.getvalue())
                unread, errors = RUNNER.completion_events(
                    repository=root,
                    include_acknowledged=False,
                )
                self.assertEqual((unread, errors), ([], []))
                acknowledged, errors = RUNNER.completion_events(
                    repository=root,
                    include_acknowledged=True,
                )
                self.assertEqual(errors, [])
                self.assertIsNotNone(acknowledged[0]["acknowledged_at"])

    def test_event_wait_blocks_internally_and_emits_one_compact_signal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-event-wait-test-") as directory:
            root = Path(directory)
            result_path = root / "result.json"
            RUNNER.write_json(result_path, {"status": "completed"})
            with mock.patch.dict(
                os.environ,
                {"XDG_CACHE_HOME": str(root / "cache")},
            ):
                event_path = RUNNER.prepare_completion_event_path()

                def publish() -> None:
                    time.sleep(0.03)
                    RUNNER.publish_completion_event(
                        result={
                            "status": "completed",
                            "exit_code": 0,
                            "engine": "agy",
                            "model": "gemini-test",
                            "repository": str(root),
                            "scope_violations": [],
                            "history_violations": [],
                        },
                        result_path=result_path,
                        event_path=event_path,
                        notification_mode="none",
                        completion_hook=None,
                    )

                publisher = threading.Thread(target=publish)
                publisher.start()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        RUNNER.events_main(
                            [
                                "wait",
                                str(event_path),
                                "--timeout",
                                "1s",
                                "--poll-seconds",
                                "0.005",
                            ]
                        ),
                        0,
                    )
                publisher.join(timeout=1)

            signals = [
                json.loads(line) for line in output.getvalue().splitlines()
            ]
            self.assertEqual(len(signals), 1)
            self.assertEqual(
                signals[0]["schema"],
                RUNNER.COMPLETION_SIGNAL_SCHEMA,
            )
            self.assertEqual(signals[0]["event_id"], event_path.stem)
            self.assertEqual(signals[0]["status"], "completed")
            self.assertEqual(signals[0]["result_path"], str(result_path.resolve()))
            self.assertNotIn("deliveries", signals[0])

    def test_event_follow_deduplicates_references_and_streams_each_event_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-event-follow-test-") as directory:
            root = Path(directory)
            result_path = root / "result.json"
            RUNNER.write_json(result_path, {"status": "completed"})
            with mock.patch.dict(
                os.environ,
                {"XDG_CACHE_HOME": str(root / "cache")},
            ):
                event_paths = [
                    RUNNER.prepare_completion_event_path(),
                    RUNNER.prepare_completion_event_path(),
                ]
                for index, event_path in enumerate(event_paths):
                    RUNNER.publish_completion_event(
                        result={
                            "status": "completed",
                            "exit_code": 0,
                            "engine": "codex",
                            "model": f"gpt-test-{index}",
                            "repository": str(root),
                        },
                        result_path=result_path,
                        event_path=event_path,
                        notification_mode="none",
                        completion_hook=None,
                    )

                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        RUNNER.events_main(
                            [
                                "follow",
                                event_paths[0].stem,
                                str(event_paths[1]),
                                event_paths[0].stem,
                                "--timeout",
                                "1s",
                                "--poll-seconds",
                                "0.005",
                            ]
                        ),
                        0,
                    )

            signals = [
                json.loads(line) for line in output.getvalue().splitlines()
            ]
            self.assertEqual(
                [signal["event_id"] for signal in signals],
                [path.stem for path in event_paths],
            )

    def test_event_wait_times_out_without_model_polling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-event-timeout-test-") as directory:
            root = Path(directory)
            with mock.patch.dict(
                os.environ,
                {"XDG_CACHE_HOME": str(root / "cache")},
            ):
                event_path = RUNNER.prepare_completion_event_path()
                with self.assertRaises(RUNNER.RunnerError) as raised:
                    RUNNER.events_main(
                        [
                            "wait",
                            event_path.stem,
                            "--timeout",
                            "0.02s",
                            "--poll-seconds",
                            "0.005",
                        ]
                    )
            self.assertEqual(raised.exception.exit_code, 12)
            self.assertIn(event_path.stem, str(raised.exception))

    def test_completion_hook_receives_private_event_path_and_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-hook-test-") as directory:
            root = Path(directory)
            hook = root / "hook.py"
            hook.write_text(
                f"#!{sys.executable}\n"
                "import os\n"
                "import pathlib\n"
                "import sys\n"
                "pathlib.Path(sys.argv[1] + '.hooked').write_text("
                "os.environ['AGENT_STATUS'], encoding='utf-8')\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            result_path = root / "result.json"
            RUNNER.write_json(result_path, {"status": "completed"})
            with mock.patch.dict(
                os.environ,
                {"XDG_CACHE_HOME": str(root / "cache")},
            ):
                event_path = RUNNER.prepare_completion_event_path()
                event = RUNNER.publish_completion_event(
                    result={
                        "status": "completed",
                        "exit_code": 0,
                        "engine": "codex",
                        "model": "gpt-test",
                        "repository": str(root),
                    },
                    result_path=result_path,
                    event_path=event_path,
                    notification_mode="none",
                    completion_hook=hook,
                )
            self.assertEqual(
                Path(str(event_path) + ".hooked").read_text(encoding="utf-8"),
                "completed",
            )
            self.assertEqual(event["deliveries"][0]["status"], "sent")

    def test_completion_hook_cannot_live_inside_an_executor_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-hook-scope-test-") as directory:
            workspace = Path(directory).resolve()
            hook = workspace / "hook"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook.chmod(0o700)
            with self.assertRaises(RUNNER.RunnerError):
                RUNNER.ensure_hook_outside_workspaces(hook, [workspace])

    def test_uncaught_detached_worker_failure_still_publishes_an_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-failure-event-test-") as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            out_dir = root / "run"
            with mock.patch.dict(
                os.environ,
                {"XDG_CACHE_HOME": str(root / "cache")},
            ):
                event_path = RUNNER.prepare_completion_event_path()
                argv = [
                    str(SCRIPT),
                    "--cwd",
                    str(repository),
                    "--brief",
                    str(root / "unused-brief.md"),
                    "--out-dir",
                    str(out_dir),
                    "--completion-event",
                    str(event_path),
                    "--notify",
                    "none",
                ]
                with mock.patch.object(sys, "argv", argv):
                    RUNNER.publish_uncaught_completion_failure(
                        RuntimeError("worker exploded"),
                        1,
                    )
            result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "runner_failed")
            self.assertEqual(event["status"], "runner_failed")
            self.assertEqual(event["error"], "worker exploded")

    def test_macos_desktop_notification_uses_argument_safe_osascript(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with (
            mock.patch.object(RUNNER.sys, "platform", "darwin"),
            mock.patch.object(
                RUNNER.shutil,
                "which",
                return_value="/usr/bin/osascript",
            ),
            mock.patch.object(
                RUNNER.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            delivery = RUNNER.send_desktop_notification(
                {
                    "status": "completed",
                    "engine": "agy",
                    "model": "gemini-test",
                    "repository": "/tmp/example",
                }
            )
        self.assertEqual(delivery["status"], "sent")
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["Agent executor: completed", "agy · gemini-test · example"])

    def test_safety_violations_override_a_successful_executor_exit(self) -> None:
        status, exit_code = RUNNER.classify_result(
            history_violations=["head"],
            scope_violations=[],
            timed_out=False,
            executor_exit_code=0,
            stdout_present=True,
            report_extracted=True,
            expect_changes=True,
            workspace_changed=True,
        )
        self.assertEqual((status, exit_code), ("history_violation", 21))

        status, exit_code = RUNNER.classify_result(
            history_violations=[],
            scope_violations=["outside.txt"],
            timed_out=False,
            executor_exit_code=0,
            stdout_present=True,
            report_extracted=True,
            expect_changes=True,
            workspace_changed=True,
        )
        self.assertEqual((status, exit_code), ("scope_violation", 22))

    def test_reported_blocker_is_a_nonzero_runner_outcome(self) -> None:
        status, exit_code = RUNNER.classify_result(
            history_violations=[],
            scope_violations=[],
            timed_out=False,
            executor_exit_code=0,
            stdout_present=True,
            report_extracted=True,
            expect_changes=True,
            workspace_changed=True,
            reported_outcome="blocked",
        )
        self.assertEqual((status, exit_code), ("blocked", 24))

    def test_wait_emits_heartbeats_and_terminates_on_timeout(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            timed_out = RUNNER.wait_with_heartbeats(
                process,
                timeout_seconds=0.08,
                heartbeat_seconds=0.02,
                monotonic_start=RUNNER.time.monotonic(),
            )
        self.assertTrue(timed_out)
        self.assertIsNotNone(process.returncode)
        self.assertIn("AGENT_PROGRESS", output.getvalue())


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = TemporaryGitRepository()
        self.external = tempfile.TemporaryDirectory(prefix="agent-runner-external-")
        self.external_path = Path(self.external.name)
        self.run_index = 0
        self._write_fake_executors()

    def _write_executable(self, name: str, source: str) -> None:
        path = self.external_path / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)

    def _write_fake_executors(self) -> None:
        common = r'''
import json
import os
import pathlib
import subprocess
import sys

REPORT = """I will narrate a tool call.
### STATUS
completed

### FILES CHANGED
- allowed/output.txt

### COMMANDS RUN
- none

### VERIFICATION
- fixture

### RISKS OR BLOCKERS
- none
"""

def mutate():
    if os.environ.get("FAKE_NO_CHANGE") == "1":
        return
    destination = pathlib.Path(os.environ.get("FAKE_OUTPUT", "allowed/output.txt"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(os.environ.get("FAKE_CONTENT", "executor output\n"), encoding="utf-8")
    if os.environ.get("FAKE_COMMIT") == "1":
        subprocess.run(["git", "add", str(destination)], check=True)
        subprocess.run(
            ["git", "commit", "-qm", "forbidden executor commit"], check=True
        )

def transient_failure():
    count_path = os.environ.get("FAKE_TRANSIENT_COUNT_PATH")
    failures = int(os.environ.get("FAKE_TRANSIENT_FAILURES", "0"))
    if not count_path or failures <= 0:
        return False
    path = pathlib.Path(count_path)
    attempts = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    if attempts >= failures:
        return False
    path.write_text(str(attempts + 1), encoding="utf-8")
    print("network error: temporary provider failure", file=sys.stderr)
    return True

def report():
    if os.environ.get("FAKE_MALFORMED") == "1":
        return "unstructured output"
    status = os.environ.get("FAKE_REPORT_STATUS", "completed")
    if os.environ.get("FAKE_PARTIAL_REPORT") == "1":
        return f"### STATUS\n{status}\n"
    return REPORT.replace(
        "\ncompleted\n\n### FILES CHANGED",
        f"\n{status}\n\n### FILES CHANGED",
        1,
    )

def capture(extra=None):
    payload = {"argv": sys.argv[1:]}
    if extra:
        payload.update(extra)
    pathlib.Path(os.environ["FAKE_CAPTURE"]).write_text(
        json.dumps(payload), encoding="utf-8"
    )
'''
        self._write_executable(
            "codex",
            "#!/usr/bin/env python3\n"
            + common
            + r"""
if "--version" in sys.argv:
    print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli test 0.0"))
elif sys.argv[1:3] == ["app-server", "--stdio"]:
    count_path = os.environ.get("FAKE_CATALOG_COUNT")
    if count_path:
        with pathlib.Path(count_path).open("a", encoding="utf-8") as handle:
            handle.write("codex\n")
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("method") == "initialize":
            print(json.dumps({"id": request["id"], "result": {}}), flush=True)
        elif request.get("method") == "model/list":
            data = [
                {
                    "id": "gpt-5.6-luna",
                    "displayName": "Luna",
                    "isDefault": False,
                    "hidden": False,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low"},
                        {"reasoningEffort": "high"},
                        {"reasoningEffort": "max"},
                    ],
                    "defaultReasoningEffort": "high",
                },
                {
                    "id": "gpt-test-default",
                    "displayName": "Test Default",
                    "isDefault": True,
                    "hidden": False,
                },
                {
                    "id": "codex-hidden",
                    "displayName": "Hidden",
                    "isDefault": False,
                    "hidden": True,
                },
            ]
            print(
                json.dumps(
                    {
                        "id": request["id"],
                        "result": {"data": data, "nextCursor": None},
                    }
                ),
                flush=True,
            )
else:
    if transient_failure():
        raise SystemExit(75)
    mutate()
    prompt = sys.stdin.read()
    output_index = sys.argv.index("--output-last-message") + 1
    pathlib.Path(sys.argv[output_index]).write_text(report(), encoding="utf-8")
    capture({"stdin": prompt})
    print(json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"}))
    for usage in json.loads(os.environ.get("FAKE_CODEX_USAGE", "[]")):
        print(json.dumps({"type": "turn.completed", "usage": usage}))
""",
        )
        self._write_executable(
            "agy",
            "#!/usr/bin/env python3\n"
            + common
            + r"""
if "--version" in sys.argv:
    print(os.environ.get("FAKE_AGY_VERSION", "agy test 0.0"))
elif len(sys.argv) > 1 and sys.argv[1] == "models":
    count_path = os.environ.get("FAKE_CATALOG_COUNT")
    if count_path:
        with pathlib.Path(count_path).open("a", encoding="utf-8") as handle:
            handle.write("agy\n")
    print(os.environ.get("FAKE_AGY_MODELS", "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)"))
else:
    if transient_failure():
        raise SystemExit(75)
    mutate()
    log_index = sys.argv.index("--log-file") + 1
    pathlib.Path(sys.argv[log_index]).write_text(
        "Created conversation 11111111-1111-1111-1111-111111111111\n",
        encoding="utf-8",
    )
    capture(
        {
            "term": os.environ.get("TERM"),
            "no_color": os.environ.get("NO_COLOR"),
        }
    )
    if "--output-format" in sys.argv:
        print(json.dumps({"type": "init", "conversation_id": "11111111-1111-1111-1111-111111111111", "model": sys.argv[sys.argv.index("--model") + 1]}))
        print(json.dumps({"type": "result", "status": os.environ.get("FAKE_AGY_STATUS", "success"), "conversation_id": "11111111-1111-1111-1111-111111111111", "response": report(), "error": os.environ.get("FAKE_AGY_ERROR", ""), "usage": {"input_tokens": 278, "output_tokens": 4, "cache_read_tokens": 30214, "total_tokens": 282}}))
    else:
        print(report())
""",
        )
        self._write_executable(
            "opencode",
            "#!/usr/bin/env python3\n"
            + common
            + r"""
if "--version" in sys.argv:
    print(os.environ.get("FAKE_OPENCODE_VERSION", "1.18.4-test"))
elif len(sys.argv) > 1 and sys.argv[1] == "models":
    count_path = os.environ.get("FAKE_CATALOG_COUNT")
    if count_path:
        with pathlib.Path(count_path).open("a", encoding="utf-8") as handle:
            handle.write("opencode\n")
    print(
        os.environ.get(
            "FAKE_OPENCODE_MODELS",
            "openrouter/z-ai/glm-5.3-flash\nopenrouter/deepseek/deepseek-v4-flash",
        )
    )
else:
    if transient_failure():
        raise SystemExit(75)
    mutate()
    capture(
        {
            "permission": os.environ.get("OPENCODE_PERMISSION"),
            "auto_share": os.environ.get("OPENCODE_AUTO_SHARE"),
            "disable_autoupdate": os.environ.get("OPENCODE_DISABLE_AUTOUPDATE"),
            "config_content": os.environ.get("OPENCODE_CONFIG_CONTENT"),
        }
    )
    event = {
        "type": "text",
        "sessionID": "ses_opencode_1",
        "part": {"type": "text", "text": report()},
    }
    print(json.dumps(event))
""",
        )
        self._write_executable(
            "claude",
            "#!/usr/bin/env python3\n"
            + common
            + r"""
if "--version" in sys.argv:
    print("claude test 0.0")
elif "-p" in sys.argv:
    # An execution run: the brief arrives on stdin.
    prompt = sys.stdin.read()
    mutate()
    capture({"stdin": prompt, "argv": sys.argv[1:], "claudecode": os.environ.get("CLAUDECODE"),
             "claude_code_token": os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN"),
             "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR")})
    error = os.environ.get("FAKE_CLAUDE_ERROR")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": bool(error), "session_id": "claude-session-1",
                      "result": error or report(), "total_cost_usd": 0.25,
                      "usage": {"input_tokens": 10, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 30, "output_tokens": 40}}))
else:
    pathlib.Path(os.environ["FAKE_CLAUDE_UNSAFE_CALL"]).write_text(
        "called\n", encoding="utf-8"
    )
    raise SystemExit(99)
""",
        )

    def tearDown(self) -> None:
        self.external.cleanup()
        self.repository.close()

    def run_main(
        self,
        *,
        engine: str = "codex",
        model: str | None = None,
        variant: str | None = None,
        fake_output: str = "allowed/output.txt",
        fake_commit: bool = False,
        no_change: bool = False,
        malformed: bool = False,
        partial_report: bool = False,
        report_status: str = "completed",
        expect_changes: bool = True,
        allow_path: str | None = "allowed",
        track_path: str | None = None,
        session: str | None = None,
        verify_commands: list[str] | None = None,
        verify_timeout: str = "10m",
        task_spec: dict | None = None,
        retry_read_only: int = 0,
        transient_failures: int = 0,
        extra_args: list[str] | None = None,
    ) -> tuple[int, dict, Path, str, dict]:
        self.run_index += 1
        out_dir = self.external_path / f"artifacts-{self.run_index}"
        capture_path = self.external_path / f"capture-{self.run_index}.json"
        input_args: list[str]
        if task_spec is not None:
            spec = self.external_path / f"task-spec-{self.run_index}.json"
            spec.write_text(json.dumps(task_spec), encoding="utf-8")
            input_args = ["--task-spec", str(spec)]
        else:
            brief = self.external_path / f"brief-{self.run_index}.md"
            brief.write_text(
                "# Objective\n\nCreate the fixture output.\n", encoding="utf-8"
            )
            input_args = ["--brief", str(brief)]
        argv = [
            str(SCRIPT),
            "--cwd",
            str(self.repository.path),
            *input_args,
            "--engine",
            engine,
            "--heartbeat-seconds",
            "0",
            "--out-dir",
            str(out_dir),
        ]
        if expect_changes:
            argv.append("--expect-changes")
        if model:
            argv.extend(["--model", model])
        if variant:
            argv.extend(["--variant", variant])
        if allow_path:
            argv.extend(["--allow-path", allow_path])
        if track_path:
            argv.extend(["--track-path", track_path])
        if session:
            argv.extend(["--session", session])
        if retry_read_only:
            argv.extend(["--retry-read-only", str(retry_read_only)])
        for verification_command in verify_commands or []:
            argv.extend(["--verify-command", verification_command])
        argv.extend(["--verify-timeout", verify_timeout])
        argv.extend(extra_args or [])
        environment = {
            "FAKE_CAPTURE": str(capture_path),
            "FAKE_COMMIT": "1" if fake_commit else "0",
            "FAKE_MALFORMED": "1" if malformed else "0",
            "FAKE_PARTIAL_REPORT": "1" if partial_report else "0",
            "FAKE_REPORT_STATUS": report_status,
            "FAKE_NO_CHANGE": "1" if no_change else "0",
            "FAKE_OUTPUT": fake_output,
            "FAKE_TRANSIENT_FAILURES": str(transient_failures),
            "FAKE_TRANSIENT_COUNT_PATH": str(
                self.external_path / f"transient-count-{self.run_index}.txt"
            ),
            "XDG_CACHE_HOME": str(self.external_path / f"cache-{self.run_index}"),
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
        }
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, environment),
            contextlib.redirect_stdout(output),
        ):
            exit_code = RUNNER.main()
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        capture = (
            json.loads(capture_path.read_text(encoding="utf-8"))
            if capture_path.exists()
            else {}
        )
        return exit_code, result, out_dir, output.getvalue(), capture

    def test_codex_default_records_scoped_delta_and_uses_stdin(self) -> None:
        exit_code, result, out_dir, summary, capture = self.run_main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_outcome"], "complete")
        self.assertEqual(result["reported_outcome"], "complete")
        self.assertEqual(result["verification"]["status"], "not_requested")
        self.assertEqual(result["engine"], "codex")
        self.assertEqual(result["model"], "gpt-5.6-luna")
        self.assertEqual(result["effort"]["bound"], "max")
        self.assertIn('model_reasoning_effort="max"', result["command"])
        self.assertEqual(result["session_id"], "codex-thread-1")
        self.assertEqual(result["run_delta"]["changed_paths"], ["allowed/output.txt"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", result["command"])
        self.assertIn("EXECUTION AND REPORT CONTRACT", capture["stdin"])
        self.assertNotIn(capture["stdin"], result["command"])
        self.assertIn("AGENT_STATUS=completed", summary)
        self.assertIn("AGENT_OUTCOME=complete", summary)
        self.assertIn("AGENT_VERIFICATION_STATUS=not_requested", summary)
        self.assertIn("AGENT_LIFECYCLE=executor_started", summary)
        self.assertIn("AGENT_LIFECYCLE=executor_finished", summary)
        self.assertIn("AGENT_REVIEW=", summary)
        self.assertTrue(result["final_message"].startswith("### STATUS"))
        self.assertEqual(result["review"]["changed_file_count"], 1)
        self.assertEqual(result["review"]["files"][0]["added_lines"], 1)
        self.assertEqual(result["review"]["files"][0]["removed_lines"], 0)
        self.assertEqual(stat.S_IMODE(out_dir.stat().st_mode), 0o700)
        for artifact in out_dir.iterdir():
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    def test_explicit_effort_overrides_preferred_luna_effort(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            extra_args=["--effort", "low"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["effort"]["bound"], "low")

    def test_model_catalog_uses_cache_and_force_refreshes(self) -> None:
        count_path = self.external_path / "catalog-count.txt"
        cache_root = self.external_path / "catalog-cache"
        environment = {
            "FAKE_CATALOG_COUNT": str(count_path),
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(cache_root),
        }
        with mock.patch.dict(os.environ, environment):
            first = RUNNER.discover_engine_catalog("agy")
            second = RUNNER.discover_engine_catalog("agy")
            refreshed = RUNNER.discover_engine_catalog("agy", refresh=True)

        self.assertEqual(first["cache"]["status"], "miss")
        self.assertEqual(second["cache"]["status"], "hit")
        self.assertEqual(refreshed["cache"]["status"], "refreshed")
        self.assertEqual(count_path.read_text(encoding="utf-8").splitlines(), ["agy", "agy"])
        cache_path = cache_root / "agent-executor" / "models-v1.json"
        self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)

    def test_requested_model_miss_refreshes_a_cached_catalog_once(self) -> None:
        count_path = self.external_path / "miss-refresh-count.txt"
        cache_root = self.external_path / "miss-refresh-cache"
        environment = {
            "FAKE_AGY_MODELS": "gemini-3.6-flash-high",
            "FAKE_CATALOG_COUNT": str(count_path),
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(cache_root),
        }
        with mock.patch.dict(os.environ, environment):
            RUNNER.discover_engine_catalog("agy")
            with mock.patch.dict(
                os.environ,
                {"FAKE_AGY_MODELS": "gemini-3.6-flash-high\ngemini-new"},
            ):
                (
                    _executable,
                    _version,
                    _available,
                    model,
                    selection,
                    catalog,
                ) = RUNNER.executor_preflight("agy", "gemini-new")

        self.assertEqual(model, "gemini-new")
        self.assertEqual(selection, "exact_user_request")
        self.assertEqual(catalog["cache"]["status"], "refreshed")
        self.assertEqual(
            count_path.read_text(encoding="utf-8").splitlines(), ["agy", "agy"]
        )

    def test_cli_version_change_invalidates_cached_catalog(self) -> None:
        count_path = self.external_path / "version-count.txt"
        cache_root = self.external_path / "version-cache"
        environment = {
            "FAKE_AGY_VERSION": "agy test 1.0",
            "FAKE_CATALOG_COUNT": str(count_path),
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(cache_root),
        }
        with mock.patch.dict(os.environ, environment):
            RUNNER.discover_engine_catalog("agy")
            with mock.patch.dict(os.environ, {"FAKE_AGY_VERSION": "agy test 2.0"}):
                changed = RUNNER.discover_engine_catalog("agy")

        self.assertEqual(changed["version"], "agy test 2.0")
        self.assertEqual(changed["cache"]["status"], "miss")
        self.assertEqual(
            count_path.read_text(encoding="utf-8").splitlines(), ["agy", "agy"]
        )

    def test_catalog_lists_codex_and_never_invokes_claude_for_discovery(self) -> None:
        cache_root = self.external_path / "table-cache"
        unsafe_call = self.external_path / "claude-unsafe.txt"
        environment = {
            "FAKE_CLAUDE_UNSAFE_CALL": str(unsafe_call),
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(cache_root),
        }
        with mock.patch.dict(os.environ, environment):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = RUNNER.models_main(
                    ["--engine", "codex", "--engine", "claude", "--format", "json"]
                )
        payload = json.loads(output.getvalue())
        codex, claude = payload["platforms"]
        self.assertEqual(exit_code, 0)
        self.assertIn(
            "gpt-5.6-luna",
            [model["id"] for model in codex["models"]],
        )
        self.assertNotIn("codex-hidden", [model["id"] for model in codex["models"]])
        # Claude has no model-list command: the catalog offers tier aliases without invoking Claude.
        self.assertEqual(claude["status"], "live")
        self.assertEqual([model["id"] for model in claude["models"]], ["opus", "sonnet", "haiku", "fable"])
        self.assertFalse(unsafe_call.exists())

    def test_catalog_filters_by_engine_and_model_substring(self) -> None:
        environment = {
            "FAKE_AGY_MODELS": (
                "gemini-3.6-flash-high\n"
                "gemini-3.6-flash-low\n"
                "claude-sonnet-4-6"
            ),
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(self.external_path / "match-cache"),
        }
        with mock.patch.dict(os.environ, environment):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = RUNNER.models_main(
                    [
                        "--engine",
                        "agy",
                        "--match",
                        "gemini-3.6-flash",
                        "--match",
                        "high",
                        "--format",
                        "json",
                    ]
                )
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload["platforms"]), 1)
        self.assertEqual(
            [model["id"] for model in payload["platforms"][0]["models"]],
            ["gemini-3.6-flash-high"],
        )

    def test_agy_uses_current_model_id_and_redacts_brief_preview(self) -> None:
        exit_code, result, _out_dir, _summary, capture = self.run_main(engine="agy")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["model"], "gemini-3.8-flash-medium")
        self.assertEqual(result["session_id"], "11111111-1111-1111-1111-111111111111")
        self.assertIn("--dangerously-skip-permissions", result["command"])
        self.assertIn("--print=<brief omitted>", result["command"])
        self.assertEqual(capture["term"], "dumb")
        self.assertEqual(capture["no_color"], "1")

    def test_opencode_glm_uses_high_variant_and_nonsharing_full_permissions(
        self,
    ) -> None:
        exit_code, result, _out_dir, _summary, capture = self.run_main(
            engine="opencode"
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["model"], "openrouter/z-ai/glm-5.3-flash")
        self.assertEqual(result["variant"], "high")
        self.assertEqual(result["session_id"], "ses_opencode_1")
        self.assertEqual(capture["permission"], json.dumps("allow"))
        self.assertEqual(capture["auto_share"], "false")
        self.assertEqual(capture["disable_autoupdate"], "true")
        runtime_config = json.loads(capture["config_content"])
        self.assertEqual(runtime_config["permission"], "allow")
        self.assertEqual(runtime_config["agent"]["build"]["permission"], "allow")
        self.assertEqual(runtime_config["share"], "disabled")
        self.assertIn("--auto", result["command"])
        self.assertIn("--pure", result["command"])
        self.assertNotIn("--share", result["command"])
        self.assertLess(
            result["command"].index(
                "Execute the attached execution brief exactly and return its required report."
            ),
            result["command"].index("--file"),
        )

    def test_opencode_deepseek_profile_is_selectable_at_high(self) -> None:
        model = "openrouter/deepseek/deepseek-v4-flash"
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            engine="opencode", model=model, variant="high"
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["model"], model)
        self.assertEqual(result["variant"], "high")

    def test_exact_sessions_are_forwarded(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            session="codex-thread-existing"
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            result["command"][1:4], ["exec", "resume", "codex-thread-existing"]
        )

        self.repository.git("clean", "-fd")
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            engine="opencode", session="ses_existing"
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("ses_existing", result["command"])

    def test_scope_and_history_violations_override_success(self) -> None:
        exit_code, result, _out_dir, summary, _capture = self.run_main(
            fake_output="outside.txt"
        )
        self.assertEqual(exit_code, 22)
        self.assertEqual(result["status"], "scope_violation")
        self.assertEqual(result["scope_violations"], ["outside.txt"])
        self.assertIn('AGENT_SCOPE_VIOLATIONS=["outside.txt"]', summary)

        self.repository.git("clean", "-fd")
        exit_code, result, _out_dir, summary, _capture = self.run_main(fake_commit=True)
        self.assertEqual(exit_code, 21)
        self.assertEqual(result["status"], "history_violation")
        self.assertIn("head", result["history_violations"])
        self.assertIn("AGENT_HISTORY_VIOLATIONS=", summary)

    def test_tracks_ignored_deliverable(self) -> None:
        tracked_path = "plans/feedback.md"
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            fake_output=tracked_path,
            allow_path=None,
            track_path=tracked_path,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["run_delta"]["changed_paths"], [tracked_path])
        self.assertEqual(result["run_delta"]["tracked_paths_changed"], [tracked_path])

    def test_malformed_report_and_missing_expected_change_fail(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(malformed=True)
        self.assertEqual(exit_code, 23)
        self.assertEqual(result["status"], "malformed_report")

        self.repository.git("clean", "-fd")
        exit_code, result, _out_dir, _summary, _capture = self.run_main(no_change=True)
        self.assertEqual(exit_code, 20)
        self.assertEqual(result["status"], "no_changes")

    def test_reported_blocker_skips_runner_verification(self) -> None:
        marker = self.external_path / "verification-should-not-run"
        exit_code, result, _out_dir, summary, _capture = self.run_main(
            report_status="BLOCKER (needs a decision)",
            verify_commands=[f"touch {marker}"],
        )
        self.assertEqual(exit_code, 24)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["task_outcome"], "blocked")
        self.assertEqual(result["reported_outcome"], "blocked")
        self.assertEqual(result["verification"]["status"], "skipped")
        self.assertEqual(
            result["verification"]["skipped_reason"],
            "runner_status:blocked",
        )
        self.assertFalse(marker.exists())
        self.assertIn("AGENT_OUTCOME=blocked", summary)

    def test_unknown_reported_status_is_malformed(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            report_status="Mostly complete",
        )
        self.assertEqual(exit_code, 23)
        self.assertEqual(result["status"], "malformed_report")
        self.assertEqual(result["reported_outcome"], "unknown")

    def test_incomplete_structured_report_is_malformed(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            partial_report=True,
        )
        self.assertEqual(exit_code, 23)
        self.assertEqual(result["status"], "malformed_report")
        self.assertFalse(result["report_contract_valid"])
        self.assertIn(
            "missing_heading:FILES CHANGED",
            result["report_contract_violations"],
        )

    def test_runner_owned_verification_passes_and_captures_private_logs(
        self,
    ) -> None:
        exit_code, result, out_dir, _summary, _capture = self.run_main(
            verify_commands=[
                "test -f allowed/output.txt",
                "printf 'runner verification passed\\n'",
            ],
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(result["verification"]["completed_count"], 2)
        second = result["verification"]["commands"][1]
        self.assertEqual(second["stdout_tail"], ["runner verification passed"])
        self.assertEqual(
            stat.S_IMODE(Path(second["stdout_path"]).stat().st_mode),
            0o600,
        )
        self.assertFalse(result["verification_changed_workspace"])
        self.assertEqual(
            result["verification_delta"],
            {"changed_paths": [], "tracked_paths_changed": []},
        )
        self.assertIn(out_dir.resolve(), Path(second["stdout_path"]).parents)

    def test_runner_owned_verification_fails_fast(self) -> None:
        exit_code, result, _out_dir, summary, _capture = self.run_main(
            verify_commands=[
                "printf 'verification failed\\n' >&2; exit 7",
                "exit 0",
            ],
        )
        self.assertEqual(exit_code, 25)
        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(result["task_outcome"], "failed")
        self.assertEqual(result["verification"]["status"], "failed")
        self.assertEqual(result["verification"]["completed_count"], 1)
        first = result["verification"]["commands"][0]
        self.assertEqual(first["exit_code"], 7)
        self.assertEqual(first["stderr_tail"], ["verification failed"])
        self.assertIn("AGENT_VERIFICATION_STATUS=failed", summary)

    def test_runner_owned_verification_times_out(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            verify_commands=["sleep 1"],
            verify_timeout="0.05s",
        )
        self.assertEqual(exit_code, 25)
        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(result["verification"]["status"], "timed_out")
        self.assertEqual(
            result["verification"]["commands"][0]["status"],
            "timed_out",
        )

    def test_runner_owned_verification_may_not_mutate_the_worktree(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            verify_commands=[
                "printf 'verification mutation\\n' > allowed/output.txt"
            ],
        )
        self.assertEqual(exit_code, 26)
        self.assertEqual(result["status"], "verification_mutation")
        self.assertTrue(result["verification_changed_workspace"])
        self.assertEqual(
            result["verification_delta"]["changed_paths"],
            ["allowed/output.txt"],
        )

    def test_read_only_run_may_complete_without_changes(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            no_change=True, expect_changes=False, allow_path=None
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "completed")

    def test_read_only_run_fails_if_executor_changes_a_path(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            expect_changes=False, allow_path=None
        )
        self.assertEqual(exit_code, 22)
        self.assertEqual(result["status"], "scope_violation")
        self.assertEqual(result["scope_violations"], ["allowed/output.txt"])

    def test_task_spec_is_the_authoritative_mutating_scope(self) -> None:
        task_spec = {
            "objective": "Create the bounded fixture output.",
            "repository_root": str(self.repository.path),
            "current_state": "The fixture repository accepts the focused output.",
            "pre_existing_changes": [],
            "plan": ["Create the output in the allowed directory."],
            "allow_paths": ["allowed/"],
            "track_paths": [],
            "development_checks": [],
            "acceptance_criteria": ["The output exists under allowed/ only."],
        }
        exit_code, result, _out_dir, _summary, _capture = self.run_main(
            task_spec=task_spec,
            allow_path=None,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_input"], "task_spec")
        self.assertEqual(result["allowed_paths"], ["allowed"])
        self.assertEqual(result["tracked_paths"], [])

    def test_task_spec_rejects_scope_flags_and_root_mismatch(self) -> None:
        spec = {
            "objective": "Read the fixture.",
            "repository_root": str(self.repository.path),
            "current_state": "The fixture exists.",
            "pre_existing_changes": [],
            "plan": ["Inspect the fixture."],
            "allow_paths": [],
            "track_paths": [],
            "development_checks": [],
            "acceptance_criteria": ["No mutation occurs."],
        }
        spec_path = self.external_path / "rejected-scope-spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(RUNNER.RunnerError, "derives scope"):
            RUNNER.resolve_execution_brief(
                RUNNER.parse_args(
                    [
                        "--task-spec",
                        str(spec_path),
                        "--allow-path",
                        "allowed",
                    ]
                ),
                repo=self.repository.path,
            )

        spec["repository_root"] = str(self.external_path)
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(RUNNER.RunnerError, "exactly match --cwd"):
            RUNNER.resolve_execution_brief(
                RUNNER.parse_args(["--task-spec", str(spec_path)]),
                repo=self.repository.path,
            )

    def test_read_only_transient_failure_retries_without_relaxing_scope(self) -> None:
        exit_code, result, out_dir, _summary, _capture = self.run_main(
            no_change=True,
            expect_changes=False,
            allow_path=None,
            retry_read_only=1,
            transient_failures=1,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["executor_attempts"]), 2)
        first, second = result["executor_attempts"]
        self.assertTrue(first["retrying"])
        self.assertTrue(first["transient_provider_failure"])
        self.assertFalse(first["workspace_changed"])
        self.assertTrue(Path(first["stderr_path"]).exists())
        self.assertFalse(second["retrying"])
        self.assertEqual(Path(second["stdout_path"]).resolve(), (out_dir / "stdout.txt").resolve())

    def test_retry_read_only_rejects_a_mutating_scope(self) -> None:
        args = RUNNER.parse_args(
            [
                "--brief",
                "brief.md",
                "--allow-path",
                "allowed",
                "--retry-read-only",
                "1",
            ]
        )
        with self.assertRaisesRegex(RUNNER.RunnerError, "requires a read-only run"):
            RUNNER.validated_run_options(args)

    def test_unavailable_opencode_model_fails_preflight(self) -> None:
        exit_code, result, _out_dir, _summary, capture = self.run_main(
            engine="opencode", model="openrouter/missing/model"
        )
        self.assertEqual(exit_code, 14)
        self.assertEqual(result["status"], "preflight_failed")
        self.assertEqual(capture, {})

    def test_unavailable_codex_model_fails_before_executor_invocation(self) -> None:
        exit_code, result, _out_dir, _summary, capture = self.run_main(
            model="gpt-model-that-does-not-exist"
        )
        self.assertEqual(exit_code, 14)
        self.assertEqual(result["status"], "preflight_failed")
        self.assertEqual(capture, {})

    def test_detached_mode_returns_without_polling_and_writes_result(self) -> None:
        brief = self.external_path / "detached-brief.md"
        brief.write_text("# Objective\n\nCreate the fixture output.\n", encoding="utf-8")
        capture_path = self.external_path / "detached-capture.json"
        cache_root = self.external_path / "detached-cache"
        environment = {
            "FAKE_CAPTURE": str(capture_path),
            "FAKE_OUTPUT": "allowed/output.txt",
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(cache_root),
        }
        argv = [
            str(SCRIPT),
            "--cwd",
            str(self.repository.path),
            "--brief",
            str(brief),
            "--detach",
            "--notify",
            "none",
            "--allow-path",
            "allowed",
            "--expect-changes",
            "--verify-command",
            "test -f allowed/output.txt",
        ]
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, environment),
            contextlib.redirect_stdout(output),
        ):
            exit_code = RUNNER.main()
        result_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("AGENT_RESULT=")
        )
        result_path = Path(result_line.partition("=")[2])
        job_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("AGENT_JOB=")
        )
        job_path = Path(job_line.partition("=")[2])
        event_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("AGENT_EVENT=")
        )
        event_path = Path(event_line.partition("=")[2])
        event_id_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("AGENT_EVENT_ID=")
        )
        for _attempt in range(100):
            if result_path.exists() and event_path.exists():
                break
            time.sleep(0.05)
        self.assertEqual(exit_code, 0)
        self.assertTrue(result_path.exists())
        self.assertTrue(event_path.exists())
        self.assertIn(cache_root.resolve(), job_path.parents)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_outcome"], "complete")
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["task_outcome"], "complete")
        self.assertEqual(event["verification_status"], "passed")
        self.assertEqual(event["result_path"], str(result_path))
        self.assertEqual(event["deliveries"], [])
        self.assertEqual(event_id_line.partition("=")[2], event_path.stem)
        self.assertNotIn("AGENT_PROGRESS", output.getvalue())

    def test_detached_task_spec_preserves_derived_scope(self) -> None:
        task_spec = self.external_path / "detached-task-spec.json"
        task_spec.write_text(
            json.dumps(
                {
                    "objective": "Create the fixture output.",
                    "repository_root": str(self.repository.path),
                    "current_state": "The output directory is available.",
                    "pre_existing_changes": [],
                    "plan": ["Create the fixture output."],
                    "allow_paths": ["allowed"],
                    "track_paths": [],
                    "development_checks": [],
                    "acceptance_criteria": ["The output is in allowed/ only."],
                }
            ),
            encoding="utf-8",
        )
        cache_root = self.external_path / "detached-task-spec-cache"
        environment = {
            "FAKE_OUTPUT": "allowed/output.txt",
            "FAKE_CAPTURE": str(self.external_path / "detached-task-spec-capture.json"),
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(cache_root),
        }
        argv = [
            str(SCRIPT),
            "--cwd",
            str(self.repository.path),
            "--task-spec",
            str(task_spec),
            "--detach",
            "--notify",
            "none",
            "--expect-changes",
        ]
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, environment),
            contextlib.redirect_stdout(output),
        ):
            exit_code = RUNNER.main()
        result_path = Path(
            next(
                line for line in output.getvalue().splitlines() if line.startswith("AGENT_RESULT=")
            ).partition("=")[2]
        )
        for _attempt in range(100):
            if result_path.exists():
                break
            time.sleep(0.05)
        self.assertEqual(exit_code, 0)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_input"], "task_spec")
        self.assertEqual(result["allowed_paths"], ["allowed"])

    def test_rejects_incompatible_variant_and_opencode_add_dir(self) -> None:
        brief = self.external_path / "invalid-brief.md"
        brief.write_text("# Objective\n\nNothing.\n", encoding="utf-8")
        with (
            mock.patch.object(
                sys,
                "argv",
                [str(SCRIPT), "--brief", str(brief), "--variant", "high"],
            ),
            self.assertRaises(RUNNER.RunnerError),
        ):
            RUNNER.main()

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    str(SCRIPT),
                    "--cwd",
                    str(self.repository.path),
                    "--brief",
                    str(brief),
                    "--engine",
                    "opencode",
                    "--add-dir",
                    str(self.external_path),
                ],
            ),
            self.assertRaises(RUNNER.RunnerError),
        ):
            RUNNER.main()


class NativeRouteAndProviderErrorTests(unittest.TestCase):
    # Reuse the integration fixtures without re-running the inherited tests.
    setUp = RunnerIntegrationTests.setUp
    tearDown = RunnerIntegrationTests.tearDown
    _write_executable = RunnerIntegrationTests._write_executable
    _write_fake_executors = RunnerIntegrationTests._write_fake_executors
    run_main = RunnerIntegrationTests.run_main

    def test_hosted_families_never_route_through_opencode(self) -> None:
        cases = {
            "openrouter/anthropic/claude-opus-4.6": "claude", "gitlab/duo-chat-opus-4-6": "claude",
            "gitlab/duo-chat-sonnet-4-5": "claude", "openrouter/openai/gpt-5.6": "codex",
            "gitlab/duo-chat-gpt-5": "codex", "openrouter/google/gemini-3.1-pro": "agy",
            "openrouter/z-ai/glm-5.3-flash": None, "openrouter/deepseek/deepseek-v4-flash": None,
            "openrouter/qwen/qwen3-coder": None, "~moonshotai/kimi-latest": None,
            "openrouter/openai/gpt-oss-120b": None, "openrouter/google/gemma-3-27b": None,
        }
        self.assertEqual({model: RUNNER.native_engine_for(model) for model in cases}, cases)

    def test_opencode_refuses_a_claude_model_before_any_provider_call(self) -> None:
        exit_code, result, _out, _summary, _capture = self.run_main(engine="opencode", model="openrouter/anthropic/claude-opus-4.6")
        self.assertEqual(exit_code, 17)
        self.assertIn("--engine claude", result["error"])

    def test_claude_engine_runs_claude_code_with_the_brief_on_stdin_and_no_host_session(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "host-secret", "CLAUDE_CONFIG_DIR": "/host/profile"}):
            exit_code, result, _out, _summary, capture = self.run_main(engine="claude", model="opus", extra_args=["--effort", "high"])
        self.assertEqual(exit_code, 0, result.get("error"))
        self.assertEqual(result["session_id"], "claude-session-1")
        self.assertIn("EXECUTION AND REPORT CONTRACT", capture["stdin"])
        self.assertIn("bypassPermissions", capture["argv"])
        self.assertEqual(capture["argv"][capture["argv"].index("--effort") + 1], "high")
        self.assertEqual((capture["claudecode"], capture["claude_code_token"], capture["claude_config_dir"]), (None, None, None))
        self.assertEqual(result["usage"]["totals"], {"fresh_input": 10, "cache_read": 200, "cache_write": 30, "output": 40})

    def test_agy_quota_error_is_reported_as_provider_quota_exhausted(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_AGY_STATUS": "error", "FAKE_AGY_ERROR": "RESOURCE_EXHAUSTED (code 429): quota"}):
            exit_code, result, _out, _summary, _capture = self.run_main(engine="agy")
        self.assertEqual((result["status"], exit_code), ("provider_quota_exhausted", 16))
        self.assertIn("RESOURCE_EXHAUSTED", result["provider_error"])


if __name__ == "__main__":
    unittest.main()
