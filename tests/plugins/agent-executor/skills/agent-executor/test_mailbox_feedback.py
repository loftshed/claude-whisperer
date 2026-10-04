from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_mailbox

MAILBOX = test_mailbox.MAILBOX
FEEDBACK = MAILBOX.feedback_module()
SERVER = test_mailbox.RUNNER.bundled_module("mailbox_mcp")


class MailboxFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.base = self.directory / "cache" / "mailbox-v1"
        self.collector = self.directory / "collector"
        self.environment = mock.patch.dict(os.environ, {"AGENT_EXECUTOR_FEEDBACK_DIR": str(self.collector)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)
        self.repository = self.directory / "project"
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        self.peer = MAILBOX.register(
            self.base,
            session="reporting-session",
            harness="codex",
            cwd=str(self.repository),
            pid=os.getpid(),
            cwd_source="hook",
        )

    def reports(self):
        return [json.loads(path.read_text()) for path in sorted(self.collector.glob("feedback-*.json"))]

    def submit(self, **changes):
        arguments = {
            "category": "efficiency",
            "operation": "wait",
            "intent": "Get a single answer to a blocking question",
            "problem": "I kept checking the inbox without knowing when to stop",
            "needed": "A bounded wait for the matching reply",
        }
        return FEEDBACK.submit(self.base, self.peer, **(arguments | changes))

    def test_agent_reports_capture_needs_once_without_messaging_or_changing_presence(self):
        before = MAILBOX.load(MAILBOX.peer_path(self.base, self.peer["session"]))
        first = self.submit()
        second = self.submit()
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second, {"id": first["id"], "status": "already_recorded", "occurrences": 1})
        self.assertEqual(len(self.reports()), 1)
        report = self.reports()[0]
        self.assertEqual(report["kind"], "agent-report")
        self.assertEqual(report["needed"], "A bounded wait for the matching reply")
        self.assertEqual(report["reporter"]["harness"], "codex")
        self.assertNotIn("reporting-session", json.dumps(report))
        self.assertNotIn(str(self.repository), json.dumps(report))
        self.assertEqual(MAILBOX.load(MAILBOX.peer_path(self.base, self.peer["session"])), before)
        self.assertEqual(list(self.base.glob("inbox/**/*.json")), [])

    def test_configured_repository_collector_works_from_another_project(self):
        config = self.directory / "config.json"
        config.write_text(json.dumps({"mailbox_feedback": {"directory": str(self.collector)}}))
        with mock.patch.dict(os.environ, {"AGENT_EXECUTOR_CONFIG": str(config)}):
            os.environ.pop("AGENT_EXECUTOR_FEEDBACK_DIR")
            response = test_mailbox.mailbox_cli(
                self.base,
                "feedback",
                "--session",
                self.peer["session"],
                "--harness",
                "codex",
                "--category",
                "routing",
                "--intent",
                "Find the project owner",
                "--problem",
                "The address was ambiguous",
                "--needed",
                "A stable owner address",
                "--operation",
                "send",
                cwd=self.repository,
                check=True,
            )
        self.assertEqual(json.loads(response.stdout)["status"], "recorded")
        self.assertEqual(self.reports()[0]["needed"], "A stable owner address")
        result = test_mailbox.mailbox_cli(
            self.base, "feedback-summary", "--scope", "cross-project", "--format", "json", check=True
        )
        summary = json.loads(result.stdout)
        self.assertEqual(summary["agentReports"], 1)
        self.assertEqual(summary["needs"][0]["problem"], "The address was ambiguous")

    def test_obvious_secrets_paths_and_urls_are_redacted(self):
        self.submit(
            problem="token=example-private-token Bearer example-auth /Users/person/private/file https://user:pass@private.test/repo t@example.test"
        )
        report = self.reports()[0]
        self.assertEqual(
            report["problem"],
            "token=[redacted] Bearer [redacted] [redacted-path] [redacted-url] [redacted-email]",
        )
        for private in ("example-private-token", "example-auth", "person/private", "user:pass", "t@example.test"):
            self.assertNotIn(private, json.dumps(report))

    def test_invalid_reports_do_not_write_partial_data(self):
        for changes in (
            {"intent": " "},
            {"needed": "x" * 801},
            {"category": "invalid"},
            {"message_ids": ["private text"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(FEEDBACK.FeedbackError):
                self.submit(**changes)
        self.assertEqual(self.reports(), [])
        self.assertEqual(self.submit()["status"], "recorded")

    def test_summary_requires_explicit_cross_project_scope_to_show_other_needs(self):
        self.submit()
        other_directory = self.directory / "separate-project"
        subprocess.run(["git", "init", "-q", str(other_directory)], check=True)
        other = MAILBOX.register(
            self.base, session="other-reporter", harness="claude", cwd=str(other_directory), pid=os.getpid()
        )
        FEEDBACK.submit(
            self.base,
            other,
            category="routing",
            intent="Identify a contract owner",
            problem="Other project problem",
            needed="Other project needs",
        )
        own = test_mailbox.mailbox_cli(
            self.base, "feedback-summary", "--format", "json", cwd=self.repository, check=True
        )
        self.assertEqual(json.loads(own.stdout)["reports"], 1)
        self.assertEqual(json.loads(own.stdout)["needs"][0]["needed"], "A bounded wait for the matching reply")
        self.assertNotIn("Other project", own.stdout)
        broad = test_mailbox.mailbox_cli(
            self.base,
            "feedback-summary",
            "--scope",
            "cross-project",
            "--format",
            "json",
            cwd=self.repository,
            check=True,
        )
        self.assertEqual(json.loads(broad.stdout)["reports"], 2)
        self.assertIn("Other project needs", broad.stdout)

    def test_mcp_feedback_is_available_and_is_not_delivered_as_a_peer_message(self):
        server = SERVER.Server(base=self.base, environ={})
        server.identify({"name": "codex-mcp-client"})
        request = test_mailbox.McpServerTests.call(
            "feedback",
            {
                "category": "context",
                "intent": "Stay within the current project",
                "problem": "A stale notification described unrelated work",
                "needed": "Route notifications using the current project identity",
            },
            session=self.peer["session"],
        )
        result = server.handle({"jsonrpc": "2.0", "id": 1, **request})
        self.assertEqual(json.loads(result["result"]["content"][0]["text"])["status"], "recorded")
        self.assertEqual(self.reports()[0]["category"], "context")
        self.assertEqual(MAILBOX.messages(self.base, self.peer["session"]), [])

    def test_same_project_summary_includes_clones_sharing_any_normalized_remote(self):
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "remote.origin.url", "https://example.test/team/app.git"],
            check=True,
        )
        self.peer = MAILBOX.register(
            self.base, session=self.peer["session"], harness="codex", cwd=str(self.repository), pid=os.getpid()
        )
        self.submit()
        replica = self.directory / "replica"
        subprocess.run(["git", "init", "-q", str(replica)], check=True)
        subprocess.run(
            ["git", "-C", str(replica), "config", "remote.origin.url", "https://example.test/person/app.git"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(replica), "config", "remote.upstream.url", "git@example.test:team/app.git"], check=True
        )
        other = MAILBOX.register(
            self.base, session="replica-reporter", harness="claude", cwd=str(replica), pid=os.getpid()
        )
        FEEDBACK.submit(
            self.base,
            other,
            category="routing",
            intent="Coordinate a shared project",
            problem="Clone routing problem",
            needed="Clone routing support",
        )
        result = test_mailbox.mailbox_cli(
            self.base, "feedback-summary", "--format", "json", cwd=self.repository, check=True
        )
        self.assertEqual(json.loads(result.stdout)["reports"], 2)
        self.assertIn("Clone routing support", result.stdout)

    def test_failed_mcp_sends_record_only_codes_and_request_feedback_once(self):
        server = SERVER.Server(base=self.base, environ={})
        server.identify({"name": "codex-mcp-client"})
        request = test_mailbox.McpServerTests.call(
            "send", {"to": "private-unavailable-peer", "text": "confidential task body"}, session=self.peer["session"]
        )
        replies = []
        threads = [
            threading.Thread(
                target=lambda index=index: replies.append(server.handle({"jsonrpc": "2.0", "id": index, **request}))
            )
            for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(replies), 8)
        self.assertTrue(all(reply["result"]["isError"] for reply in replies))
        self.assertEqual(sum("what you needed" in reply["result"]["content"][0]["text"] for reply in replies), 1)
        report = self.reports()[0]
        self.assertEqual((report["kind"], report["code"], report["occurrences"]), ("diagnostic", "peer_unavailable", 8))
        self.assertNotIn("confidential task body", json.dumps(report))
        self.assertNotIn("private-unavailable-peer", json.dumps(report))

    def test_cli_failure_also_records_sanitized_diagnostics(self):
        response = test_mailbox.mailbox_cli(
            self.base,
            "send",
            "--session",
            self.peer["session"],
            "--harness",
            "codex",
            "--to",
            "missing-peer",
            "--text",
            "private body",
            cwd=self.repository,
        )
        self.assertEqual(response.returncode, 4)
        self.assertEqual(self.reports()[0]["code"], "peer_unavailable")
        self.assertNotIn("private body", json.dumps(self.reports()))

    def test_filtered_context_is_recorded_once_and_never_delivered_by_default(self):
        other_directory = self.directory / "other-project"
        subprocess.run(["git", "init", "-q", str(other_directory)], check=True)
        other = MAILBOX.register(
            self.base, session="other-session", harness="claude", cwd=str(other_directory), pid=os.getpid()
        )
        MAILBOX.send(
            self.base,
            sender=other,
            to=self.peer["session"],
            text="Unrelated confidential context",
            scope="cross-project",
        )
        for _ in range(5):
            self.assertEqual(MAILBOX.take(self.base, self.peer["session"], mark=False), [])
        self.assertEqual(len(self.reports()), 1)
        report = self.reports()[0]
        self.assertEqual((report["code"], report["occurrences"]), ("context_filtered", 1))
        self.assertNotIn("Unrelated confidential context", json.dumps(report))
        self.assertEqual(
            [m["text"] for m in MAILBOX.take(self.base, self.peer["session"], scope="cross-project")],
            ["Unrelated confidential context"],
        )

    def test_only_reply_timeouts_generate_diagnostics(self):
        plain = MAILBOX.wait(self.base, self.peer["session"], timeout=0.01)
        self.assertEqual(plain["status"], "timed_out")
        self.assertEqual(self.reports(), [])
        question = MAILBOX.wait(self.base, self.peer["session"], timeout=0.01, reply_to="abc-123456")
        self.assertEqual(question["status"], "timed_out")
        self.assertEqual(self.reports()[0]["code"], "reply_timeout")
        self.assertEqual(self.reports()[0]["measurements"], {"waitSeconds": 0.01})

    def test_reply_timeouts_request_agent_needs_once_without_waking_a_peer(self):
        arguments = (
            "wait",
            "--session",
            self.peer["session"],
            "--harness",
            "codex",
            "--timeout",
            "0.01",
            "--reply-to",
            "abc-123456",
        )
        responses = [test_mailbox.mailbox_cli(self.base, *arguments, cwd=self.repository) for _ in range(2)]
        self.assertEqual([response.returncode for response in responses], [12, 12])
        first, second = [json.loads(response.stdout) for response in responses]
        self.assertIn("what you needed", first["feedbackRequest"])
        self.assertNotIn("feedbackRequest", second)
        self.assertEqual((self.reports()[0]["code"], self.reports()[0]["occurrences"]), ("reply_timeout", 2))
        self.assertEqual(MAILBOX.messages(self.base, self.peer["session"]), [])

    def test_stop_extension_records_counts_without_copying_the_message(self):
        other = MAILBOX.register(
            self.base, session="local-sender", harness="claude", cwd=str(self.repository), pid=os.getpid()
        )
        MAILBOX.send(self.base, sender=other, to=self.peer["session"], text="Useful question")
        with mock.patch.object(MAILBOX, "harness_pid", return_value=os.getpid()):
            result = MAILBOX.hook(
                {"hook_event_name": "Stop", "session_id": self.peer["session"], "cwd": str(self.repository)},
                harness="codex",
                base=self.base,
            )
        self.assertEqual(result["decision"], "block")
        report = self.reports()[0]
        self.assertEqual(report["code"], "turn_extended")
        self.assertEqual(report["measurements"], {"messageCount": 1, "messageBytes": 15})
        self.assertNotIn("Useful question", json.dumps(report))

    def test_collector_failure_does_not_break_mailbox_delivery_or_errors(self):
        self.collector.write_text("not a directory")
        other = MAILBOX.register(
            self.base, session="local-sender", harness="claude", cwd=str(self.repository), pid=os.getpid()
        )
        MAILBOX.send(self.base, sender=other, to=self.peer["session"], text="Still delivered")
        self.assertEqual([m["text"] for m in MAILBOX.take(self.base, self.peer["session"])], ["Still delivered"])
        self.assertEqual(
            MAILBOX.wait(self.base, self.peer["session"], timeout=0.01, reply_to="abc-123456")["status"], "timed_out"
        )
        with self.assertRaises(OSError):
            self.submit()

    def test_concurrent_diagnostics_keep_all_occurrences_and_a_valid_summary(self):
        threads = [
            threading.Thread(
                target=FEEDBACK.observe,
                args=(self.base, self.peer),
                kwargs={"code": "reply_timeout", "category": "delivery", "operation": "wait"},
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.submit()
        (self.collector / "feedback-broken.json").write_text('{"schema":"agent-executor.mailbox-feedback.v1"}')
        (self.collector / "feedback-malformed-kind.json").write_text(
            '{"schema":"agent-executor.mailbox-feedback.v1","kind":["agent-report"]}'
        )
        summary = FEEDBACK.summary(self.base)
        self.assertEqual((summary["reports"], summary["diagnostics"], summary["agentReports"]), (2, 1, 1))
        self.assertEqual(summary["occurrences"], 9)
        self.assertEqual(summary["unreadableReports"], 2)
        self.assertEqual(summary["signals"], {"reply_timeout": 8, "agent_report": 1})
        self.assertEqual(summary["needs"][0]["needed"], "A bounded wait for the matching reply")

    def test_disabled_collection_is_explicit_and_does_not_affect_waits(self):
        config = self.directory / "config.json"
        config.write_text(json.dumps({"mailbox_feedback": {"enabled": False}}))
        with mock.patch.dict(os.environ, {"AGENT_EXECUTOR_CONFIG": str(config)}):
            os.environ.pop("AGENT_EXECUTOR_FEEDBACK_DIR")
            self.assertEqual(
                MAILBOX.wait(self.base, self.peer["session"], timeout=0.01, reply_to="abc-123456")["status"],
                "timed_out",
            )
            with self.assertRaisesRegex(FEEDBACK.FeedbackError, "disabled"):
                self.submit()
        self.assertEqual(self.reports(), [])
