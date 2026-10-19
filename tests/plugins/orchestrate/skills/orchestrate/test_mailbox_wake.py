"""Observable wake behavior: native idle activation, busy deferral and durable fallback."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/orchestrate/skills/orchestrate/scripts"
sys.path.insert(0, str(SCRIPTS))
import mailbox_mcp  # noqa: E402
import mailbox_wake  # noqa: E402
import peer_mailbox  # noqa: E402


class NativeQueue:
    def __init__(self, state="idle", loaded=True, race=False):
        self.state = state
        self.loaded = loaded
        self.race = race
        self.pending = []
        self.started = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def request(self, method, params):
        if method == "thread/loaded/list":
            return {"data": ["target"] if self.loaded else [], "nextCursor": None}
        if method == "thread/queue/list":
            return {"data": self.pending, "nextCursor": None}
        if method == "thread/read":
            return {"thread": {"status": {"type": self.state}}}
        if method == "thread/queue/add":
            self.pending.append(params)
            if not self.race:
                self.started.append(self.pending.pop(0))
                self.state = "active"
            return {"queuedSubmission": {"id": "queue-1"}}
        raise AssertionError(f"Unexpected native operation: {method}")


class MailboxWakeTests(unittest.TestCase):
    def test_burst_requests_share_one_pending_turn_and_all_mail_is_delivered(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="target", harness="codex", cwd=str(SCRIPTS))
            native = NativeQueue(race=True)
            with patch.object(mailbox_wake, "CodexSocket", return_value=native):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(
                        pool.map(
                            lambda i: peer_mailbox.send(
                                base, sender=sender, to="target", text=f"Request {i}", wake=True
                            ),
                            range(4),
                        )
                    )
            self.assertEqual([r["wake"]["status"] for r in results], ["scheduled"] * 4)
            self.assertEqual(len(native.pending), 1)
            self.assertEqual(native.pending[0]["threadId"], "target")
            self.assertEqual(
                sorted(m["text"] for m in peer_mailbox.take(base, "target")),
                ["Request 0", "Request 1", "Request 2", "Request 3"],
            )
            native.pending.clear()
            with patch.object(mailbox_wake, "CodexSocket", return_value=native):
                later = peer_mailbox.send(base, sender=sender, to="target", text="Next request", wake=True)
            self.assertEqual(later["wake"]["status"], "scheduled")
            self.assertEqual(len(native.pending), 1)
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["Next request"])

    def test_consumed_mail_does_not_wake_but_a_new_request_does(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            target = peer_mailbox.register(base, session="target", harness="codex", cwd=str(SCRIPTS))
            old = peer_mailbox.send(base, sender=sender, to="target", text="Already handled")
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["Already handled"])
            native = NativeQueue(race=True)
            with patch.object(mailbox_wake, "CodexSocket", return_value=native):
                result = mailbox_wake.wake(target, old["id"], base=base)
                self.assertEqual(result["status"], "queued")
                self.assertEqual(native.pending, [])
                peer_mailbox.send(base, sender=sender, to="target", text="New question", wake=True)
            self.assertEqual(len(native.pending), 1)
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["New question"])

    def test_mail_consumed_during_queue_inspection_does_not_schedule_a_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="target", harness="codex", cwd=str(SCRIPTS))
            native = NativeQueue(race=True)
            original = native.request
            delivered = []

            def request(method, params):
                if method == "thread/queue/list":
                    delivered.extend(peer_mailbox.take(base, "target"))
                return original(method, params)

            native.request = request
            with patch.object(mailbox_wake, "CodexSocket", return_value=native):
                result = peer_mailbox.send(base, sender=sender, to="target", text="Question", wake=True)
            self.assertEqual(result["wake"]["status"], "queued")
            self.assertEqual(native.pending, [])
            self.assertEqual([m["text"] for m in delivered], ["Question"])

    def test_pending_codex_wake_on_later_page_is_reused(self):
        native = NativeQueue(race=True)
        original = native.request

        def request(method, params):
            if method == "thread/queue/list":
                if params["cursor"] is None:
                    return {"data": [{"clientUserMessageId": "user-prompt"}], "nextCursor": "page-2"}
                self.assertEqual(params["cursor"], "page-2")
                return {"data": [{"clientUserMessageId": "peer-mailbox-previous"}], "nextCursor": None}
            return original(method, params)

        native.request = request
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            result = mailbox_wake.wake_codex({"session": "target"}, "new")
        self.assertEqual(result, {"status": "scheduled", "reason": "mailbox wake already queued for this recipient"})
        self.assertEqual(native.pending, [])

    def test_opencode_burst_reuses_pending_wake_and_delivers_all_mail(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="ses_target", harness="opencode", cwd=str(SCRIPTS))
            pending = []

            def api(command, **_kwargs):
                if command[2] == "GET":
                    return type("Result", (), {"returncode": 0, "stdout": json.dumps({"data": pending})})()
                pending.append(json.loads(command[-1]))
                return type("Result", (), {"returncode": 0})()

            with patch.object(mailbox_wake.subprocess, "run", side_effect=api):
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(
                        pool.map(
                            lambda i: peer_mailbox.send(
                                base, sender=sender, to="ses_target", text=f"Question {i}", wake=True
                            ),
                            range(3),
                        )
                    )
            self.assertEqual([r["wake"]["status"] for r in results], ["scheduled"] * 3)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["delivery"], "queue")
            self.assertEqual(
                sorted(m["text"] for m in peer_mailbox.take(base, "ses_target")),
                ["Question 0", "Question 1", "Question 2"],
            )

    def test_queue_inspection_failure_retains_mail_without_scheduling(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="target", harness="codex", cwd=str(SCRIPTS))
            native = NativeQueue(race=True)
            original = native.request

            def request(method, params):
                if method == "thread/queue/list":
                    raise mailbox_wake.ControlError("unsupported")
                return original(method, params)

            native.request = request
            with patch.object(mailbox_wake, "CodexSocket", return_value=native):
                result = peer_mailbox.send(base, sender=sender, to="target", text="Keep this", wake=True)
            self.assertEqual(result["wake"]["status"], "unavailable")
            self.assertEqual(native.pending, [])
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["Keep this"])

    def test_recipient_starting_during_inspection_keeps_mail_for_the_active_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="target", harness="codex", cwd=str(SCRIPTS))
            native = NativeQueue(race=True)
            original = native.request

            def request(method, params):
                if method == "thread/queue/list":
                    native.state = "active"
                return original(method, params)

            native.request = request
            with patch.object(mailbox_wake, "CodexSocket", return_value=native):
                result = peer_mailbox.send(base, sender=sender, to="target", text="Active turn request", wake=True)
            self.assertEqual(result["wake"]["status"], "queued")
            self.assertEqual(native.pending, [])
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["Active turn request"])

    def test_malformed_queue_results_leave_mail_deliverable(self):
        for harness, session, field in [("codex", "target", "clientUserMessageId"), ("opencode", "ses_target", "id")]:
            for data in [None, [{}], [{field: None}]]:
                with self.subTest(harness=harness, data=data), tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
                    peer_mailbox.register(base, session=session, harness=harness, cwd=str(SCRIPTS))
                    native = NativeQueue(race=True)
                    original = native.request

                    def request(method, params, data=data, original=original):
                        if method == "thread/queue/list":
                            return {"data": data, "nextCursor": None}
                        return original(method, params)

                    native.request = request
                    response = type("Result", (), {"returncode": 0, "stdout": json.dumps({"data": data})})()
                    with (
                        patch.object(mailbox_wake, "CodexSocket", return_value=native),
                        patch.object(mailbox_wake.subprocess, "run", return_value=response),
                    ):
                        result = peer_mailbox.send(base, sender=sender, to=session, text="Retained question", wake=True)
                    self.assertEqual(result["wake"]["status"], "unavailable")
                    self.assertEqual(native.pending, [])
                    self.assertEqual([m["text"] for m in peer_mailbox.take(base, session)], ["Retained question"])

    def test_explicit_cross_project_delivery_uses_normal_wake_and_inbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="target", harness="codex", cwd=temporary)
            with patch.object(mailbox_wake, "CodexSocket", return_value=NativeQueue()):
                sent = peer_mailbox.send(
                    base, sender=sender, to="target", text="Coordination request", scope="cross-project", wake=True
                )
            self.assertEqual(sent["wake"]["status"], "scheduled")
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["Coordination request"])

    def test_idle_receiver_starts_existing_thread_without_policy_or_model_overrides(self):
        native = NativeQueue()
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            outcome = mailbox_wake.wake_codex({"harness": "codex", "session": "target"}, "hello-1")
        self.assertEqual(outcome["status"], "scheduled")
        self.assertEqual(native.started[0]["threadId"], "target")
        self.assertEqual(native.started[0]["clientUserMessageId"], "peer-mailbox-hello-1")
        self.assertEqual(set(native.started[0]), {"threadId", "clientUserMessageId", "input"})
        self.assertIn("not the user", native.started[0]["input"][0]["text"])

    def test_busy_receiver_defers_and_idle_receiver_activates(self):
        native = NativeQueue(state="active")
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            self.assertEqual(
                mailbox_wake.wake_codex({"harness": "codex", "session": "target"}, "one")["status"], "queued"
            )
            self.assertEqual(native.pending, [])
            self.assertEqual(native.started, [])
            native.state = "idle"
            self.assertEqual(
                mailbox_wake.wake_codex({"harness": "codex", "session": "target"}, "two")["status"], "scheduled"
            )
        self.assertEqual(native.started[0]["clientUserMessageId"], "peer-mailbox-two")

    def test_busy_race_is_owned_by_native_queue_admission(self):
        native = NativeQueue(race=True)
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            outcome = mailbox_wake.wake_codex({"harness": "codex", "session": "target"}, "one")
        self.assertEqual(outcome["status"], "scheduled")
        self.assertEqual(native.pending[0]["threadId"], "target")

    def test_missing_loaded_session_never_resumes_a_second_copy(self):
        native = NativeQueue(loaded=False)
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            self.assertEqual(
                mailbox_wake.wake_codex({"harness": "codex", "session": "target"}, "one")["status"], "unavailable"
            )

    def test_unavailable_wake_does_not_lose_mail_and_quiet_delivery_skips_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="target", harness="codex", cwd=str(SCRIPTS))
            with patch.object(mailbox_wake, "CodexSocket", side_effect=OSError("offline")):
                sent = peer_mailbox.send(base, sender=sender, to="target", text="Please review", wake=True)
                quiet = peer_mailbox.send(base, sender=sender, to="target", text="For later", wake=False)
            self.assertEqual(sent["wake"]["status"], "unavailable")
            self.assertNotIn("wake", quiet)
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["Please review", "For later"])

    def test_opencode_uses_durable_queue_admission(self):
        def accept(command, **_kwargs):
            if command[2] == "GET":
                return type("Result", (), {"returncode": 0, "stdout": '{"data": []}'})()
            self.assertEqual(command[:4], ["opencode", "api", "POST", "/api/session/ses_target/prompt"])
            body = json.loads(command[-1])
            self.assertEqual(body["delivery"], "queue")
            self.assertEqual(body["resume"], True)
            self.assertEqual(body["id"], "msg_mailbox_hello_1")
            return type("Result", (), {"returncode": 0})()

        with patch.object(mailbox_wake.subprocess, "run", side_effect=accept):
            self.assertEqual(
                mailbox_wake.wake_opencode({"harness": "opencode", "session": "ses_target"}, "hello-1")["status"],
                "scheduled",
            )

    def test_mcp_sender_can_choose_quiet_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            peer_mailbox.register(base, session="target", harness="gemini", cwd=str(SCRIPTS))
            server = mailbox_mcp.Server(base=base, environ={})
            server.harness = "codex"
            loud = json.loads(server.call("send", {"to": "target", "text": "hello"}, {"sessionId": "sender"}))
            quiet = json.loads(
                server.call("send", {"to": "target", "text": "later", "wake": False}, {"sessionId": "sender"})
            )
            self.assertEqual(loud["wake"]["status"], "unavailable")
            self.assertNotIn("wake", quiet)
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["hello", "later"])


if __name__ == "__main__":
    unittest.main()
