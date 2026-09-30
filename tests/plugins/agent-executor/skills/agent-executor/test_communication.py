from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_coordination
import test_run_agent

RUNNER = test_run_agent.RUNNER
COMM = RUNNER.bundled_module("communication")
SUPPORT = RUNNER.bundled_module("execution_support")
SCRIPT = Path(COMM.__file__)


class CommunicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.channel = self.directory / "channel.json"
        self.result = self.directory / "result.json"
        COMM.initialize(
            self.channel,
            repository=self.directory,
            result_path=self.result,
            deadline=(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True, timeout=5)

    def send(self, id="q1", sender="worker", kind="question", text="Which component owns cancellation?", reply_to=None):
        return COMM.send(self.channel, sender=sender, message_id=id, kind=kind, text=text, reply_to=reply_to)

    def test_worker_asks_and_receives_exact_reply_from_another_process(self):
        worker = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "ask",
                str(self.channel),
                "--sender",
                "worker",
                "--id",
                "q1",
                "--text",
                "Which version?",
                "--timeout",
                "3",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            received = COMM.wait(self.channel, recipient="conductor", timeout=2)
            self.assertEqual(received["messages"][0]["text"], "Which version?")
            reply = self.cli(
                "send",
                str(self.channel),
                "--sender",
                "conductor",
                "--id",
                "a1",
                "--kind",
                "reply",
                "--reply-to",
                "q1",
                "--text",
                "Use version 2; preserve cancellation.",
            )
            self.assertEqual(reply.returncode, 0, reply.stderr)
            stdout, stderr = worker.communicate(timeout=4)
            self.assertEqual(worker.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["messages"][0]["text"], "Use version 2; preserve cancellation.")
            self.assertEqual(COMM.inbox(self.channel, "conductor"), [])
            # Restarting the waiter replays the durable answer, without sending a new question.
            replay = COMM.wait(self.channel, recipient="worker", timeout=0.1, reply_to="q1")
            self.assertEqual(replay["messages"][0]["id"], "a1")
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate()

    def test_conductor_question_worker_reply_and_acknowledgment(self):
        self.send(sender="conductor", text="What evidence supports that?")
        self.assertEqual(COMM.inbox(self.channel, "worker")[0]["text"], "What evidence supports that?")
        self.send(id="a1", sender="worker", kind="reply", text="The trace shows cancellation first.", reply_to="q1")
        result = COMM.wait(self.channel, recipient="conductor", timeout=0.1, reply_to="q1")
        self.assertEqual(result["messages"][0]["text"], "The trace shows cancellation first.")
        COMM.acknowledge(self.channel, "conductor", "a1")
        self.assertEqual(COMM.inbox(self.channel, "conductor"), [])
        self.assertEqual([item["kind"] for item in COMM.read(self.channel)["messages"]], ["question", "reply"])

    def test_retry_is_idempotent_and_conflicting_or_misdirected_reply_is_rejected(self):
        self.send()
        self.send()
        self.assertEqual([message["id"] for message in COMM.inbox(self.channel, "conductor")], ["q1"])
        with self.assertRaisesRegex(ValueError, "different content"):
            self.send(text="Changed question")
        with self.assertRaisesRegex(ValueError, "question sent to"):
            self.send(id="a1", sender="worker", kind="reply", reply_to="q1")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            COMM.acknowledge(self.channel, "worker", "q1")

    def test_concurrent_senders_do_not_drop_messages(self):
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "send",
                    str(self.channel),
                    "--sender",
                    "worker" if index % 2 else "conductor",
                    "--id",
                    f"m{index}",
                    "--kind",
                    "update",
                    "--text",
                    f"Finding {index}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(8)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
        history = COMM.read(self.channel)["messages"]
        self.assertEqual(sorted(message["text"] for message in history), [f"Finding {index}" for index in range(8)])

    def test_wait_times_out_and_completion_closes_the_channel(self):
        result = self.cli("wait", str(self.channel), "--recipient", "conductor", "--timeout", ".02")
        self.assertEqual((result.returncode, json.loads(result.stdout)["status"]), (12, "timed_out"))
        self.result.write_text(json.dumps({"finished_at": "now", "status": "completed"}))
        self.assertEqual(COMM.wait(self.channel, recipient="worker", timeout=1)["reason"], "completed")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.send()

    def test_channel_survives_steering_until_the_final_segment_finishes(self):
        next_result = self.directory / "next.json"
        self.result.write_text(
            json.dumps({"finished_at": "now", "status": "superseded", "steered_to": {"result_path": str(next_result)}})
        )
        self.send()
        self.assertEqual(COMM.inbox(self.channel, "conductor")[0]["id"], "q1")
        COMM.acknowledge(self.channel, "conductor", "q1")
        next_result.write_text(json.dumps({"finished_at": "now", "status": "blocked"}))
        self.assertEqual(COMM.wait(self.channel, recipient="conductor", timeout=0.1)["reason"], "blocked")

    def test_multiple_reader_leases_exclude_writer_and_writer_excludes_readers(self):
        with tempfile.TemporaryDirectory() as cache:
            with SUPPORT.workspace_lease(self.directory, Path(cache), read_only=True):
                with SUPPORT.workspace_lease(self.directory, Path(cache), read_only=True):
                    with self.assertRaisesRegex(SUPPORT.ContractError, "another executor"):
                        with SUPPORT.workspace_lease(self.directory, Path(cache)):
                            pass
            with SUPPORT.workspace_lease(self.directory, Path(cache)):
                with self.assertRaisesRegex(SUPPORT.ContractError, "another executor"):
                    with SUPPORT.workspace_lease(self.directory, Path(cache), read_only=True):
                        pass
            # Releasing the exclusive holder allows a reader again.
            with SUPPORT.workspace_lease(self.directory, Path(cache), read_only=True):
                self.send(text="Reader resumed after writer released its lease")
            self.assertEqual(
                COMM.inbox(self.channel, "conductor")[0]["text"], "Reader resumed after writer released its lease"
            )


class OrchestratorCollaborationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_coordination.CoordinatorTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_investigation_can_start_without_a_hypothesis_and_is_audited_read_only(self):
        with mock.patch.dict(os.environ, {"FAKE_NO_CHANGE": "1"}):
            code, run = self.fixture.start("explore", "investigation")
        self.assertEqual((code, run["runner_status"]), (0, "completed"))
        result = json.loads(Path(run["result_path"]).read_text())
        self.assertEqual((result["allowed_paths"], result["expected_changes"]), ([], False))
        code, writing = self.fixture.start("bad-explore", "investigation")
        self.assertEqual((code, writing["runner_status"]), (0, "scope_violation"))

    def test_claude_cli_dispatch_is_supported_when_host_is_not_claude(self):
        coordinator = test_coordination.COORDINATOR
        state = coordinator.load(self.fixture.directory)
        state["host"] = "codex"
        coordinator.save(self.fixture.directory, state)
        with mock.patch.dict(os.environ, {"FAKE_NO_CHANGE": "1"}):
            code, run = self.fixture.call(
                "dispatch",
                "--kind",
                "investigation",
                "--request-id",
                "claude-research",
                "--engine",
                "claude",
                "--model",
                "sonnet",
            )
        self.assertEqual((code, run["runner_status"]), (0, "completed"))

    def test_live_worker_question_wakes_completion_wait_and_receives_answer(self):
        # Exercise the actual runner transport with a deterministic executor. No model quota.
        executable = self.fixture.fixture.external_path / "codex"
        source = executable.read_text()
        source = source.replace(
            "    prompt = sys.stdin.read()",
            """    prompt = sys.stdin.read()
    answer = subprocess.run([sys.executable, os.environ['FAKE_COMM_SCRIPT'], 'ask', os.environ['AGENT_CHANNEL'],
                             '--sender', 'worker', '--id', 'which-version', '--text', 'Which client version?',
                             '--timeout', '10'], capture_output=True, text=True, check=True)
    pathlib.Path(os.environ['FAKE_ANSWER']).write_text(answer.stdout)
""",
        )
        executable.write_text(source)
        answer_path = self.fixture.fixture.external_path / "answer.json"
        with mock.patch.dict(
            os.environ, {"FAKE_NO_CHANGE": "1", "FAKE_COMM_SCRIPT": str(SCRIPT), "FAKE_ANSWER": str(answer_path)}
        ):
            code, run = self.fixture.start("live", "investigation", "--background")
        self.assertEqual((code, run["status"]), (0, "active"))
        watched = subprocess.run(
            [
                sys.executable,
                "-B",
                str(test_run_agent.SCRIPT),
                "events",
                "wait",
                run["event_path"],
                "--messages",
                "--timeout",
                "10s",
                "--poll-seconds",
                ".02",
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(watched.returncode, 0, watched.stderr)
        signal = json.loads(watched.stdout)
        self.assertEqual(signal["messages"][0]["text"], "Which client version?")
        self.assertEqual(self.fixture.call("next")[1]["action"], "respond")
        COMM.send(
            Path(run["channel_path"]),
            sender="conductor",
            message_id="version-answer",
            kind="reply",
            text="Client 2",
            reply_to="which-version",
        )
        completed = list(
            RUNNER.wait_for_completion_events(
                [Path(run["event_path"])], repository=None, timeout_seconds=10, poll_seconds=0.02
            )
        )[0]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(json.loads(answer_path.read_text())["messages"][0]["text"], "Client 2")
        code, state = self.fixture.call("recover")
        self.assertEqual((code, state["budget"]["used"]), (0, 1))

    def test_readonly_observation_commands_remain_available_under_dispatch_lock(self):
        with SUPPORT.workspace_lease(self.fixture.directory, RUNNER.default_agent_cache_dir() / "coordinator-locks"):
            code, shown = self.fixture.call("show")
            self.assertEqual((code, shown["status"]), (0, "active"))
            self.assertEqual(self.fixture.call("next")[1]["action"], "continue")

    def test_two_investigators_run_concurrently_and_prevent_a_writer(self):
        directory = self.fixture.fixture.external_path
        release = directory / "release"
        runs = []
        try:
            for index in range(2):
                started = directory / f"started-{index}"
                with mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_NO_CHANGE": "1",
                        "FAKE_BLOCK": "1",
                        "FAKE_RELEASE": str(release),
                        "FAKE_STARTED": str(started),
                    },
                ):
                    code, run = self.fixture.start(f"reader-{index}", "investigation", "--background")
                self.assertEqual((code, run["status"]), (0, "active"))
                runs.append(run)
                until = time.monotonic() + 5
                while not started.exists() and time.monotonic() < until:
                    time.sleep(0.02)
                self.assertEqual(started.read_text(), "started")
            code, error = self.fixture.start("writer", "implementation")
            self.assertEqual(code, 2)
            self.assertIn("only active read-only runs can overlap", error)
        finally:
            release.touch()
            for run in runs:
                result = list(
                    RUNNER.wait_for_completion_events(
                        [Path(run["event_path"])], repository=None, timeout_seconds=10, poll_seconds=0.02
                    )
                )[0]
                self.assertEqual(result["status"], "completed")
        self.assertEqual(self.fixture.call("recover")[1]["budget"]["used"], 2)

    def test_consultant_keeps_original_context_and_prior_results(self):
        coordinator = test_coordination.COORDINATOR
        state = coordinator.load(self.fixture.directory)
        state["task_spec"]["context_packet"] = {"decisions": ["Keep the public API"], "evidence": ["Original trace"]}
        coordinator.save(self.fixture.directory, state)
        with mock.patch.dict(os.environ, {"FAKE_NO_CHANGE": "1"}):
            _, investigation = self.fixture.start("explore", "investigation")
            code, run = self.fixture.start("consult", "consultation", "--packet", str(self.fixture.packet))
        self.assertEqual((code, run["runner_status"]), (0, "completed"))
        spec = json.loads((Path(run["result_path"]).parent.parent / "task-spec.json").read_text())
        self.assertEqual(spec["context_packet"]["decisions"][0], "Keep the public API")
        self.assertEqual(
            spec["context_packet"]["evidence"],
            [
                "Original trace",
                "Callback ran after cancellation",
                "Prior investigation result: " + investigation["result_path"],
            ],
        )

    def test_native_investigator_uses_the_channel_and_completes_its_audit(self):
        code, run = self.fixture.call(
            "dispatch",
            "--kind",
            "investigation",
            "--request-id",
            "native-research",
            "--transport",
            "native",
            "--engine",
            "claude",
            "--model",
            "sonnet",
        )
        self.assertEqual((code, run["status"]), (0, "reserved"))
        packet = self.fixture.fixture.external_path / "native-receipt.json"
        try:
            packet.write_text(json.dumps({"native_handle": "research-session"}))
            self.assertEqual(
                self.fixture.call("native-attach", "--run-id", run["run_id"], "--packet", str(packet))[0], 0
            )
            channel = Path(run["channel_path"])
            COMM.send(channel, sender="worker", message_id="q1", kind="question", text="Should I trace the callback?")
            self.assertEqual(self.fixture.call("next")[1]["messages"][0]["text"], "Should I trace the callback?")
            COMM.send(
                channel,
                sender="conductor",
                message_id="a1",
                kind="reply",
                reply_to="q1",
                text="Yes, inspect its cancellation path.",
            )
            self.assertEqual(
                COMM.wait(channel, recipient="worker", timeout=0.1, reply_to="q1")["messages"][0]["text"],
                "Yes, inspect its cancellation path.",
            )
            report = self.fixture.fixture.external_path / "native-investigation.md"
            report.write_text(
                "### STATUS\nCOMPLETE\n### FILES CHANGED\nnone\n### COMMANDS RUN\nInspection only\n### VERIFICATION\nCallback has no cancellation guard.\n### RISKS OR BLOCKERS\nNeeds a scoped implementation.\n"
            )
            packet.write_text(
                json.dumps(
                    {
                        "native_handle": "research-session",
                        "event_id": "native-investigation-done",
                        "terminal": True,
                        "tool_status": "completed",
                        "report_path": str(report),
                    }
                )
            )
            code, finished = self.fixture.call("native-complete", "--run-id", run["run_id"], "--packet", str(packet))
            self.assertEqual((code, finished["runner_status"]), (0, "completed"))
            with self.assertRaisesRegex(ValueError, "closed"):
                COMM.send(channel, sender="conductor", message_id="late", kind="update", text="Too late")
        finally:
            if test_coordination.COORDINATOR.NATIVE.process_state(run) == "active":
                os.kill(run["pid"], 15)


if __name__ == "__main__":
    unittest.main()
