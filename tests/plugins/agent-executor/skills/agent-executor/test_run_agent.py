from __future__ import annotations

import argparse
import contextlib
import hashlib
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

# Tests mirror the repository layout under tests/, so the skill under test sits at the same
# relative path below plugins/.
SKILL_DIR = Path(__file__).resolve().parents[5] / "plugins" / "agent-executor" / "skills" / "agent-executor"
SCRIPT = SKILL_DIR / "scripts" / "run_agent.py"
FIXTURES = Path(__file__).parent / "fixtures"
# Runner tests must never query real subscription accounts through ai-usage; test_quota.py sets
# its own fake per test.
os.environ["AI_USAGE_BIN"] = str(Path(__file__).parent / "no-ai-usage")
# Routes and families come from the bundled defaults only, never the developer's own config.
os.environ["AGENT_EXECUTOR_CONFIG"] = str(Path(tempfile.mkdtemp()) / "absent-config.json")
SPEC = importlib.util.spec_from_file_location("agent_executor_runner", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
MAILBOX = RUNNER.bundled_module("peer_mailbox")


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
            capture_output=True,
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
            RUNNER.normalize_repo_paths(["src/jobs", "src/jobs", "README.md"], option="--allow-path"),
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
            with self.subTest(invalid=invalid), self.assertRaises(RUNNER.RunnerError):
                RUNNER.normalize_repo_paths([invalid], option="--track-path")


class RunMessageEventTests(unittest.TestCase):
    def test_events_wait_messages_returns_the_run_mailbox_without_marking_read(self):
        with tempfile.TemporaryDirectory(prefix="agent-run-message-event-") as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}):
                event_path = RUNNER.prepare_completion_event_path()
                result_path = root / "result.json"
                info = MAILBOX.open_run(
                    MAILBOX.root(),
                    event_path.stem,
                    worker_harness="claude",
                    repository=root,
                    result_path=result_path,
                    deadline=(RUNNER.dt.datetime.now(RUNNER.dt.UTC) + RUNNER.dt.timedelta(minutes=5)).isoformat(),
                    worker_pid=None,
                )
                worker = MAILBOX.load(MAILBOX.peer_path(MAILBOX.root(), info["worker"]))
                question = MAILBOX.send(
                    MAILBOX.root(),
                    sender=worker,
                    to=info["conductor"],
                    text="Need the target version.",
                    kind="question",
                    client_id="version-question",
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        RUNNER.events_main(
                            ["wait", str(event_path), "--messages", "--timeout", "1s", "--poll-seconds", ".01"]
                        ),
                        0,
                    )

                signal = json.loads(output.getvalue())
                self.assertEqual(signal["schema"], "agent-executor.message-signal.v1")
                self.assertEqual(signal["mailbox"], info["conductor"])
                self.assertEqual(signal["messages"][0]["id"], question["id"])
                self.assertIsNone(signal["run_status"])
                self.assertIsNone(MAILBOX.messages(MAILBOX.root(), info["conductor"])[0]["readAt"])


class RemovedMessagesCommandTests(unittest.TestCase):
    def test_messages_command_points_to_mailbox_and_exits_with_code_four(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "messages", "send"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stderr.strip(), "run_agent.py messages was removed; use run_agent.py mailbox.")

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
        self.assertEqual(RUNNER.state_delta(before, after)["changed_paths"], ["tracked.txt"])

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
        self.assertEqual(RUNNER.state_delta(before, after)["changed_paths"], ["tracked.txt"])

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
        self.assertIn("branch", RUNNER.find_history_violations(before_branch, after_branch))

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
        exact = "# Final response\n\nSTATUS\nFILES CHANGED\nCOMMANDS RUN\nVERIFICATION\nRISKS OR BLOCKERS\n"
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

            signals = [json.loads(line) for line in output.getvalue().splitlines()]
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

            signals = [json.loads(line) for line in output.getvalue().splitlines()]
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
import time

def block_until_released(announce=None):
    """A long-running fresh run: expose a session (unless silent), then work until released or interrupted."""
    if os.environ.get("FAKE_BLOCK") != "1":
        return
    if announce is not None and os.environ.get("FAKE_BLOCK_SILENT") != "1":
        announce()
        sys.stdout.flush()
    pathlib.Path(os.environ["FAKE_STARTED"]).write_text("started", encoding="utf-8")
    release = pathlib.Path(os.environ["FAKE_RELEASE"])
    while not release.exists():
        time.sleep(0.02)

def lease_held():
    """Whether another process could take the worktree lease right now (a resumed run inherits it)."""
    path = os.environ.get("FAKE_LEASE_PROBE")
    if not path:
        return None
    import fcntl
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False

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
    if sys.argv[1:3] == ["exec", "resume"]:
        time.sleep(float(os.environ.get("FAKE_RESUME_SLEEP", "0")))
    else:
        block_until_released(lambda: print(json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"})))
    if transient_failure():
        raise SystemExit(75)
    mutate()
    prompt = sys.stdin.read()
    output_index = sys.argv.index("--output-last-message") + 1
    pathlib.Path(sys.argv[output_index]).write_text(report(), encoding="utf-8")
    capture({"stdin": prompt, "lease_held": lease_held()})
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
    if "--conversation" not in sys.argv:
        block_until_released(lambda: print(json.dumps({"type": "init", "conversation_id": "11111111-1111-1111-1111-111111111111", "model": sys.argv[sys.argv.index("--model") + 1]})))
    if transient_failure():
        raise SystemExit(75)
    mutate()
    log_index = sys.argv.index("--log-file") + 1
    pathlib.Path(sys.argv[log_index]).write_text(
        "Created conversation 11111111-1111-1111-1111-111111111111\n",
        encoding="utf-8",
    )
    stream_input = None
    if "--input-format" in sys.argv:
        stream_input = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    capture(
        {
            "term": os.environ.get("TERM"),
            "no_color": os.environ.get("NO_COLOR"),
            "stream_input": stream_input,
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
if "--pure" in sys.argv and (os.environ.get("FAKE_OPENCODE_VERSION", "").startswith("2.") or os.environ.get("FAKE_OPENCODE_REJECT_PURE") == "1"):
    print("Unrecognized flag: --pure", file=sys.stderr)
    sys.exit(1)
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
            "openrouter/~z-ai/glm-flash-latest\nopenrouter/z-ai/glm-5.3-flash\nopenrouter/deepseek/deepseek-v4-flash",
        )
    )
else:
    if "--session" not in sys.argv:
        block_until_released(lambda: print(json.dumps({"type": "step_start", "sessionID": "ses_opencode_1", "part": {"type": "step-start"}})))
    if transient_failure():
        raise SystemExit(75)
    mutate()
    capture(
        {
            "argv": sys.argv[1:],
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
    # An execution run: the brief arrives on stdin. Like Claude Code's json output, a blocked run prints nothing.
    prompt = sys.stdin.read()
    resumed = "--resume" in sys.argv
    if not resumed:
        block_until_released()
    mutate()
    capture({"stdin": prompt, "argv": sys.argv[1:], "claudecode": os.environ.get("CLAUDECODE"),
             "claude_code_token": os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN"),
             "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR")})
    error = os.environ.get("FAKE_CLAUDE_ERROR")
    if os.environ.get("FAKE_CLAUDE_STDERR"):
        print(os.environ["FAKE_CLAUDE_STDERR"], file=sys.stderr)
    if os.environ.get("FAKE_CLAUDE_STDOUT_FILE"):
        # A captured Claude Code stdout, replayed verbatim.
        sys.stdout.write(pathlib.Path(os.environ["FAKE_CLAUDE_STDOUT_FILE"]).read_text(encoding="utf-8"))
    else:
        session = sys.argv[sys.argv.index("--resume") + 1] if resumed else "claude-session-1"
        print(json.dumps({"type": "result", "subtype": "success", "is_error": bool(error), "session_id": session,
                          "result": error or report(), "total_cost_usd": 0.25,
                          "usage": {"input_tokens": 10, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 30, "output_tokens": 40}}))
    raise SystemExit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
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
            brief.write_text("# Objective\n\nCreate the fixture output.\n", encoding="utf-8")
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
            "FAKE_TRANSIENT_COUNT_PATH": str(self.external_path / f"transient-count-{self.run_index}.txt"),
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
        capture = json.loads(capture_path.read_text(encoding="utf-8")) if capture_path.exists() else {}
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
        exit_code, result, _out_dir, _summary, _capture = self.run_main(extra_args=["--effort", "low"])
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
        self.assertEqual(count_path.read_text(encoding="utf-8").splitlines(), ["agy", "agy"])

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
        self.assertEqual(count_path.read_text(encoding="utf-8").splitlines(), ["agy", "agy"])

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
                exit_code = RUNNER.models_main(["--engine", "codex", "--engine", "claude", "--format", "json"])
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
            "FAKE_AGY_MODELS": ("gemini-3.6-flash-high\ngemini-3.6-flash-low\nclaude-sonnet-4-6"),
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

    def test_current_agy_reads_the_brief_from_stdin(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_AGY_VERSION": "agy 1.2.11"}):
            exit_code, result, _out_dir, _summary, capture = self.run_main(engine="agy")
        self.assertEqual(exit_code, 0)
        self.assertIn("--input-format", result["command"])
        self.assertNotIn("--print=<brief omitted>", result["command"])
        [message] = capture["stream_input"]
        self.assertEqual(message["event"], "user")
        self.assertIn("EXECUTION AND REPORT CONTRACT", message["message"]["content"])

    def test_opencode_glm_uses_high_variant_and_nonsharing_full_permissions(
        self,
    ) -> None:
        exit_code, result, _out_dir, _summary, capture = self.run_main(engine="opencode")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["model"], "openrouter/~z-ai/glm-flash-latest")
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
            result["command"].index("Execute the attached execution brief exactly and return its required report."),
            result["command"].index("--file"),
        )

    def test_opencode_deepseek_profile_is_selectable_at_high(self) -> None:
        model = "openrouter/deepseek/deepseek-v4-flash"
        exit_code, result, _out_dir, _summary, _capture = self.run_main(engine="opencode", model=model, variant="high")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["model"], model)
        self.assertEqual(result["variant"], "high")

    def test_opencode_v2_command_flags(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_OPENCODE_VERSION": "opencode v2.0.20"}):
            exit_code, result, _out_dir, _summary, _capture = self.run_main(engine="opencode")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["model"], "openrouter/~z-ai/glm-flash-latest")
        self.assertEqual(result["variant"], "high")
        self.assertIn("--auto", result["command"])
        self.assertNotIn("--pure", result["command"])
        self.assertNotIn("--variant", result["command"])
        self.assertNotIn("--dir", result["command"])
        self.assertIn("openrouter/~z-ai/glm-flash-latest#high", result["command"])
        self.assertIn("--file", result["command"])

    def test_opencode_caches_version_and_switches_when_upgraded_to_v2(self) -> None:
        cache_root = self.external_path / "opencode-cache"
        env_v1 = {
            "FAKE_OPENCODE_VERSION": "1.18.4-test",
            "XDG_CACHE_HOME": str(cache_root),
        }
        with mock.patch.dict(os.environ, env_v1):
            exit_code, result1, _out1, _sum1, _cap1 = self.run_main(engine="opencode")
            self.assertEqual(exit_code, 0)
            self.assertIn("--pure", result1["command"])

        # Simulate user upgrading opencode to v2
        self.repository.git("clean", "-fd")
        env_v2 = {
            "FAKE_OPENCODE_VERSION": "2.0.20",
            "XDG_CACHE_HOME": str(cache_root),
        }
        with mock.patch.dict(os.environ, env_v2):
            exit_code, result2, _out2, _sum2, _cap2 = self.run_main(engine="opencode")
            self.assertEqual(exit_code, 0)
            self.assertNotIn("--pure", result2["command"])
            self.assertIn("openrouter/~z-ai/glm-flash-latest#high", result2["command"])

    def test_exact_sessions_are_forwarded(self) -> None:
        exit_code, result, _out_dir, _summary, _capture = self.run_main(session="codex-thread-existing")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["command"][1:4], ["exec", "resume", "codex-thread-existing"])

        self.repository.git("clean", "-fd")
        exit_code, result, _out_dir, _summary, _capture = self.run_main(engine="opencode", session="ses_existing")
        self.assertEqual(exit_code, 0)
        self.assertIn("ses_existing", result["command"])

    def test_scope_and_history_violations_override_success(self) -> None:
        exit_code, result, _out_dir, summary, _capture = self.run_main(fake_output="outside.txt")
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
            verify_commands=["printf 'verification mutation\\n' > allowed/output.txt"],
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
        exit_code, result, _out_dir, _summary, _capture = self.run_main(expect_changes=False, allow_path=None)
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
        exit_code, result, _out_dir, _summary, capture = self.run_main(model="gpt-model-that-does-not-exist")
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
        result_line = next(line for line in output.getvalue().splitlines() if line.startswith("AGENT_RESULT="))
        result_path = Path(result_line.partition("=")[2])
        job_line = next(line for line in output.getvalue().splitlines() if line.startswith("AGENT_JOB="))
        job_path = Path(job_line.partition("=")[2])
        event_line = next(line for line in output.getvalue().splitlines() if line.startswith("AGENT_EVENT="))
        event_path = Path(event_line.partition("=")[2])
        event_id_line = next(line for line in output.getvalue().splitlines() if line.startswith("AGENT_EVENT_ID="))
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

    def test_detached_launch_fails_fast_when_another_executor_holds_the_worktree(self) -> None:
        brief = self.external_path / "held-brief.md"
        brief.write_text("# Objective\n\nCreate the fixture output.\n", encoding="utf-8")
        cache_root = self.external_path / "held-cache"
        environment = {
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
        ]
        support = RUNNER.bundled_module("execution_support")
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, environment),
            contextlib.redirect_stdout(output),
        ):
            with support.workspace_lease(self.repository.path, RUNNER.default_agent_cache_dir()):
                with self.assertRaises(RUNNER.RunnerError) as caught:
                    RUNNER.main()
        self.assertEqual(caught.exception.exit_code, 27)
        self.assertNotIn("AGENT_JOB_STATUS=started", output.getvalue(), "no job may be reported as started")

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
            next(line for line in output.getvalue().splitlines() if line.startswith("AGENT_RESULT=")).partition("=")[2]
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


class RouteTests(unittest.TestCase):
    def resolve(self, *extra: str, config: dict | None = None) -> argparse.Namespace:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "config.json"
            if config is not None:
                path.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_EXECUTOR_CONFIG": str(path)}):
                return RUNNER.resolve_route(RUNNER.parse_args(["--brief", "b.md", *extra]))

    def test_defaults_and_roles(self) -> None:
        plain = self.resolve()
        self.assertEqual((plain.engine, plain.model, plain.effort), ("codex", None, "max"))
        self.assertEqual(RUNNER.preferred_model("codex"), "gpt:luna")
        consult = self.resolve("--route", "consultation")
        self.assertEqual((consult.engine, consult.model, consult.effort), ("codex", "gpt:sol", "high"))
        glm = self.resolve("--route", "opencode")
        self.assertEqual((glm.engine, glm.model, glm.variant), ("opencode", "glm:flash", "high"))
        claude = self.resolve("--route", "claude")
        self.assertEqual((claude.engine, claude.model, claude.effort), ("claude", "claude:best", "high"))

    def test_explicit_flags_win_and_config_overrides(self) -> None:
        self.assertEqual(self.resolve("--route", "consultation", "--effort", "low").effort, "low")
        self.assertIsNone(
            self.resolve("--model", "gpt-6-luna").effort, "the route's effort belongs to the route's model"
        )
        custom = self.resolve(
            "--route",
            "implementation",
            config={"routes": {"implementation": {"engine": "codex", "model": "gpt-6-luna", "effort": "high"}}},
        )
        self.assertEqual((custom.model, custom.effort), ("gpt-6-luna", "high"))
        with self.assertRaises(RUNNER.RunnerError) as caught:
            self.resolve("--route", "nonexistent")
        self.assertEqual(caught.exception.exit_code, 4)


class PruneTests(unittest.TestCase):
    def test_prune_removes_old_acknowledged_state_and_keeps_unreviewed_results(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"AGENT_EXECUTOR_HOME": home}):
            root = Path(home)
            old = time.time() - 40 * 86400

            def job(name: str) -> Path:
                directory = root / "jobs-v1" / name / "run"
                directory.mkdir(parents=True)
                (directory / "result.json").write_text("{}", encoding="utf-8")
                return directory / "result.json"

            def event(name: str, result: Path, acknowledged: bool) -> Path:
                path = root / "completions-v1" / f"{name}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"result_path": str(result), "acknowledged_at": "x" if acknowledged else None}),
                    encoding="utf-8",
                )
                return path

            reviewed, pending = job("reviewed"), job("pending")
            orphan_run = root / "runs-v1" / "old-foreground"
            orphan_run.mkdir(parents=True)
            paths = [
                event("reviewed", reviewed, True),
                event("pending", pending, False),
                root / "jobs-v1" / "reviewed",
                root / "jobs-v1" / "pending",
                orphan_run,
            ]
            for path in paths:
                os.utime(path, (old, old))
            dry = RUNNER.prune_state(14 * 86400, dry_run=True)
            self.assertEqual((dry["events"], dry["jobs"], dry["runs"]), (1, 1, 1))
            self.assertTrue((root / "jobs-v1" / "reviewed").exists(), "dry run removes nothing")
            RUNNER.prune_state(14 * 86400)
            self.assertFalse((root / "jobs-v1" / "reviewed").exists())
            self.assertFalse(orphan_run.exists())
            self.assertTrue((root / "jobs-v1" / "pending").exists(), "unacknowledged results are kept")
            self.assertTrue((root / "completions-v1" / "pending.json").exists())

    def test_durations_accept_days(self) -> None:
        self.assertEqual(RUNNER.parse_duration("14d", option="--older-than"), 14 * 86400)


class NativeRouteAndProviderErrorTests(unittest.TestCase):
    # Reuse the integration fixtures without re-running the inherited tests.
    setUp = RunnerIntegrationTests.setUp
    tearDown = RunnerIntegrationTests.tearDown
    _write_executable = RunnerIntegrationTests._write_executable
    _write_fake_executors = RunnerIntegrationTests._write_fake_executors
    run_main = RunnerIntegrationTests.run_main

    def test_hosted_families_never_route_through_opencode(self) -> None:
        cases = {
            "openrouter/anthropic/claude-opus-4.6": "claude",
            "gitlab/duo-chat-opus-4-6": "claude",
            "gitlab/duo-chat-sonnet-4-5": "claude",
            "openrouter/openai/gpt-5.6": "codex",
            "gitlab/duo-chat-gpt-5": "codex",
            "openrouter/google/gemini-3.1-pro": "agy",
            "openrouter/z-ai/glm-5.3-flash": None,
            "openrouter/deepseek/deepseek-v4-flash": None,
            "openrouter/qwen/qwen3-coder": None,
            "~moonshotai/kimi-latest": None,
            "openrouter/openai/gpt-oss-120b": None,
            "openrouter/google/gemma-3-27b": None,
        }
        self.assertEqual({model: RUNNER.native_engine_for(model) for model in cases}, cases)

    def test_opencode_refuses_a_claude_model_before_any_provider_call(self) -> None:
        exit_code, result, _out, _summary, _capture = self.run_main(
            engine="opencode", model="openrouter/anthropic/claude-opus-4.6"
        )
        self.assertEqual(exit_code, 17)
        self.assertIn("--engine claude", result["error"])

    def test_claude_engine_runs_claude_code_with_the_brief_on_stdin_and_no_host_session(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "host-secret", "CLAUDE_CONFIG_DIR": "/host/profile"},
        ):
            exit_code, result, _out, _summary, capture = self.run_main(
                engine="claude", model="opus", extra_args=["--effort", "high"]
            )
        self.assertEqual(exit_code, 0, result.get("error"))
        self.assertEqual(result["session_id"], "claude-session-1")
        self.assertIn("EXECUTION AND REPORT CONTRACT", capture["stdin"])
        self.assertIn("bypassPermissions", capture["argv"])
        self.assertEqual(capture["argv"][capture["argv"].index("--effort") + 1], "high")
        self.assertEqual(
            (capture["claudecode"], capture["claude_code_token"], capture["claude_config_dir"]), (None, None, None)
        )
        self.assertEqual(
            result["usage"]["totals"], {"fresh_input": 10, "cache_read": 200, "cache_write": 30, "output": 40}
        )

    def test_agy_quota_error_is_reported_as_provider_quota_exhausted(self) -> None:
        with mock.patch.dict(
            os.environ, {"FAKE_AGY_STATUS": "error", "FAKE_AGY_ERROR": "RESOURCE_EXHAUSTED (code 429): quota"}
        ):
            exit_code, result, _out, _summary, _capture = self.run_main(engine="agy")
        self.assertEqual((result["status"], exit_code), ("provider_quota_exhausted", 16))
        self.assertIn("RESOURCE_EXHAUSTED", result["provider_error"])

    def run_claude_fixture(self, fixture: str, **environment: str) -> tuple[int, dict, Path]:
        env = {"FAKE_CLAUDE_STDOUT_FILE": str(FIXTURES / fixture), **environment}
        with mock.patch.dict(os.environ, env):
            exit_code, result, out_dir, _summary, _capture = self.run_main(
                engine="claude",
                model="sonnet",
                verify_commands=["test -f allowed/output.txt"],
            )
        return exit_code, result, out_dir

    def test_claude_json_array_output_yields_the_report_and_runs_the_gates(self) -> None:
        # Claude Code with verbose output prints every message as one JSON array on a single line.
        exit_code, result, out_dir = self.run_claude_fixture("claude-2.1-json-array.json")
        self.assertEqual((result["status"], exit_code), ("completed", 0), result.get("error"))
        final_output = (out_dir / "final-output.txt").read_text(encoding="utf-8")
        self.assertTrue(final_output.startswith("### STATUS\n\nCOMPLETE"))
        self.assertEqual(result["reported_outcome"], "complete")
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(result["session_id"], "00000000-0000-4000-8000-00000000c1a0")
        self.assertEqual(result["usage"]["totals"]["output"], 79761)

    def test_claude_model_content_mentioning_quota_is_not_a_provider_quota_error(self) -> None:
        # Regression: tool output and the report mention quota, rate limits and HTTP 429, and a
        # rate_limit_event says overage is rejected. None of that is a provider error.
        exit_code, result, out_dir = self.run_claude_fixture("claude-2.1-json-array-multi-result.json")
        self.assertEqual((result["status"], exit_code), ("completed", 0))
        self.assertIsNone(result["provider_error"])
        self.assertIn("plan.storageQuota", (out_dir / "final-output.txt").read_text(encoding="utf-8"))

    def test_claude_nonzero_exit_with_quota_words_in_content_is_an_ordinary_failure(self) -> None:
        exit_code, result, _out_dir = self.run_claude_fixture(
            "claude-2.1-json-array-multi-result.json", FAKE_CLAUDE_EXIT="1"
        )
        self.assertEqual((result["status"], exit_code), ("failed", 1))

    def test_claude_usage_limit_error_is_reported_as_provider_quota_exhausted(self) -> None:
        with mock.patch.dict(
            os.environ, {"FAKE_CLAUDE_ERROR": "Claude AI usage limit reached|1790427600", "FAKE_CLAUDE_EXIT": "1"}
        ):
            exit_code, result, _out, _summary, _capture = self.run_main(engine="claude", model="sonnet")
        self.assertEqual((result["status"], exit_code), ("provider_quota_exhausted", 16))
        self.assertIn("usage limit reached", result["provider_error"])

    def test_claude_rate_limit_on_stderr_is_reported_as_provider_quota_exhausted(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_CLAUDE_STDERR": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
                "FAKE_CLAUDE_EXIT": "1",
                "FAKE_NO_CHANGE": "1",
            },
        ):
            exit_code, result, _out, _summary, _capture = self.run_main(
                engine="claude", model="sonnet", expect_changes=False
            )
        self.assertEqual((result["status"], exit_code), ("provider_quota_exhausted", 16))


if __name__ == "__main__":
    unittest.main()


class StatusFormattingTests(unittest.TestCase):
    def outcome(self, text: str) -> str | None:
        report, extracted = RUNNER.extract_raw_final_report(text)
        return RUNNER.parse_reported_outcome(report, extracted)

    def test_decorated_status_tokens_parse(self) -> None:
        for text in (
            "STATUS\n`COMPLETE`",
            "**STATUS**\n**COMPLETE**",
            "## STATUS:\nCOMPLETE.",
            "## **STATUS**\n- `COMPLETE`",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.outcome(text), "complete")
        self.assertEqual(self.outcome("STATUS\n`BLOCKED` - no network"), "blocked")

    def test_qualified_complete_stays_unknown(self) -> None:
        self.assertEqual(self.outcome("STATUS\nCOMPLETE - except tests"), "unknown")


class ClaudeOutputParsingTests(unittest.TestCase):
    REPORT = "### STATUS\nCOMPLETE\n\n### FILES CHANGED\n- none\n"

    def result_event(self, text: str | None = None, **fields: object) -> dict:
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "s-1",
            "result": self.REPORT if text is None else text,
            **fields,
        }

    def test_fixture_array_single_line(self) -> None:
        text = (FIXTURES / "claude-2.1-json-array.json").read_text(encoding="utf-8")
        report, session, status, error = RUNNER.parse_claude_output(text)
        self.assertEqual((status, error, session), ("success", None, "00000000-0000-4000-8000-00000000c1a0"))
        self.assertTrue(report.startswith("### STATUS"))

    def test_fixture_with_several_results_uses_the_last(self) -> None:
        text = (FIXTURES / "claude-2.1-json-array-multi-result.json").read_text(encoding="utf-8")
        report, _session, status, _error = RUNNER.parse_claude_output(text)
        self.assertEqual(status, "success")
        self.assertTrue(report.startswith("### STATUS"), report[:80])

    def test_every_output_shape_yields_the_report(self) -> None:
        init = {"type": "system", "subtype": "init", "session_id": "s-1"}
        final = self.result_event()
        shapes = {
            "single object": json.dumps(final),
            "json array": json.dumps([init, final]),
            "pretty array": json.dumps([init, final], indent=2),
            "json lines": f"{json.dumps(init)}\n{json.dumps(final)}\n",
            "noise before json": f"warning: something\n{json.dumps([init, final])}\n",
            "envelope": json.dumps({"event": final}),
            "messages envelope": json.dumps({"messages": [init, final]}),
            "wrapped result text": json.dumps(self.result_event({"text": self.REPORT})),
            "content blocks": json.dumps(self.result_event([{"type": "text", "text": self.REPORT}])),
            "stream-json": "\n".join(
                json.dumps(event)
                for event in (
                    init,
                    {"type": "stream_event", "event": {"type": "content_block_delta"}, "session_id": "s-1"},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
                    final,
                )
            ),
        }
        for name, text in shapes.items():
            with self.subTest(shape=name):
                report, session, status, error = RUNNER.parse_claude_output(text)
                self.assertEqual((report, session, status, error), (self.REPORT, "s-1", "success", None))

    def test_no_result_stays_empty(self) -> None:
        for text in ("", "not json", json.dumps([{"type": "system", "subtype": "init", "session_id": "s-1"}])):
            with self.subTest(text=text[:20]):
                report, _session, status, _error = RUNNER.parse_claude_output(text)
                self.assertEqual((report, status), ("", "unknown"))

    def test_error_result_carries_the_error_not_a_report(self) -> None:
        text = json.dumps([self.result_event("API Error: 500", is_error=True, api_error_status=500)])
        report, _session, status, error = RUNNER.parse_claude_output(text)
        self.assertEqual((report, status), ("", "error"))
        self.assertIn("API Error: 500", error)


class ProviderQuotaDetectionTests(unittest.TestCase):
    CONTENT = (
        "docs mention live quota checks; form field plan.storageQuota; "
        "'The request rate limit was exceeded.' with status 429 from the mock; X-Rate-Limit-Retry-After-Seconds"
    )

    def test_fixture_stdout_never_signals_quota(self) -> None:
        for fixture in ("claude-2.1-json-array.json", "claude-2.1-json-array-multi-result.json"):
            text = (FIXTURES / fixture).read_text(encoding="utf-8")
            with self.subTest(fixture=fixture):
                self.assertIsNone(RUNNER.provider_terminal_error("claude", text))
                self.assertFalse(RUNNER.structured_quota_signal("claude", text))

    def test_model_content_is_not_a_provider_error_for_any_engine(self) -> None:
        streams = {
            "claude": json.dumps(
                [
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": self.CONTENT}]}},
                    {"type": "result", "is_error": False, "result": self.CONTENT},
                ]
            ),
            "codex": json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": self.CONTENT}}),
            "opencode": json.dumps({"type": "text", "part": {"type": "text", "text": self.CONTENT}}),
            "agy": json.dumps({"type": "result", "status": "success", "response": self.CONTENT, "error": ""}),
        }
        for engine, stdout in streams.items():
            with self.subTest(engine=engine):
                error = RUNNER.provider_terminal_error(engine, stdout)
                self.assertIsNone(error)
                self.assertFalse(RUNNER.structured_quota_signal(engine, stdout))
                self.assertFalse(RUNNER.provider_quota_exhausted(error, ""))

    def test_bare_quota_vocabulary_does_not_match(self) -> None:
        for text in (
            "quota",
            "plan.storageQuota",
            "rate limit",
            "rate-limit",
            "usage limit",
            "credit balance",
            "The request rate limit was exceeded.",
            "checks the quota before sending",
        ):
            with self.subTest(text=text):
                self.assertFalse(RUNNER.provider_quota_exhausted(text))

    def test_real_provider_errors_still_match(self) -> None:
        cases = {
            "agy": json.dumps(
                {
                    "type": "result",
                    "status": "error",
                    "response": "",
                    "error": "RESOURCE_EXHAUSTED (code 429): You have exhausted your capacity on this model.",
                }
            ),
            "claude-usage-limit": json.dumps(
                [
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": True,
                        "result": "Claude AI usage limit reached|1790427600",
                    }
                ]
            ),
            "claude-hit-limit": json.dumps(
                {"type": "result", "is_error": True, "result": "You've hit your limit · resets 3pm"}
            ),
            "claude-api-429": json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "result": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
                }
            ),
            "claude-credits": json.dumps({"type": "result", "is_error": True, "result": "Credit balance is too low"}),
            "codex-turn-failed": json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "You've hit your usage limit. Upgrade to Pro or try again later."},
                }
            ),
            "codex-error": json.dumps(
                {"type": "error", "message": "exceeded retry limit, last status: 429 Too Many Requests"}
            ),
            "codex-quota": json.dumps(
                {"type": "error", "message": "Quota exceeded. Check your plan and billing details."}
            ),
            "opencode": json.dumps(
                {
                    "type": "error",
                    "error": {
                        "name": "APIError",
                        "data": {
                            "message": "Insufficient credits. Add more using https://openrouter.ai/settings/credits",
                            "statusCode": 402,
                        },
                    },
                }
            ),
        }
        for name, stdout in cases.items():
            engine = name.split("-")[0]
            with self.subTest(case=name):
                error = RUNNER.provider_terminal_error(engine, stdout)
                self.assertIsNotNone(error)
                self.assertTrue(RUNNER.provider_quota_exhausted(error, ""), error)

    def test_structured_signals(self) -> None:
        rejected = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}
        overage_only = {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "overageStatus": "rejected"},
        }
        self.assertTrue(RUNNER.structured_quota_signal("claude", json.dumps([rejected])))
        self.assertFalse(RUNNER.structured_quota_signal("claude", json.dumps([overage_only])))
        self.assertTrue(
            RUNNER.structured_quota_signal("claude", json.dumps({"type": "assistant", "error": "rate_limit"}))
        )
        self.assertTrue(
            RUNNER.structured_quota_signal(
                "claude", json.dumps({"type": "result", "is_error": True, "api_error_status": 429})
            )
        )
        self.assertTrue(
            RUNNER.structured_quota_signal(
                "opencode",
                json.dumps(
                    {
                        "type": "error",
                        "error": {"name": "APIError", "data": {"message": "Too Many Requests", "statusCode": 429}},
                    }
                ),
            )
        )

    def test_stderr_messages(self) -> None:
        for line in (
            "ERROR: Quota exceeded. Check your plan and billing details.",
            "Error: 429 Too Many Requests",
            "API Error: 429 rate_limit_error",
            "error: insufficient_quota",
        ):
            with self.subTest(line=line):
                self.assertTrue(RUNNER.provider_quota_exhausted(None, line))
        self.assertFalse(
            RUNNER.provider_quota_exhausted(None, "warning: config mentions quota and rate limit settings")
        )


class SteerTests(unittest.TestCase):
    """`steer` on detached jobs driven by fake executors: no model is ever called."""

    setUp = RunnerIntegrationTests.setUp
    _write_executable = RunnerIntegrationTests._write_executable
    _write_fake_executors = RunnerIntegrationTests._write_fake_executors

    def tearDown(self) -> None:
        # Release any still-blocked fake so no detached runner outlives the temporary directories.
        for release in self.external_path.glob("release-*"):
            release.write_text("go", encoding="utf-8")
        for job in getattr(self, "launched", []):
            self.wait_until(lambda job=job: not RUNNER.runner_process_alive(job["pid"], job["event_path"]), timeout=30)
        RunnerIntegrationTests.tearDown(self)

    def wait_until(self, condition, *, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def environment(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(self.external_path / "steer-cache"),
            **extra,
        }

    def launch(
        self,
        *,
        engine: str = "codex",
        model: str | None = None,
        block: bool = True,
        silent: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        self.run_index += 1
        index = self.run_index
        brief = self.external_path / f"steer-brief-{index}.md"
        brief.write_text("# Objective\n\nCreate the fixture output.\n", encoding="utf-8")
        environment = self.environment(
            FAKE_CAPTURE=str(self.external_path / f"steer-capture-{index}.json"),
            FAKE_OUTPUT="allowed/output.txt",
            FAKE_BLOCK="1" if block else "0",
            FAKE_BLOCK_SILENT="1" if silent else "0",
            FAKE_STARTED=str(self.external_path / f"started-{index}"),
            FAKE_RELEASE=str(self.external_path / f"release-{index}"),
            **(extra_env or {}),
        )
        argv = [
            str(SCRIPT),
            "--cwd",
            str(self.repository.path),
            "--brief",
            str(brief),
            "--engine",
            engine,
            "--detach",
            "--notify",
            "none",
            "--allow-path",
            "allowed",
            "--expect-changes",
            "--verify-command",
            "test -f allowed/output.txt",
            "--timeout",
            "5m",
        ]
        if model:
            argv += ["--model", model]
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, environment),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(RUNNER.main(), 0)
        values = dict(line.split("=", 1) for line in output.getvalue().splitlines() if line.startswith("AGENT_"))
        job_path = Path(values["AGENT_JOB"])
        job = {
            "job_dir": job_path.parent,
            "job_path": job_path,
            "event_path": values["AGENT_EVENT"],
            "event_id": values["AGENT_EVENT_ID"],
            "result_path": Path(values["AGENT_RESULT"]),
            "pid": int(values["AGENT_JOB_PID"]),
            "mailbox": values["AGENT_MAILBOX"],
            "capture": Path(environment["FAKE_CAPTURE"]),
            "started": Path(environment["FAKE_STARTED"]),
            "release": Path(environment["FAKE_RELEASE"]),
        }
        self.launched = [*getattr(self, "launched", []), job]
        if block:
            state_path = job["job_dir"] / RUNNER.STEER_STATE_NAME
            self.assertTrue(
                self.wait_until(
                    lambda: (
                        job["started"].exists()
                        and state_path.exists()
                        and json.loads(state_path.read_text())["phase"] == "executor_running"
                    )
                )
            )
        return job

    def steer(self, reference: str, message: str, *extra: str) -> tuple[int, str]:
        output = io.StringIO()
        argv = [str(SCRIPT), "steer", reference, "--message", message, *extra]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, self.environment()),
            contextlib.redirect_stdout(output),
        ):
            return RUNNER.main(), output.getvalue()

    def steer_refused(
        self, reference: str, *extra: str, message: str = "Look at the parser instead."
    ) -> RUNNER.RunnerError:
        with self.assertRaises(RUNNER.RunnerError) as caught:
            self.steer(reference, message, *extra)
        return caught.exception

    def wait_event(self, event_reference: str) -> dict:
        with mock.patch.dict(os.environ, self.environment()):
            path = RUNNER.completion_event_path_from_reference(event_reference)
            return list(
                RUNNER.wait_for_completion_events([path], repository=None, timeout_seconds=60, poll_seconds=0.05)
            )[-1]

    def test_codex_steer_resumes_the_same_session_under_the_same_contract(self) -> None:
        cache = self.external_path / "steer-cache" / "agent-executor"
        key = hashlib.sha256(os.fsencode(str(self.repository.path.resolve()))).hexdigest()
        job = self.launch(
            extra_env={"FAKE_RESUME_SLEEP": "1.5", "FAKE_LEASE_PROBE": str(cache / "worktree-locks-v1" / f"{key}.lock")}
        )
        with mock.patch.dict(os.environ, self.environment()):
            mailbox = RUNNER.bundled_module("peer_mailbox")
            mailbox_info = mailbox.run_info(mailbox.root(), job["event_id"])
            worker = mailbox.load(mailbox.peer_path(mailbox.root(), mailbox_info["worker"]))
            question = mailbox.send(
                mailbox.root(),
                sender=worker,
                to=mailbox_info["conductor"],
                text="Does steering keep this conversation?",
                kind="question",
                client_id="before-steer",
            )
        message = "New lead: the root cause is in the tokenizer, not the parser."
        exit_code, output = self.steer(job["event_id"], message, "--cwd", str(self.repository.path))
        self.assertEqual(exit_code, 0)
        values = dict(line.split("=", 1) for line in output.splitlines())
        self.assertEqual(values["AGENT_STEER_STATUS"], "accepted")
        self.assertEqual(values["AGENT_SESSION"], "codex-thread-1")
        self.assertEqual(values["AGENT_STEERED_FROM"], job["event_id"])
        self.assertNotEqual(values["AGENT_EVENT_ID"], job["event_id"])

        # The continuation runs under the job's lease: no second runner can start meanwhile.
        support = RUNNER.bundled_module("execution_support")
        with mock.patch.dict(os.environ, self.environment()), self.assertRaises(support.ContractError):
            with support.workspace_lease(self.repository.path, RUNNER.default_agent_cache_dir()):
                pass

        # Waiting on the original event follows the job to its continuation.
        event = self.wait_event(job["event_id"])
        self.assertEqual(event["event_id"], values["AGENT_EVENT_ID"])
        self.assertEqual(event["steered_from"], [job["event_id"]])
        result = json.loads(Path(values["AGENT_RESULT"]).read_text(encoding="utf-8"))
        self.assertEqual((result["status"], result["verification"]["status"]), ("completed", "passed"))
        self.assertEqual(result["mailbox"]["run"], job["event_id"])
        self.assertEqual(result["mailbox"]["conductor"], job["mailbox"])
        with mock.patch.dict(os.environ, self.environment()):
            self.assertEqual(mailbox.run_history(mailbox.root(), job["event_id"])["messages"][0]["id"], question["id"])
        self.assertEqual(result["command"][1:4], ["exec", "resume", "codex-thread-1"])
        self.assertEqual(result["session_id"], "codex-thread-1")
        self.assertEqual(result["allowed_paths"], ["allowed"])
        self.assertEqual(result["verification_commands"], ["test -f allowed/output.txt"])
        self.assertEqual(result["run_delta"]["changed_paths"], ["allowed/output.txt"])
        self.assertEqual([attempt["segment"] for attempt in result["executor_attempts"]], [0, 1])
        self.assertTrue(result["executor_attempts"][0]["steered"])
        steer = result["steering"]["steers"][0]
        self.assertEqual(
            (steer["steer_id"], steer["session_id"], steer["previous_event_id"]), (1, "codex-thread-1", job["event_id"])
        )
        self.assertEqual(steer["message_sha256"], RUNNER.text_fingerprint(message))

        capture = json.loads(job["capture"].read_text(encoding="utf-8"))
        self.assertTrue(capture["lease_held"], "the resumed executor must still hold the worktree lease")
        for expected in (
            "STEERING UPDATE 1",
            message,
            "Paths you may change: allowed",
            "test -f allowed/output.txt",
            "EXECUTION AND REPORT CONTRACT",
            "RISKS OR BLOCKERS",
        ):
            self.assertIn(expected, capture["stdin"])

        # Audit trail: the message, request and answer stay in the job directory.
        steer_dir = job["job_dir"] / "steers" / "1"
        self.assertEqual((steer_dir / "message.txt").read_text(encoding="utf-8"), message)
        self.assertEqual(json.loads((steer_dir / "response.json").read_text())["status"], "accepted")
        self.assertTrue((steer_dir / "request.json").exists())
        self.assertEqual(stat.S_IMODE((steer_dir / "message.txt").stat().st_mode), 0o600)
        ledger = json.loads((job["job_dir"] / RUNNER.STEER_STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(ledger["phase"], "finished")
        self.assertEqual(ledger["steers"][0]["event_id"], values["AGENT_EVENT_ID"])

        # The interrupted segment is superseded and pre-acknowledged; the inbox shows one result to review.
        original = json.loads(Path(job["event_path"]).read_text(encoding="utf-8"))
        self.assertEqual((original["status"], original["superseded_by"]), ("steered", values["AGENT_EVENT_ID"]))
        self.assertIsNotNone(original["acknowledged_at"])
        self.assertEqual(json.loads(job["result_path"].read_text())["status"], "steered")
        with mock.patch.dict(os.environ, self.environment()):
            inbox, _errors = RUNNER.completion_events(repository=self.repository.path, include_acknowledged=False)
        self.assertEqual([item["event_id"] for item in inbox], [values["AGENT_EVENT_ID"]])

        # A steered job can be addressed again by its new event, and the finished job is refused.
        error = self.steer_refused(values["AGENT_EVENT_ID"])
        self.assertEqual(error.exit_code, RUNNER.EXIT_STEER_JOB_FINISHED)
        self.assertIn("--session codex-thread-1 --resume-result", str(error))

    def test_each_resumable_engine_continues_its_own_session(self) -> None:
        cases = {
            "claude": ("opus", "--resume"),
            "agy": (None, "--conversation"),
            "opencode": (None, "--session"),
        }
        for engine, (model, flag) in cases.items():
            with self.subTest(engine=engine):
                self.repository.git("clean", "-fdq")
                job = self.launch(engine=engine, model=model)
                exit_code, output = self.steer(str(job["job_dir"]), f"Steer the {engine} run.")
                self.assertEqual(exit_code, 0)
                values = dict(line.split("=", 1) for line in output.splitlines())
                self.wait_event(values["AGENT_EVENT_ID"])
                result = json.loads(Path(values["AGENT_RESULT"]).read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "completed", result.get("final_message"))
                self.assertEqual(result["command"][result["command"].index(flag) + 1], values["AGENT_SESSION"])
                first = json.loads(job["result_path"].read_text(encoding="utf-8"))
                if engine == "claude":
                    # Claude Code prints nothing until it finishes, so the runner names the session up front.
                    self.assertEqual(
                        first["command"][first["command"].index("--session-id") + 1], values["AGENT_SESSION"]
                    )
                brief = Path(result["submitted_brief_path"]).read_text(encoding="utf-8")
                self.assertIn(f"Steer the {engine} run.", brief)
                self.assertIn("EXECUTION AND REPORT CONTRACT", brief)

    def test_refuses_other_worktrees_and_executors_without_a_session(self) -> None:
        job = self.launch(silent=True)
        other = TemporaryGitRepository()
        self.addCleanup(other.close)
        error = self.steer_refused(job["event_id"], "--cwd", str(other.path))
        self.assertEqual(error.exit_code, RUNNER.EXIT_STEER_OTHER_WORKTREE)

        error = self.steer_refused(job["event_id"])
        self.assertEqual(error.exit_code, RUNNER.EXIT_STEER_REJECTED)
        self.assertIn("has not exposed a session ID", str(error))
        self.assertIn("relaunch", str(error))
        response = json.loads((job["job_dir"] / "steers" / "1" / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(response["status"], "rejected")

        # The rejected request left the executor running untouched; it finishes as an ordinary run.
        job["release"].write_text("go", encoding="utf-8")
        event = self.wait_event(job["event_id"])
        result = json.loads(job["result_path"].read_text(encoding="utf-8"))
        self.assertEqual((event["status"], result["status"]), ("completed", "completed"))
        self.assertNotIn("steering", result)
        self.assertEqual(len(result["executor_attempts"]), 1)

    def test_refuses_a_finished_job_and_points_to_session_resume(self) -> None:
        job = self.launch(block=False)
        self.wait_event(job["event_id"])
        error = self.steer_refused(str(job["job_path"]))
        self.assertEqual(error.exit_code, RUNNER.EXIT_STEER_JOB_FINISHED)
        self.assertIn(f"--session codex-thread-1 --resume-result {job['result_path']}", str(error))

    def test_refuses_a_job_whose_runner_cannot_be_found(self) -> None:
        cache = self.external_path / "steer-cache" / "agent-executor"
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        cases = {
            "dead-pid": (dead.pid, 1, RUNNER.EXIT_STEER_PROCESS_NOT_FOUND),
            # A live PID whose command line is not this job's runner (a reused PID).
            "reused-pid": (os.getpid(), 1, RUNNER.EXIT_STEER_PROCESS_NOT_FOUND),
            "pre-0.11-runner": (os.getpid(), None, RUNNER.EXIT_STEER_REJECTED),
        }
        for name, (pid, protocol, expected) in cases.items():
            with self.subTest(case=name):
                job_dir = cache / "jobs-v1" / name
                job_dir.mkdir(parents=True)
                job = {
                    "schema": "agent-executor.job.v1",
                    "runner_version": "0.10.1",
                    "pid": pid,
                    "repository": str(self.repository.path.resolve()),
                    "result_path": str(job_dir / "run" / "result.json"),
                    "completion_event_path": str(
                        cache / "completions-v1" / f"{hashlib.md5(name.encode()).hexdigest()}.json"
                    ),
                    "completion_event_id": "0" * 32,
                }
                if protocol is not None:
                    job["steer_protocol"] = protocol
                (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
                error = self.steer_refused(str(job_dir))
                self.assertEqual(error.exit_code, expected, str(error))
                self.assertFalse((job_dir / "steers").exists(), "no request may be written for a refused steer")


class SteeringUnitTests(unittest.TestCase):
    def test_brief_restates_scope_checks_and_report_contract(self) -> None:
        brief = RUNNER.steering_brief(
            "Try the cache first.",
            steer_id=2,
            allowed_paths=["src", "tests"],
            verify_commands=["make test"],
            expect_changes=True,
        )
        self.assertTrue(brief.startswith("STEERING UPDATE 2"))
        for expected in (
            "Try the cache first.",
            "src, tests",
            "make test",
            "net change",
            "cannot widen the scope",
            RUNNER.REPORT_CONTRACT,
        ):
            self.assertIn(expected, brief)
        read_only = RUNNER.steering_brief(
            "Only look.", steer_id=1, allowed_paths=[], verify_commands=[], expect_changes=False
        )
        self.assertIn("read-only", read_only)
        self.assertNotIn("net change", read_only)

    def test_claude_fresh_runs_name_their_session_and_resumes_do_not(self) -> None:
        common = dict(
            executable="claude",
            engine="claude",
            model="opus",
            variant=None,
            repo=Path("/repo"),
            add_dirs=[],
            continue_last=False,
            timeout="15m",
            submitted_brief_path=Path("/b"),
            final_output_path=Path("/f"),
            log_path=Path("/l"),
        )
        fresh = RUNNER.build_executor_command(session_id=None, new_session_id="abc", **common)
        self.assertEqual(fresh[fresh.index("--session-id") + 1], "abc")
        resumed = RUNNER.build_executor_command(session_id="abc", new_session_id="ignored", **common)
        self.assertNotIn("--session-id", resumed)
        self.assertEqual(resumed[resumed.index("--resume") + 1], "abc")

    def test_poll_interrupt_is_not_a_timeout(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True, stderr=subprocess.DEVNULL
        )
        calls = []

        def poll() -> bool:
            calls.append(1)
            if len(calls) < 2:
                return False
            RUNNER.interrupt_process(process, grace_seconds=5)
            return True

        timed_out = RUNNER.wait_with_heartbeats(
            process,
            timeout_seconds=20,
            heartbeat_seconds=0,
            monotonic_start=time.monotonic(),
            poll=poll,
            poll_seconds=0.02,
        )
        self.assertFalse(timed_out)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(len(calls), 2)
