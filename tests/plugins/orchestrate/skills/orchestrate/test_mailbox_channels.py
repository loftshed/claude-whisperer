"""Task channels: a post reaches its channel's members, and other teams stay quiet."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/orchestrate/skills/orchestrate/scripts"
sys.path.insert(0, str(SCRIPTS))
import mailbox_channels  # noqa: E402
import mailbox_wake  # noqa: E402
import peer_mailbox  # noqa: E402

GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.base = root / "mailbox-v1"
        self.repo = root / "archive"
        subprocess.run([*GIT, "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run([*GIT, "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "start"], check=True)
        self.worktree = root / "archive-graph"
        subprocess.run(
            [*GIT, "-C", str(self.repo), "worktree", "add", "-q", str(self.worktree), "-b", "graph"], check=True
        )
        self.elsewhere = root / "unrelated"
        subprocess.run([*GIT, "init", "-q", "-b", "main", str(self.elsewhere)], check=True)
        self.patches = patch.dict(os.environ, {}, clear=False)
        self.patches.start()
        self.addCleanup(self.patches.stop)
        os.environ.pop(mailbox_channels.LAUNCH_VARIABLE, None)

    def peer(self, session, *, harness="codex", cwd=None, channels=(), state="idle"):
        peer = peer_mailbox.register(
            self.base, session=session, harness=harness, cwd=str(cwd or self.repo), pid=os.getpid(), state=state
        )
        if channels:
            peer = mailbox_channels.update(self.base, session, join=list(channels))
        return peer

    def inbox(self, session):
        return [message["text"] for message in peer_mailbox.incoming(self.base, session)]

    def post(self, sender, channel, text, **options):
        return mailbox_channels.post(self.base, sender=sender, channel=channel, text=text, **options)

    def test_a_post_reaches_only_its_channels_members(self):
        fetcher = self.peer("fetcher", channels=["retrieval"])
        self.peer("fetcher-two", channels=["retrieval"])
        self.peer("mapper", channels=["graph"])
        self.peer("bystander")
        result = self.post(fetcher, "retrieval", "census batch 3 is in")
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(self.inbox("fetcher-two"), ["census batch 3 is in"])
        for quiet in ("mapper", "bystander", "fetcher"):
            self.assertEqual(self.inbox(quiet), [], quiet)

    def test_all_reaches_the_project_and_worktree_only_this_checkout(self):
        sender = self.peer("sender", channels=["retrieval"])
        self.peer("main-checkout")
        self.peer("in-worktree", cwd=self.worktree, channels=["graph"])
        self.peer("other-project", cwd=self.elsewhere)
        self.post(sender, "all", "everyone: schema changed")
        self.post(sender, "worktree", "this checkout only")
        self.assertEqual(self.inbox("main-checkout"), ["everyone: schema changed", "this checkout only"])
        self.assertEqual(self.inbox("in-worktree"), ["everyone: schema changed"])
        self.assertEqual(self.inbox("other-project"), [])

    def test_a_replay_keeps_the_frozen_recipients_and_delivers_nothing_twice(self):
        sender = self.peer("sender", channels=["graph"])
        self.peer("first", channels=["graph"])
        self.peer("second", channels=["graph"])
        real_send = peer_mailbox.send
        failed = []

        def flaky(base, **options):
            if options["to"] == "second" and not failed:  # the first attempt to reach `second` fails
                failed.append(True)
                raise peer_mailbox.MailboxError("disk hiccup")
            return real_send(base, **options)

        with patch.object(peer_mailbox, "send", side_effect=flaky):
            first = self.post(sender, "graph", "node schema v2", idempotency_key="schema-v2")
        self.assertEqual(first["delivered"], 1)
        self.peer("latecomer", channels=["graph"])
        again = self.post(sender, "graph", "node schema v2", idempotency_key="schema-v2")
        self.assertEqual(again["postId"], first["postId"])
        self.assertEqual(again["delivered"], 2)
        self.assertEqual(self.inbox("first"), ["node schema v2"])
        self.assertEqual(self.inbox("second"), ["node schema v2"])
        self.assertEqual(self.inbox("latecomer"), [])
        with self.assertRaises(peer_mailbox.MailboxError):
            self.post(sender, "graph", "something else", idempotency_key="schema-v2")

    def test_a_replay_after_the_sender_moves_finishes_in_the_accepted_context(self):
        sender = self.peer("sender", channels=["graph"])
        self.peer("first", channels=["graph"])
        self.peer("second", channels=["graph"])
        real_send = peer_mailbox.send

        def second_down(base, **options):
            if options["to"] == "second":
                raise peer_mailbox.MailboxError("disk hiccup")
            return real_send(base, **options)

        with patch.object(peer_mailbox, "send", side_effect=second_down):
            first = self.post(sender, "graph", "edges ready", idempotency_key="edges")
        moved = self.peer("sender", cwd=self.elsewhere)
        again = self.post(moved, "graph", "edges ready", idempotency_key="edges")
        self.assertEqual(again["postId"], first["postId"])
        self.assertEqual([r.get("error") for r in again["recipients"]], [None, None])
        self.assertEqual(self.inbox("first"), ["edges ready"])
        (copy,) = peer_mailbox.incoming(self.base, "second")
        self.assertEqual(copy["from"]["cwd"], sender["cwd"])
        kept = peer_mailbox.load(mailbox_channels.post_path(self.base, "graph", first["postId"]))
        self.assertEqual(kept["checkout"], sender.get("checkout") or sender["cwd"])
        self.assertNotEqual(moved["cwd"], sender["cwd"])

    def test_a_post_accepted_but_not_yet_published_is_recovered(self):
        sender = self.peer("sender", channels=["graph"])
        self.peer("member", channels=["graph"])
        real_write = peer_mailbox.write

        def crash_on_publish(path, value):
            if "channels" in Path(path).parts:
                raise OSError("power cut")
            return real_write(path, value)

        with patch.object(peer_mailbox, "write", side_effect=crash_on_publish), self.assertRaises(OSError):
            self.post(sender, "graph", "layout v3", idempotency_key="layout")
        self.assertEqual(self.inbox("member"), [])
        result = self.post(sender, "graph", "layout v3", idempotency_key="layout")
        self.assertEqual(result["delivered"], 1)
        (kept,) = mailbox_channels.history(self.base, sender, "graph")["posts"]
        self.assertEqual((kept["postId"], kept["text"]), (result["postId"], "layout v3"))

    def test_an_unreadable_or_expired_key_is_refused(self):
        sender = self.peer("sender", channels=["graph"])
        self.peer("member", channels=["graph"])
        self.post(sender, "graph", "old news", idempotency_key="old")
        (operation,) = (self.base / "channel-ops").glob("*/*.json")
        entry = peer_mailbox.load(operation)
        operation.write_text("{not json")
        with self.assertRaisesRegex(peer_mailbox.MailboxError, "unreadable"):
            self.post(sender, "graph", "old news", idempotency_key="old")
        entry["acceptedAt"] = peer_mailbox.stamp(peer_mailbox.now() - peer_mailbox.MAX_AGE * 2)
        peer_mailbox.write(operation, entry)
        with self.assertRaisesRegex(peer_mailbox.MailboxError, "expired"):
            self.post(sender, "graph", "old news", idempotency_key="old")
        peer_mailbox.prune(self.base)
        tombstone = peer_mailbox.load(operation)
        self.assertEqual(tombstone["schema"], mailbox_channels.EXPIRED_SCHEMA)
        self.assertNotIn("fingerprint", tombstone)
        with self.assertRaisesRegex(peer_mailbox.MailboxError, "expired"):
            self.post(sender, "graph", "old news", idempotency_key="old")
        peer_mailbox.unregister(self.base, "sender")
        peer_mailbox.prune(self.base)
        self.assertFalse(operation.exists())

    def test_replies_to_any_copy_thread_on_the_post(self):
        sender = self.peer("sender", channels=["graph"])
        member = self.peer("member", channels=["graph"])
        posted = self.post(sender, "graph", "which layout?")
        (copy,) = peer_mailbox.incoming(self.base, "member")
        self.assertIn(f'post="{posted["postId"]}"', peer_mailbox.render(copy))
        peer_mailbox.send(self.base, sender=member, to="sender", text="force-directed", reply_to=copy["id"])
        for handle in (posted["postId"], posted["recipients"][0]["id"]):
            with self.subTest(handle=handle):
                found = peer_mailbox.wait(self.base, "sender", timeout=0.5, reply_to=handle, mark=False)
                self.assertEqual([m["replyTo"] for m in found["messages"]], [posted["postId"]])
        self.post(member, "graph", "agreed: force-directed", reply_to=copy["id"])
        replies = [p for p in mailbox_channels.history(self.base, sender, "graph")["posts"] if p["replyTo"]]
        self.assertEqual([p["replyTo"] for p in replies], [posted["postId"]])

    def test_posts_are_quiet_unless_a_wake_is_asked_for(self):
        sender = self.peer("sender", channels=["graph"])
        self.peer("member", channels=["graph"])
        with patch.object(mailbox_wake, "wake", return_value={"status": "scheduled", "phase": "submitted"}) as wake:
            quiet = self.post(sender, "graph", "fyi")
            wake.assert_not_called()
            urgent = self.post(sender, "graph", "blocked: need the edge list", wake=True)
            wake.assert_called_once()
        self.assertFalse(quiet["wakeRequested"])
        self.assertTrue(urgent["wakeRequested"])

    def test_same_account_claude_members_are_reached_through_the_mailbox(self):
        sender = self.peer("claude-one", harness="claude", channels=["graph"])
        self.peer("claude-two", harness="claude", channels=["graph"])
        with self.assertRaises(peer_mailbox.MailboxError) as refused:
            peer_mailbox.send(self.base, sender=sender, to="claude-two", text="direct")
        self.assertEqual(refused.exception.code, "native_required")
        self.assertEqual(self.post(sender, "graph", "channel post")["delivered"], 1)
        self.assertEqual(self.inbox("claude-two"), ["channel post"])

    def test_quiet_channel_posts_never_keep_a_turn_going(self):
        sender = self.peer("sender", channels=["graph"])
        self.peer("worker", harness="claude", channels=["graph"], state="busy")
        payload = {"session_id": "worker", "cwd": str(self.repo)}
        self.post(sender, "graph", "progress note")
        with patch.object(peer_mailbox, "harness_pid", return_value=os.getpid()):
            stopped = peer_mailbox.hook({**payload, "hook_event_name": "Stop"}, harness="claude", base=self.base)
            self.assertIsNone(stopped)
            self.assertEqual(self.inbox("worker"), ["progress note"])  # still unread for the next turn
            started = peer_mailbox.hook(
                {**payload, "hook_event_name": "UserPromptSubmit"}, harness="claude", base=self.base
            )
            self.assertIn("progress note", started["hookSpecificOutput"]["additionalContext"])
            self.post(sender, "graph", "urgent: stop the build", wake=True)
            urgent = peer_mailbox.hook({**payload, "hook_event_name": "Stop"}, harness="claude", base=self.base)
        self.assertEqual(urgent["decision"], "block")
        self.assertIn("urgent: stop the build", urgent["reason"])

    def test_roster_notices_follow_channel_mates_only(self):
        self.peer("mapper", harness="claude", channels=["graph"])
        self.peer("fetcher", channels=["retrieval"])
        payload = {"session_id": "mapper", "cwd": str(self.repo), "hook_event_name": "UserPromptSubmit"}

        def roster():
            with patch.object(peer_mailbox, "harness_pid", return_value=os.getpid()):
                out = peer_mailbox.hook(payload, harness="claude", base=self.base)
            return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

        first = roster()
        self.assertIn("1 in retrieval", first)
        self.peer("fetcher-two", channels=["retrieval"])
        self.assertNotIn("working in this project", roster())  # another team grew: no notice
        self.peer("mapper-two", channels=["graph"])
        self.assertIn("mapper-two", roster())

    def test_session_start_points_a_channel_less_agent_at_existing_channels(self):
        self.peer("fetcher", channels=["retrieval"])
        self.peer("mapper", channels=["graph"])
        with patch.object(peer_mailbox, "harness_pid", return_value=os.getpid()):
            out = peer_mailbox.hook(
                {"session_id": "newcomer", "cwd": str(self.repo), "hook_event_name": "SessionStart"},
                harness="claude",
                base=self.base,
            )
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Task channels in this project: graph (1), retrieval (1)", text)
        self.assertEqual(
            mailbox_channels.memberships(peer_mailbox.load(peer_mailbox.peer_path(self.base, "newcomer"))), []
        )

    def test_history_is_explicit_paginated_and_project_scoped(self):
        sender = self.peer("sender", channels=["graph"])
        reader = self.peer("reader")  # not a member: may still read on request
        outsider = self.peer("outsider", cwd=self.elsewhere)
        for number in range(3):
            self.post(sender, "graph", f"note {number}")
        page = mailbox_channels.history(self.base, reader, "graph", limit=2)
        self.assertEqual([post["text"] for post in page["posts"]], ["note 2", "note 1"])
        self.assertTrue(page["more"])
        older = mailbox_channels.history(self.base, reader, "graph", limit=2, before=page["posts"][-1]["postId"])
        self.assertEqual([post["text"] for post in older["posts"]], ["note 0"])
        self.assertEqual(mailbox_channels.history(self.base, outsider, "graph")["posts"], [])
        self.assertEqual(self.inbox("reader"), [])  # reading delivers nothing

    def test_membership_changes_only_when_asked(self):
        self.peer("agent")
        peer_mailbox.set_focus(self.base, "agent", "Fetching parish books", channels=["retrieval"])
        peer_mailbox.set_focus(self.base, "agent", "Still fetching parish books")
        self.assertEqual(
            mailbox_channels.memberships(peer_mailbox.load(peer_mailbox.peer_path(self.base, "agent"))), ["retrieval"]
        )
        os.environ[mailbox_channels.LAUNCH_VARIABLE] = "graph, not valid!, all"
        launched = self.peer("helper")
        self.assertEqual(mailbox_channels.memberships(launched), ["graph"])
        mailbox_channels.update(self.base, "helper", leave=["graph"])
        refreshed = self.peer("helper")  # a later refresh never undoes the leave
        self.assertEqual(mailbox_channels.memberships(refreshed), [])

    def test_leaving_stops_future_posts(self):
        sender = self.peer("sender", channels=["graph"])
        self.peer("member", channels=["graph"])
        self.post(sender, "graph", "before")
        mailbox_channels.update(self.base, "member", leave=["graph"])
        self.post(sender, "graph", "after")
        self.assertEqual(self.inbox("member"), ["before"])

    def test_reserved_and_invalid_names_are_refused(self):
        self.peer("agent")
        for bad in ("all", "worktree", "Has Caps", "", "x" * 41):
            with self.subTest(name=bad), self.assertRaises(peer_mailbox.MailboxError):
                mailbox_channels.update(self.base, "agent", join=[bad])

    def test_clear_keeps_the_sessions_channels(self):
        old = self.peer("before-clear", harness="claude", channels=["graph"])
        peer_mailbox.end_session(self.base, old["session"], reason="clear", pid=os.getpid())
        successor = peer_mailbox.register(
            self.base, session="after-clear", harness="claude", cwd=str(self.repo), pid=os.getpid()
        )
        notice = peer_mailbox.adopt_cleared(self.base, successor)
        self.assertIn("graph", notice)
        kept = peer_mailbox.load(peer_mailbox.peer_path(self.base, "after-clear"))
        self.assertEqual(mailbox_channels.memberships(kept), ["graph"])

    def test_the_cli_posts_joins_and_lists(self):
        self.peer("member", channels=["graph"])
        store = Path(self.temporary.name)
        common = ["--session", "cli-agent", "--harness", "codex"]
        with (
            patch.dict(os.environ, {"AGENT_EXECUTOR_HOME": str(store)}),
            patch("os.getcwd", return_value=str(self.repo)),
        ):
            self.assertEqual(peer_mailbox.main(["join", "graph", *common]), 0)
            self.assertEqual(peer_mailbox.main(["send", "--channel", "graph", "--text", "via cli", *common]), 0)
            self.assertEqual(peer_mailbox.main(["channels", *common, "--format", "json"]), 0)
            self.assertNotEqual(
                peer_mailbox.main(["send", "--channel", "graph", "--to", "member", "--text", "x", *common]), 0
            )
        self.assertEqual(self.inbox("member"), ["via cli"])


if __name__ == "__main__":
    unittest.main()
