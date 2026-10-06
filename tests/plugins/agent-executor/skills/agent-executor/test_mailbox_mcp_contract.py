"""Exercise both MCP revisions through the server's real newline-delimited stdio transport."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import test_mailbox

MAILBOX = test_mailbox.MAILBOX
SCRIPTS = test_mailbox.SCRIPTS


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "mailbox-v1"
        self.env = {
            **os.environ,
            "AGENT_EXECUTOR_HOME": self.temporary.name,
            "AGENT_EXECUTOR_FEEDBACK_DIR": str(Path(self.temporary.name) / "feedback"),
        }
        for key in ("CLAUDE_CODE_SESSION_ID", "AGENT_MAILBOX_LISTENER_SESSION", "AGENT_MAILBOX_SESSION"):
            self.env.pop(key, None)
        self.identifier = 0
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(test_mailbox.SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(self.close)
        self.sender = MAILBOX.register(
            self.base, session="fixture-sender", harness="claude", cwd=os.getcwd(), pid=os.getpid()
        )
        self.reader = MAILBOX.register(
            self.base, session="fixture-reader", harness="codex", cwd=os.getcwd(), pid=os.getpid()
        )

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()

    def rpc(self, method, params=None, *, client="codex-mcp-client", modern=True):
        self.identifier += 1
        params = dict(params or {})
        if modern:
            params["_meta"] = {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": client, "version": "1"},
                **params.get("_meta", {}),
            }
        request = {"jsonrpc": "2.0", "id": self.identifier, "method": method, "params": params}
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        # A watchdog owns only this test's process; malformed requests must not hang the suite.
        import threading

        watchdog = threading.Timer(10, self.process.kill)
        watchdog.start()
        try:
            line = self.process.stdout.readline()
            self.assertNotEqual(line, "", "server exited without replying")
            return json.loads(line)
        finally:
            watchdog.cancel()

    def tool(self, name, arguments=None, *, session="fixture-reader", client="codex-mcp-client"):
        return self.rpc(
            "tools/call", {"name": name, "arguments": {"session": session, **(arguments or {})}}, client=client
        )

    def send_fixture(self, text, reply_to=None):
        return MAILBOX.send(self.base, sender=self.sender, to=self.reader["name"], text=text, reply_to=reply_to)

    def test_modern_discovery_and_cacheable_catalog_without_a_handshake(self):
        discovered = self.rpc("server/discover")["result"]
        self.assertEqual(discovered["resultType"], "complete")
        self.assertEqual(discovered["supportedVersions"][0], "2026-07-28")
        self.assertEqual(discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "peer_mailbox_mcp")
        first = self.rpc("tools/list")["result"]
        second = self.rpc("tools/list", client="opencode")["result"]
        self.assertEqual(first, second)
        self.assertEqual((first["ttlMs"], first["cacheScope"]), (3600000, "public"))
        self.assertEqual(
            [t["name"] for t in first["tools"]], ["ack", "feedback", "focus", "inbox", "peers", "send", "wait"]
        )
        for tool in first["tools"]:
            self.assertEqual(
                set(tool["annotations"]), {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
            )
            self.assertIn("outputSchema", tool)

    def test_modern_metadata_errors_and_legacy_negotiation(self):
        unsupported = self.rpc("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}})
        self.assertEqual(unsupported["error"]["code"], -32022)
        self.assertEqual(unsupported["error"]["data"]["requested"], "2099-01-01")
        missing = self.rpc("tools/list", {"_meta": {"io.modelcontextprotocol/clientCapabilities": None}})
        self.assertEqual(missing["error"]["code"], -32602)
        missing_version = self.rpc(
            "tools/list", {"_meta": {"io.modelcontextprotocol/clientCapabilities": {}}}, modern=False
        )
        self.assertEqual(missing_version["error"]["code"], -32602)
        old = self.rpc(
            "initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "claude-code"}}, modern=False
        )
        self.assertEqual(old["result"]["protocolVersion"], "2025-11-25")
        newer = self.rpc(
            "initialize", {"protocolVersion": "2026-07-28", "clientInfo": {"name": "claude-code"}}, modern=False
        )
        self.assertEqual(newer["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(self.rpc("ping")["error"]["code"], -32601)
        self.assertEqual(self.rpc("ping", modern=False)["result"], {})

    def test_interleaved_modern_identity_does_not_inherit_another_client(self):
        for client, session, harness in [
            ("claude-code", "caller-a", "claude"),
            ("opencode", "caller-b", "opencode"),
            ("codex-mcp-client", "caller-c", "codex"),
        ]:
            answer = self.tool("peers", client=client, session=session)["result"]["structuredContent"]
            self.assertEqual(answer["peers"][0]["session"], session)
            self.assertEqual(answer["peers"][0]["harness"], harness)
        response = self.rpc("tools/call", {"name": "peers", "arguments": {}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("explicit session", response["result"]["content"][0]["text"])
        self.assertEqual(
            self.tool("focus", {"text": "verify isolation"}, session="caller-c")["result"]["structuredContent"],
            {"status": "set", "focus": "verify isolation"},
        )

    def test_schema_constraints_fail_before_side_effects(self):
        invalid = [
            ("wait", {"timeoutSeconds": 601}),
            ("wait", {"timeoutSeconds": 10**400}),
            ("peers", {"limit": 0}),
            ("peers", {"limit": True}),
            ("focus", {"text": "x" * 201}),
            ("focus", {"text": "\ud800"}),
            ("send", {"to": self.sender["name"], "text": "🐈" * 9000}),
            ("ack", {"ids": ["../escape"]}),
            ("feedback", {"category": "routing", "intent": "x" * 801, "problem": "failure", "needed": "fix"}),
            (
                "feedback",
                {
                    "category": "routing",
                    "intent": "find owner",
                    "problem": "failure",
                    "needed": "fix",
                    "messageIds": ["a-abcdef"] * 6,
                },
            ),
            ("inbox", {"markRead": True, "offset": 1}),
            ("wait", {"replyTo": "../escape"}),
        ]
        for tool, arguments in invalid:
            with self.subTest(tool=tool, arguments=list(arguments)):
                result = self.tool(tool, arguments, session="invalid-caller")["result"]
                self.assertTrue(result["isError"])
        self.assertFalse(MAILBOX.peer_path(self.base, "invalid-caller").exists())
        result = self.tool("send", {"to": self.sender["name"], "text": "valid", "wake": False})["result"]
        self.assertEqual(result["structuredContent"]["to"], self.sender["name"])
        self.assertEqual([m["text"] for m in MAILBOX.messages(self.base, self.sender["session"])], ["valid"])

    def test_inbox_pages_and_read_only_offsets_preserve_every_message(self):
        ids = [self.send_fixture(text)["id"] for text in ["alpha", "beta", "gamma"]]
        first = self.tool("inbox", {"markRead": False, "limit": 2})["result"]
        self.assertEqual([m["id"] for m in first["structuredContent"]["messages"]], ids[:2])
        self.assertEqual((first["structuredContent"]["has_more"], first["structuredContent"]["next_offset"]), (True, 2))
        last = self.tool("inbox", {"markRead": False, "limit": 2, "offset": 2, "response_format": "json"})["result"]
        self.assertEqual([m["text"] for m in last["structuredContent"]["messages"]], ["gamma"])
        self.assertEqual(json.loads(last["content"][0]["text"]), last["structuredContent"])
        self.assertIn("not from the user", last["structuredContent"]["notice"])
        drained = self.tool("inbox", {"limit": 2})["result"]["structuredContent"]
        self.assertEqual([m["id"] for m in drained["messages"]], ids[:2])
        self.assertEqual(drained["next_offset"], 0)
        self.assertEqual([m["id"] for m in self.tool("inbox")["result"]["structuredContent"]["messages"]], ids[2:])
        self.assertEqual(self.tool("inbox")["result"]["structuredContent"]["status"], "empty")

    def test_message_bodies_are_not_duplicated_and_byte_budget_never_truncates(self):
        bodies = ["first-" + ("a" * 8000), "second-" + ("b" * 8000), "third"]
        ids = [self.send_fixture(body)["id"] for body in bodies]
        result = self.tool("inbox")["result"]
        self.assertEqual([m["id"] for m in result["structuredContent"]["messages"]], ids[:1])
        self.assertEqual(json.dumps(result).count(bodies[0]), 1)
        self.assertEqual([m["text"] for m in MAILBOX.messages(self.base, self.reader["session"])], bodies[1:])
        rest = self.tool("wait", {"timeoutSeconds": 0.1, "limit": 1})["result"]
        self.assertEqual([m["id"] for m in rest["structuredContent"]["messages"]], ids[1:2])
        self.assertTrue(rest["structuredContent"]["has_more"])
        self.tool("inbox")
        big = "x" * 32768
        self.send_fixture(big)
        result = self.tool("inbox")["result"]
        self.assertIn(big, result["content"][0]["text"])
        self.assertEqual(json.dumps(result).count(big), 1)

    def test_read_only_peer_pagination_covers_all_sessions(self):
        page = self.tool("peers", {"limit": 1})["result"]["structuredContent"]
        self.assertEqual(
            (page["peers"][0]["session"], page["total_count"], page["has_more"], page["next_offset"]),
            ("fixture-reader", 2, True, 1),
        )
        next_page = self.tool("peers", {"limit": 1, "offset": 1})["result"]["structuredContent"]
        self.assertEqual([p["session"] for p in next_page["peers"]], ["fixture-sender"])
        self.assertFalse(next_page["has_more"])

    def test_storage_errors_do_not_expose_paths_or_secrets(self):
        server = test_mailbox.RUNNER.bundled_module("mailbox_mcp").Server(base=self.base, environ={})
        server.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}})
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "peers", "arguments": {}, "_meta": {"sessionId": "error-caller"}},
        }
        with mock.patch.object(server, "execute", side_effect=OSError("secret /private/user/path")):
            response = server.handle(request)
        text = response["result"]["content"][0]["text"]
        self.assertIn("check mailbox/collector permissions", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("/private", text)
        feedback_error = test_mailbox.RUNNER.bundled_module("mailbox_mcp").mailbox.feedback_module().FeedbackError
        with mock.patch.object(
            server, "execute", side_effect=feedback_error("mailbox feedback collection is disabled")
        ):
            disabled = server.handle(request)
        self.assertIn("mailbox feedback collection is disabled", disabled["result"]["content"][0]["text"])
        self.assertEqual(self.tool("focus", {"text": "keep serving"})["result"]["structuredContent"]["status"], "set")

    def test_frozen_evaluation_answers_are_retrievable_without_consuming_mail(self):
        self.close()
        evaluation_server = Path(__file__).with_name("mailbox_evaluation_server.py")
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(evaluation_server)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        peers = []
        messages = []
        for name, target, key, extra in (
            ("peers", peers, "peers", {}),
            ("inbox", messages, "messages", {"markRead": False}),
        ):
            offset = 0
            while True:
                response = self.tool(
                    name, {"limit": 2, "offset": offset, "response_format": "json", **extra}, session="investigator"
                )["result"]["structuredContent"]
                target.extend(response[key])
                if not response["has_more"]:
                    break
                offset = response["next_offset"]
        by_id = {message["id"]: message for message in messages}
        accepted = sorted((m for m in messages if "verification accepted" in m["text"]), key=lambda m: m["sentAt"])
        blocked = next(m for m in messages if "verification rejected" in m["text"])
        latest = accepted[-1]
        first = accepted[0]
        latest_parent = by_id[latest["replyTo"]]
        first_parent = by_id[first["replyTo"]]
        first_root = by_id[first_parent["replyTo"]]
        blocked_parent = by_id[blocked["replyTo"]]

        def minutes(earlier, later):
            return str(
                int(
                    (
                        dt.datetime.fromisoformat(later["sentAt"]) - dt.datetime.fromisoformat(earlier["sentAt"])
                    ).total_seconds()
                    / 60
                )
            )

        answers = [
            re.search(r"accepted (crate-\d+)", latest["text"])[1],
            next(p["harness"] for p in peers if p["name"] == latest["from"]),
            re.search(r"digest ([a-f0-9]+)", first["text"])[1],
            re.search(r"stamp (review-\d+)", blocked_parent["text"])[1],
            minutes(first_root, first),
            minutes(latest_parent, latest),
            next(
                p["session"]
                for p in peers
                if p["name"] == blocked_parent["from"] and p["focus"] == "storage maintainer"
            ),
            str(len({re.search(r"accepted (crate-\d+)", m["text"])[1] for m in accepted})),
            re.search(r"([a-f0-9]+) was the rejected digest", first_parent["text"])[1],
            re.search(r"stamp (review-\d+)", latest["text"])[1],
        ]
        evaluation = ET.parse(Path(__file__).parent / "evaluations/evaluation.xml")
        self.assertEqual(answers, [pair.findtext("answer") for pair in evaluation.findall("qa_pair")])
        reread = self.tool(
            "inbox", {"markRead": False, "limit": 50, "response_format": "json"}, session="investigator"
        )["result"]["structuredContent"]
        self.assertEqual([m["id"] for m in reread["messages"]], [m["id"] for m in messages])

    def test_pending_wait_capacity_and_cancellation_keep_other_tools_available(self):
        metadata = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "codex-mcp-client", "version": "1"},
        }
        for identifier in range(100, 108):
            self.process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "method": "tools/call",
                        "params": {
                            "name": "wait",
                            "arguments": {"session": "fixture-reader", "timeoutSeconds": 30},
                            "_meta": metadata,
                        },
                    }
                )
                + "\n"
            )
        self.process.stdin.flush()
        refused = self.tool("wait", {"timeoutSeconds": 30})["result"]
        self.assertEqual(refused["resultType"], "complete")
        self.assertTrue(refused["isError"])
        self.assertIn("cancel a pending wait", refused["content"][0]["text"])
        self.process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {"name": "wait", "arguments": {}, "_meta": ["malformed"]},
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        malformed = json.loads(self.process.stdout.readline())
        self.assertTrue(malformed["result"]["isError"])
        for identifier in range(100, 108):
            self.process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": identifier}})
                + "\n"
            )
        self.process.stdin.flush()
        focused = self.tool("focus", {"text": "waits cancelled"})["result"]
        self.assertEqual(focused["structuredContent"], {"status": "set", "focus": "waits cancelled"})

    def test_modern_calls_without_identity_or_metadata_never_fall_back_silently(self):
        before = self.rpc("tools/call", {"name": "peers", "arguments": {}}, modern=False)
        self.assertEqual(before["error"]["code"], -32602)
        self.assertIn("metadata", before["error"]["message"])
        after = self.tool("peers")["result"]["structuredContent"]
        self.assertEqual(after["peers"][0]["session"], "fixture-reader")
