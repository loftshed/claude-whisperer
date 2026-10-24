"""Exercise both MCP revisions through the server's real newline-delimited stdio transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import mailbox_evaluation_server
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

    def test_channels_join_through_focus_and_post_through_send(self):
        joined = self.tool("focus", {"text": "Graph build", "channels": ["graph"]})["result"]
        self.assertEqual(joined["structuredContent"]["channels"], ["graph"])
        MAILBOX.register(self.base, session="fixture-other", harness="codex", cwd=os.getcwd(), pid=os.getpid())
        posted = self.tool("send", {"channel": "graph", "text": "edges ready"}, session="fixture-sender")["result"]
        self.assertFalse(posted.get("isError"), posted)
        self.assertEqual(
            (posted["structuredContent"]["channel"], posted["structuredContent"]["delivered"]), ("graph", 1)
        )
        self.assertEqual([m["text"] for m in MAILBOX.incoming(self.base, "fixture-reader")], ["edges ready"])
        self.assertEqual(MAILBOX.incoming(self.base, "fixture-other"), [])
        (row,) = self.tool("inbox")["result"]["structuredContent"]["messages"]
        self.assertEqual((row["channel"], row["postId"]), ("graph", posted["structuredContent"]["postId"]))
        both = self.tool("send", {"channel": "graph", "to": "fixture-reader", "text": "x"}, session="fixture-sender")
        self.assertTrue(both["result"]["isError"])
        rows = self.tool("peers")["result"]["structuredContent"]["peers"]
        self.assertEqual(next(p for p in rows if p["session"] == "fixture-reader")["channels"], "graph")
        left = self.tool("focus", {"text": "Graph build", "leave": ["graph"]})["result"]["structuredContent"]
        self.assertNotIn("channels", left)

    def test_registration_focus_and_peer_display_names_preserve_old_clients(self):
        args = {
            "cwd": os.getcwd(),
            "harness": "codex",
            "focus": "Designer · translation parity",
            "model": "gpt-6-sol",
            "harnessLabel": "Codex CLI",
        }
        registered = self.tool("register", args)["result"]
        self.assertEqual(registered["structuredContent"], {"session": "fixture-reader"})
        rows = self.tool("peers")["result"]
        reader = next(p for p in rows["structuredContent"]["peers"] if p["session"] == "fixture-reader")
        self.assertEqual(reader["displayName"], args["focus"])
        self.assertEqual(reader["model"], "gpt-6-sol")
        self.assertEqual(
            reader["displayLabel"], f"{reader['name']} · Designer · translation parity · gpt-6-sol · Codex CLI"
        )
        self.assertEqual(reader["harnessLabel"], "Codex CLI")
        self.assertEqual(reader["modelSource"], "declared")
        self.assertIn("gpt-6-sol · Codex CLI", rows["content"][0]["text"])
        # The model joins the name after the person; the rest of the name the session started with stays.
        person = self.reader["name"].split("-")[0]
        self.assertEqual(reader["name"], self.reader["name"].replace(f"{person}-", f"{person}-gpt-6-sol-", 1))
        self.assertEqual(reader["checkout"], str(SCRIPTS.parents[4].resolve()))
        # The repository is named after its shared Git directory, so a linked worktree keeps the main name.
        common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=SCRIPTS,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(reader["repositoryName"], Path(common_dir).parent.name)
        self.assertTrue(rows["content"][0]["text"].startswith(f"{reader['name']} · {args['focus']}"))
        self.tool("register", {"cwd": os.getcwd(), "harness": "codex"})
        self.assertEqual(MAILBOX.load(MAILBOX.peer_path(self.base, "fixture-reader"))["focus"], args["focus"])
        self.tool("focus", {"text": "Mock server · rebase fixes", "model": "gpt-6-astra", "harnessLabel": "Codex"})
        rows = self.tool("peers")["result"]["structuredContent"]["peers"]
        self.assertEqual(next(p for p in rows if p["session"] == "fixture-reader")["model"], "gpt-6-astra")
        self.assertEqual(
            next(p for p in rows if p["session"] == "fixture-reader")["displayName"], "Mock server · rebase fixes"
        )
        self.assertEqual(next(p for p in rows if p["session"] == "fixture-sender")["displayName"], self.sender["name"])
        self.assertTrue(self.tool("register", {**args, "focus": "   "})["result"]["isError"])
        self.assertTrue(self.tool("focus", {"text": "Review", "harnessLabel": " "})["result"]["isError"])
        self.assertEqual(next(p for p in rows if p["session"] == "fixture-reader")["harness"], "codex")
        self.assertEqual(
            next(p for p in rows if p["session"] == "fixture-reader")["displayLabel"],
            f"{reader['name'].replace('gpt-6-sol', 'gpt-6-astra')} · Mock server · rebase fixes · gpt-6-astra · Codex",
        )
        same_codex = MAILBOX.register(
            self.base, session="fixture-codex-peer", harness="codex", cwd=os.getcwd(), pid=os.getpid()
        )
        rows = self.tool("peers")["result"]["structuredContent"]["peers"]
        peer_row = next(p for p in rows if p["session"] == same_codex["session"])
        self.assertEqual(peer_row["nativeAddress"], "fixture-codex-peer")
        self.assertIn("codex_tui.send_message_to_thread", peer_row["nativeTool"])
        cross_row = next(p for p in rows if p["session"] == "fixture-sender")
        self.assertEqual((cross_row["nativeAddress"], cross_row["nativeTool"]), ("", ""))

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
            [t["name"] for t in first["tools"]],
            ["ack", "feedback", "focus", "inbox", "peers", "register", "send", "wait"],
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
        aliased = self.tool("send", {"to": self.sender["name"], "message": "via alias", "wake": False})["result"]
        self.assertFalse(aliased.get("isError", False))
        waited = self.tool(
            "wait",
            {"timeout": 0.05, "reply_to": aliased["structuredContent"]["id"], "mark_read": False},
        )["result"]
        self.assertEqual(waited["structuredContent"]["status"], "timed_out")

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
        answers = mailbox_evaluation_server.evaluation_answers(peers, messages)
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
