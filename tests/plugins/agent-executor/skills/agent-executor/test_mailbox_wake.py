"""Observable wake behavior: native idle activation, busy deferral and durable fallback."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/agent-executor/skills/agent-executor/scripts"
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
    def test_cross_project_delivery_does_not_activate_unrelated_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(base, session="target", harness="codex", cwd=temporary)
            with patch.object(mailbox_wake, "wake", side_effect=AssertionError("must not wake another project")):
                sent = peer_mailbox.send(
                    base, sender=sender, to="target", text="Coordination request", scope="cross-project", wake=True
                )
            self.assertEqual(sent["wake"]["status"], "queued")
            self.assertEqual(
                [m["text"] for m in peer_mailbox.take(base, "target", scope="cross-project")], ["Coordination request"]
            )

    def test_idle_receiver_starts_existing_thread_without_policy_or_model_overrides(self):
        native = NativeQueue()
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            outcome = mailbox_wake.wake({"harness": "codex", "session": "target"}, "hello-1")
        self.assertEqual(outcome["status"], "scheduled")
        self.assertEqual(native.started[0]["threadId"], "target")
        self.assertEqual(native.started[0]["clientUserMessageId"], "peer-mailbox-hello-1")
        self.assertEqual(set(native.started[0]), {"threadId", "clientUserMessageId", "input"})
        self.assertIn("not the user", native.started[0]["input"][0]["text"])

    def test_busy_receiver_defers_and_idle_receiver_activates(self):
        native = NativeQueue(state="active")
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            self.assertEqual(mailbox_wake.wake({"harness": "codex", "session": "target"}, "one")["status"], "queued")
            self.assertEqual(native.pending, [])
            self.assertEqual(native.started, [])
            native.state = "idle"
            self.assertEqual(mailbox_wake.wake({"harness": "codex", "session": "target"}, "two")["status"], "scheduled")
        self.assertEqual(native.started[0]["clientUserMessageId"], "peer-mailbox-two")

    def test_busy_race_is_owned_by_native_queue_admission(self):
        native = NativeQueue(race=True)
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            outcome = mailbox_wake.wake({"harness": "codex", "session": "target"}, "one")
        self.assertEqual(outcome["status"], "scheduled")
        self.assertEqual(native.pending[0]["threadId"], "target")

    def test_missing_loaded_session_never_resumes_a_second_copy(self):
        native = NativeQueue(loaded=False)
        with patch.object(mailbox_wake, "CodexSocket", return_value=native):
            self.assertEqual(
                mailbox_wake.wake({"harness": "codex", "session": "target"}, "one")["status"], "unavailable"
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
            self.assertEqual(command[:4], ["opencode", "api", "POST", "/api/session/ses_target/prompt"])
            body = json.loads(command[-1])
            self.assertEqual(body["delivery"], "queue")
            self.assertEqual(body["resume"], True)
            self.assertEqual(body["id"], "msg_mailbox_hello_1")
            return type("Result", (), {"returncode": 0})()

        with patch.object(mailbox_wake.subprocess, "run", side_effect=accept):
            self.assertEqual(
                mailbox_wake.wake({"harness": "opencode", "session": "ses_target"}, "hello-1")["status"], "scheduled"
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
            self.assertEqual(loud["wake"]["status"], "unsupported")
            self.assertNotIn("wake", quiet)
            self.assertEqual([m["text"] for m in peer_mailbox.take(base, "target")], ["hello", "later"])


if __name__ == "__main__":
    unittest.main()
