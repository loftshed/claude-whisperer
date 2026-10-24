"""Work notes remain useful after their author disappears, without becoming a transcript."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/orchestrate/skills/orchestrate/scripts"
sys.path.insert(0, str(SCRIPTS))
import mailbox_tasks as tasks  # noqa: E402
import peer_mailbox as mailbox  # noqa: E402


class TaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.owner = mailbox.register(self.base, session="author", harness="codex", cwd=str(SCRIPTS), pid=os.getpid())
        self.owner = mailbox.annotate(self.base, "author", focus="Repair export", branch="fix/export")
        self.reader = mailbox.register(self.base, session="reader", harness="claude", cwd=str(SCRIPTS), pid=os.getpid())

    def test_latest_note_survives_its_owner_and_has_a_concrete_next_step(self):
        tasks.save(self.base, self.owner, done="Reproduced empty export", next="Fix serializer", refs="PR #8")
        tasks.save(self.base, self.owner, done="Serializer fixed; focused tests pass", next="Review PR #8")
        mailbox.unregister(self.base, "author")
        result = tasks.listing(self.base, self.reader)
        self.assertEqual(len(result["tasks"]), 1)
        note = result["tasks"][0]
        self.assertEqual(
            [note["task"], note["done"], note["next"], note["refs"], note["ownerPresent"]],
            ["Repair export", "Serializer fixed; focused tests pass", "Review PR #8", "PR #8", False],
        )
        self.assertIn("Next: Review PR #8", tasks.render(result))
        self.assertIn("Where:", tasks.render(result))

    def test_a_new_task_clears_old_progress_and_completion(self):
        tasks.save(self.base, self.owner, status="complete", done="Export fixed", next="", refs="PR #8")
        self.owner["focus"] = "Investigate import"
        tasks.save(self.base, self.owner)
        note = tasks.listing(self.base, self.reader)["tasks"][0]
        self.assertEqual([note["task"], note["status"]], ["Investigate import", "active"])
        self.assertNotIn("done", note)
        self.assertNotIn("refs", note)

    def test_long_notes_are_rejected_without_losing_the_previous_checkpoint(self):
        tasks.save(self.base, self.owner, next="Review serialization")
        for details in ({"next": "a" * 241}, {"done": []}, {"status": "abandoned"}, {"unknown": "x"}):
            with self.subTest(details=details), self.assertRaises(mailbox.MailboxError):
                tasks.save(self.base, self.owner, **details)
        self.assertEqual(tasks.listing(self.base, self.reader)["tasks"][0]["next"], "Review serialization")

    def test_project_scope_and_pagination_keep_notes_discoverable(self):
        tasks.save(self.base, self.owner, next="Review")
        outsider = {**self.owner, "session": "elsewhere", "repo": "/another-project", "remotes": [], "focus": "Other"}
        tasks.save(self.base, outsider, next="Other work")
        self.assertEqual([n["task"] for n in tasks.listing(self.base, self.reader)["tasks"]], ["Repair export"])
        first = tasks.listing(self.base, self.reader, scope="cross-project", limit=1)
        second = tasks.listing(self.base, self.reader, scope="cross-project", limit=1, offset=first["next_offset"])
        self.assertEqual([first["tasks"][0]["task"], second["tasks"][0]["task"]], ["Other", "Repair export"])
        self.assertFalse(second["has_more"])

    def test_expired_notes_are_removed_and_updates_are_quiet(self):
        tasks.save(self.base, self.owner, next="Review")
        with patch.object(mailbox, "now", return_value=mailbox.now() + dt.timedelta(days=31)):
            tasks.prune(self.base)
        tasks.save(self.base, self.reader | {"focus": "Fresh task"}, next="Start")
        result = tasks.listing(self.base, self.reader)
        self.assertEqual([n["task"] for n in result["tasks"]], ["Fresh task"])
        self.assertEqual(list((self.base / "tasks").glob("*/*.json")), [tasks.path_for(self.base, self.reader)])
        self.assertFalse((self.base / "inbox").exists())
        self.assertFalse((self.base / "wakes").exists())


class TaskInterfaceTests(unittest.TestCase):
    def test_focus_and_tasks_work_through_mcp_after_the_author_leaves(self):
        import test_mailbox_mcp_contract

        client = test_mailbox_mcp_contract.ContractTests()
        client.setUp()
        self.addCleanup(client.doCleanups)
        updated = client.tool(
            "focus",
            {"text": "Repair export", "done": "6 tests pass", "next": "Review PR #8", "status": "blocked"},
        )["result"]
        self.assertEqual(updated["structuredContent"]["focus"], "Repair export")
        client.tool("focus", {"text": "Repair export", "blocker": "Waiting on review"})
        mailbox.unregister(client.base, "fixture-reader")
        result = client.tool("tasks", session="fixture-sender")["result"]
        note = result["structuredContent"]["tasks"][0]
        self.assertEqual(
            [note["task"], note["done"], note["next"], note["blocker"], note["ownerPresent"]],
            ["Repair export", "6 tests pass", "Review PR #8", "Waiting on review", False],
        )
        self.assertIn("Next: Review PR #8", result["content"][0]["text"])

    def test_cli_checkpoint_and_lookup(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"AGENT_EXECUTOR_HOME": temporary}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = mailbox.main(
                    [
                        "focus",
                        "--session",
                        "cli-author",
                        "--harness",
                        "codex",
                        "--text",
                        "Repair export",
                        "--done",
                        "Tests pass",
                        "--next",
                        "Review",
                        "--status",
                        "active",
                    ]
                )
            self.assertEqual(status, 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = mailbox.main(["tasks", "--format", "json"])
            self.assertEqual(status, 0)
            note = json.loads(output.getvalue())["tasks"][0]
            self.assertEqual([note["task"], note["done"], note["next"]], ["Repair export", "Tests pass", "Review"])


if __name__ == "__main__":
    unittest.main()
