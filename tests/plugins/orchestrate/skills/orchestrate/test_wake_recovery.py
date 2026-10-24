"""Bounded recovery for wakes that did not reach an idle recipient."""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/orchestrate/skills/orchestrate/scripts"
sys.path.insert(0, str(SCRIPTS))
import mailbox_wake  # noqa: E402
import peer_mailbox  # noqa: E402
import wake_recovery  # noqa: E402

NOT_SENT = {"status": "unavailable", "reason": "offline", "phase": "not-sent"}
SUBMITTED = {"status": "scheduled", "reason": "taken", "phase": "submitted"}
UNKNOWN = {"status": "unavailable", "reason": "timed out after connecting", "phase": "unknown"}


class FakeWake:
    def __init__(self, result=SUBMITTED):
        self.result = result
        self.calls = []

    def __call__(self, target, message_id, *, base=None, retry=False):
        self.calls.append((target["session"], message_id, retry))
        return dict(self.result)


class WakeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.sender = peer_mailbox.register(self.base, session="sender", harness="codex", cwd=str(SCRIPTS))
        peer_mailbox.register(
            self.base, session="target", harness="claude", cwd=str(SCRIPTS), pid=os.getpid(), state="idle"
        )
        self.start = peer_mailbox.now()

    def send(self, text, first=NOT_SENT):
        with patch.object(mailbox_wake, "wake", return_value=dict(first)):
            return peer_mailbox.send(self.base, sender=self.sender, to="target", text=text, wake=True)

    def intent(self):
        return peer_mailbox.load(wake_recovery.intent_path(self.base, "target"))

    def at(self, seconds, wake):
        return wake_recovery.reconcile(self.base, moment=self.start + dt.timedelta(seconds=seconds), wake=wake)

    def test_quiet_mail_never_creates_an_intent(self):
        peer_mailbox.send(self.base, sender=self.sender, to="target", text="fyi", wake=False)
        self.assertIsNone(self.intent())

    def test_a_wake_that_sent_nothing_is_retried_once_due_and_the_recipient_is_idle(self):
        sent = self.send("hello")
        self.assertEqual((self.intent()["state"], self.intent()["messages"]), ("pending", [sent["id"]]))
        wake = FakeWake()
        self.assertEqual(self.at(1, wake), [])  # not due yet
        outcome = self.at(3, wake)
        self.assertEqual(wake.calls, [("target", sent["id"], True)])
        self.assertEqual(outcome[0]["outcome"], "attempted")
        self.assertEqual((self.intent()["state"], self.intent()["attempts"]), ("submitted", 1))

    def test_reading_the_mail_retires_the_intent(self):
        sent = self.send("hello")
        peer_mailbox.mark_read(self.base, "target", [sent["id"]])
        self.assertEqual(self.at(3, FakeWake())[0]["outcome"], "delivered")
        self.assertIsNone(self.intent())

    def test_a_busy_or_parked_recipient_is_not_retried_and_spends_no_attempt(self):
        self.send("hello")
        wake = FakeWake()
        peer_mailbox.annotate(self.base, "target", state="busy")
        self.assertEqual(self.at(3, wake)[0]["outcome"], "waiting")
        seconds = 10
        for stop in ("USER_CANCELED", "MAX_INVOCATIONS", "ERROR"):
            with self.subTest(stop=stop):
                peer_mailbox.annotate(self.base, "target", state="idle", lastStopReason=stop)
                waiting = self.at(seconds, wake)[0]
                self.assertIn(stop, waiting["reason"])
                seconds += 10
        self.assertEqual((wake.calls, self.intent()["attempts"]), ([], 0))

    def test_wake_retry_for_one_peer_never_attempts_another(self):
        store = Path(self.temporary.name) / "cli"
        base = store / "mailbox-v1"
        sender = peer_mailbox.register(base, session="sender", harness="codex", cwd=str(SCRIPTS))
        for session in ("target", "bystander"):
            peer_mailbox.register(
                base, session=session, harness="claude", cwd=str(SCRIPTS), pid=os.getpid(), state="idle"
            )
            with patch.object(mailbox_wake, "wake", return_value=dict(NOT_SENT)):
                peer_mailbox.send(base, sender=sender, to=session, text="hi", wake=True)
            path = wake_recovery.intent_path(base, session)
            peer_mailbox.write(path, {**peer_mailbox.load(path), "nextAt": "2000-01-01T00:00:00Z"})  # both due
        wake = FakeWake()
        with (
            patch.dict("os.environ", {"AGENT_EXECUTOR_HOME": str(store)}),
            patch.object(mailbox_wake, "wake", wake),
        ):
            code = peer_mailbox.main(["wake-retry", "--to", "target", "--session", "sender", "--harness", "codex"])
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in wake.calls], ["target"])
        self.assertEqual(peer_mailbox.load(wake_recovery.intent_path(base, "bystander"))["attempts"], 0)

    def test_wake_retry_does_not_override_a_parked_stop(self):
        self.send("hello")
        wake = FakeWake(NOT_SENT)
        for seconds in range(3, 3 + 301 * wake_recovery.MAX_ATTEMPTS + 1, 301):
            self.at(seconds, wake)
        self.assertEqual(self.intent()["state"], "recovery-needed")
        peer_mailbox.annotate(self.base, "target", lastStopReason="USER_CANCELED")
        wake_recovery.reopen(self.base, ["target"])
        calls = len(wake.calls)
        soon = peer_mailbox.now() + dt.timedelta(seconds=5)  # reopen restarts the clock from the real now
        self.assertEqual(wake_recovery.reconcile(self.base, moment=soon, wake=wake)[0]["outcome"], "waiting")
        self.assertEqual(len(wake.calls), calls)

    def test_a_submitted_notice_that_started_nothing_is_resubmitted_after_its_window(self):
        sent = self.send("hello", first=SUBMITTED)
        marker = self.base / "wakes" / peer_mailbox.session_key("target") / "claude-wake.json"
        peer_mailbox.write(marker, {"id": sent["id"], "requestedAt": peer_mailbox.stamp(self.start)})
        wake = FakeWake()
        self.assertEqual(self.at(10, wake), [])  # still inside the accept window
        self.assertTrue(marker.exists())
        self.at(25, wake)
        self.assertEqual(wake.calls, [("target", sent["id"], True)])
        self.assertFalse(marker.exists())  # lifted, so the adapter really submits again

    def test_an_attempt_that_may_have_submitted_waits_out_the_window_like_a_submission(self):
        sent = self.send("hello", first=UNKNOWN)
        self.assertEqual(self.intent()["state"], "submitted")
        wake = FakeWake(UNKNOWN)
        self.assertEqual(self.at(3, wake), [])  # no quick resend that could duplicate a delivered notice
        self.at(25, wake)
        self.assertEqual(wake.calls, [("target", sent["id"], True)])
        self.assertEqual(self.intent()["state"], "submitted")
        self.assertEqual(self.at(30, wake), [])

    def test_limits_mark_recovery_needed_until_wake_retry_reopens(self):
        self.send("hello")
        wake = FakeWake(NOT_SENT)
        seconds = 3
        for _ in range(wake_recovery.MAX_ATTEMPTS):
            self.assertEqual(self.at(seconds, wake)[0]["outcome"], "attempted")
            seconds += max(wake_recovery.BACKOFF) + 1  # just past the longest backoff
        self.assertEqual(self.at(seconds, wake)[0]["outcome"], "recovery-needed")
        calls = len(wake.calls)
        self.assertEqual(self.at(seconds + 60, wake), [])
        self.assertEqual(len(wake.calls), calls)
        self.assertEqual(wake_recovery.reopen(self.base, ["target"]), ["target"])
        self.assertEqual((self.intent()["state"], self.intent()["attempts"]), ("pending", 0))

    def test_an_old_intent_needs_recovery_rather_than_retrying_forever(self):
        self.send("hello")
        outcome = self.at(wake_recovery.MAX_AGE.total_seconds() + 60, FakeWake())
        self.assertEqual(outcome[0]["outcome"], "recovery-needed")

    def test_a_recipient_that_is_gone_retires_the_intent(self):
        self.send("hello")
        peer_mailbox.unregister(self.base, "target")
        self.assertEqual(self.at(3, FakeWake())[0]["outcome"], "recipient gone")
        self.assertIsNone(self.intent())

    def test_a_retried_claude_wake_waits_out_a_session_waiting_for_input(self):
        self.send("hello")
        message = peer_mailbox.incoming(self.base, "target")[0]["id"]
        record = (self.base, {"sessionId": "target", "status": "waiting", "pid": os.getpid()})
        with patch.object(mailbox_wake, "claude_record", return_value=record):
            first = mailbox_wake.wake({"session": "target", "harness": "claude"}, message, base=self.base)
            retry = mailbox_wake.wake({"session": "target", "harness": "claude"}, message, base=self.base, retry=True)
        self.assertNotEqual(first["reason"], retry["reason"])
        self.assertEqual((retry["status"], retry["phase"]), ("queued", "not-sent"))

    def test_antigravity_hooks_record_a_user_cancel_and_clear_it_on_the_next_turn(self):
        payload = {"conversationId": "agy", "workspacePaths": [str(SCRIPTS)]}
        with patch.dict("os.environ", {}, clear=True), patch.object(peer_mailbox, "harness_pid", return_value=None):
            peer_mailbox.hook({**payload, "terminationReason": "USER_CANCELED"}, harness="gemini", base=self.base)
            stopped = peer_mailbox.load(peer_mailbox.peer_path(self.base, "agy"))
            peer_mailbox.hook({**payload, "invocationNum": 1}, harness="gemini", base=self.base)
            resumed = peer_mailbox.load(peer_mailbox.peer_path(self.base, "agy"))
        self.assertEqual(stopped["lastStopReason"], "USER_CANCELED")
        self.assertIsNone(resumed["lastStopReason"])

    def test_the_daemon_loop_reconciles_until_it_stops(self):
        stopping = threading.Event()
        passes = threading.Event()
        with (
            patch.object(wake_recovery, "RECONCILE_EVERY", 0.01),
            patch.object(wake_recovery, "reconcile", side_effect=lambda base: passes.set()),
        ):
            loop = threading.Thread(target=wake_recovery.run, args=(self.base, stopping))
            loop.start()
            self.assertTrue(passes.wait(2))
            stopping.set()
            loop.join(2)
        self.assertFalse(loop.is_alive())


if __name__ == "__main__":
    unittest.main()
