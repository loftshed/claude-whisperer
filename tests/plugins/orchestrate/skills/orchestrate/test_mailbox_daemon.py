"""Public HTTP/lifecycle behavior of the single local daemon, using disposable stores."""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import mailbox_evaluation_server
import test_mailbox

SCRIPTS = test_mailbox.SCRIPTS
sys.path.insert(0, str(SCRIPTS))
import mailbox_daemon as daemon  # noqa: E402 -- deployed entry point, tested through real HTTP
import peer_mailbox as mailbox  # noqa: E402 -- fixture setup and durable observation


class TestMailboxDaemon(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mailbox-daemon-contract-")
        self.base = Path(self.temp.name) / "cache" / "mailbox-v1"
        self.project = Path(self.temp.name) / "project"
        self.other = Path(self.temp.name) / "other"
        for path in (self.project, self.other):
            subprocess.run(["git", "init", "-q", str(path)], check=True)
        self.state = None
        self.children = []
        self.counter = 0

    def tearDown(self):
        with contextlib.suppress(OSError, mailbox.MailboxError):
            daemon.stop_service(self.base)
        for child in self.children:
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=5)
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        self.temp.cleanup()

    def start(self):
        self.state = daemon.ensure_running(self.base)
        return self.state

    def peer(self, session, harness="codex", cwd=None, **kwargs):
        return mailbox.register(
            self.base, session=session, harness=harness, cwd=str(cwd or self.project), pid=os.getpid(), **kwargs
        )

    def rpc(
        self,
        method,
        params=None,
        *,
        client="contract-client",
        modern=True,
        identifier=None,
        headers=None,
        url=None,
        token=None,
    ):
        self.counter += 1
        params = dict(params or {})
        if modern:
            params["_meta"] = {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": client, "version": "1"},
                **params.get("_meta", {}),
            }
        request = {
            "jsonrpc": "2.0",
            "id": self.counter if identifier is None else identifier,
            "method": method,
            "params": params,
        }
        outgoing = {
            "Authorization": "Bearer " + (daemon.read_token(self.base) if token is None else token),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if modern:
            outgoing.update({"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method})
            if method == "tools/call":
                outgoing["Mcp-Name"] = params["name"]
        for key, value in (headers or {}).items():
            if value is None:
                outgoing.pop(key, None)
            else:
                outgoing[key] = value
        parsed = urllib.parse.urlparse(url or self.state["url"])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=4)
        try:
            connection.request("POST", "/mcp", json.dumps(request), outgoing)
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else None, dict(response.headers)
        finally:
            connection.close()

    def call(self, name, arguments=None, *, session="sender", **kwargs):
        args = dict(arguments or {})
        if session is not None:
            args["session"] = session
        return self.rpc("tools/call", {"name": name, "arguments": args}, **kwargs)

    def body(self, response):
        status, value, _ = response
        self.assertEqual(status, 200, value)
        self.assertNotIn("error", value)
        self.assertFalse(value["result"].get("isError", False), value)
        return value["result"]["structuredContent"]

    def send(self, text="hello", key="delivery", *, identifier=None, session="sender", **kwargs):
        return self.call(
            "send",
            {"to": "receiver", "text": text, "wake": False, "idempotencyKey": key, **kwargs},
            session=session,
            identifier=identifier,
        )

    def fault_service(self, crash=False, delay=0):
        script = """
import os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import mailbox_daemon as d, peer_mailbox as m, mailbox_wake as wake
base=Path(sys.argv[2]); counter=Path(sys.argv[3]); crash=sys.argv[4]=='1'; delay=float(sys.argv[5])
def activate(*args,**kwargs):
 n=int(counter.read_text()) if counter.exists() else 0
 counter.write_text(str(n+1))
 if delay:time.sleep(delay)
 return {'status':'scheduled','reason':'synthetic activation'}
wake.wake=activate
original=m.write
def write(path,value):
 if crash and '/journal/' in str(path) and value.get('response'):
  os.kill(os.getpid(),9)
 return original(path,value)
m.write=write
d.serve(base)
"""
        counter = Path(self.temp.name) / "wake-count"
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                str(SCRIPTS),
                str(self.base),
                str(counter),
                "1" if crash else "0",
                str(delay),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        until = time.monotonic() + 15
        while time.monotonic() < until:
            state = daemon.owned_state(self.base)
            if state and state["pid"] == child.pid:
                self.state = state
                return counter, child
            if child.poll() is not None:
                self.fail("fixture daemon exited before readiness")
            time.sleep(0.05)
        self.fail("fixture daemon did not become ready")

    def test_concurrent_startup_and_runtime_health(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            states = list(pool.map(lambda _: daemon.ensure_running(self.base), range(10)))
        self.state = states[0]
        self.assertEqual(len({s["pid"] for s in states}), 1)
        self.assertEqual(len({s["url"] for s in states}), 1)
        self.assertEqual(
            daemon.check_daemon(self.state["url"], daemon.read_token(self.base))["instance"], self.state["instance"]
        )
        self.assertEqual(self.state["protocolVersions"][0], "2026-07-28")
        self.assertNotIn("token", self.state)

    def test_plugin_removal_does_not_break_lazy_wake_import(self):
        source = Path(self.temp.name) / "plugin-copy"
        shutil.copytree(SCRIPTS.parent, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        controller = source / "scripts/mailbox_daemon.py"
        launched = subprocess.run(
            [sys.executable, "-B", str(controller), "start", "--root", str(self.base), "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.state = json.loads(launched.stdout)
        shutil.rmtree(source)
        self.peer("sender")
        self.peer("receiver", "unknown")
        result = self.body(self.send(wake=True))
        self.assertEqual(result["wake"]["status"], "unsupported")
        inbox = self.body(self.call("inbox", {"response_format": "json"}, session="receiver"))
        self.assertEqual(inbox["messages"][0]["text"], "hello")

    def test_startup_budget_includes_lock_contention(self):
        self.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with daemon.locked_file(daemon.daemon_base(self.base) / "startup.lock"):
                future = pool.submit(daemon.ensure_running, self.base, startup_timeout=0.15)
                with self.assertRaisesRegex(mailbox.MailboxError, "startup busy"):
                    future.result(timeout=1)

    def test_malformed_journals_do_not_block_service_startup(self):
        directory = self.base / "journal/corrupt-fixture"
        directory.mkdir(parents=True)
        broken = directory / "broken.json"
        mailbox.write(broken, {"schema": "agent-executor.send-operation.expired.v1"})
        before = broken.read_bytes()
        sender = self.peer("sender")
        self.peer("receiver", "claude")
        mailbox.send(self.base, sender=sender, to="receiver", text="preserve", wake=False, idempotency_key="corrupt")
        operation = next((self.base / "journal" / mailbox.session_key("sender")).glob("*.json"))
        entry = mailbox.load(operation)
        entry["payload"] = None
        entry["message"]["sentAt"] = mailbox.stamp(mailbox.now() - dt.timedelta(days=8))
        mailbox.write(operation, entry)
        original_operation = operation.read_bytes()
        self.start()
        self.assertEqual(broken.read_bytes(), before)
        self.assertEqual(operation.read_bytes(), original_operation)
        self.assertEqual(
            self.body(self.call("register", {"cwd": str(self.project), "harness": "unknown"}, session=None)).keys(),
            {"session"},
        )

    def test_graceful_and_abrupt_restart_keep_endpoint_credentials_and_unread_mail(self):
        self.start()
        original = self.state
        token = daemon.read_token(self.base)
        self.peer("sender")
        self.peer("receiver", "claude")
        sent = self.body(self.send())
        self.assertTrue(daemon.stop_service(self.base))
        self.start()
        self.assertEqual((self.state["url"], daemon.read_token(self.base)), (original["url"], token))
        self.assertNotEqual(self.state["instance"], original["instance"])
        before = self.state
        os.kill(before["pid"], signal.SIGKILL)
        until = time.monotonic() + 3
        while daemon.check_daemon(before["url"], token) and time.monotonic() < until:
            time.sleep(0.05)
        self.start()
        self.assertEqual((self.state["url"], daemon.read_token(self.base)), (original["url"], token))
        incoming = self.body(self.call("inbox", session="receiver"))
        self.assertEqual([m["id"] for m in incoming["messages"]], [sent["id"]])
        self.assertEqual(self.body(self.call("inbox", session="receiver")), incoming)
        self.assertEqual(
            self.body(self.call("ack", {"ids": [sent["id"]]}, session="receiver")), {"acknowledged": [sent["id"]]}
        )
        self.assertEqual(self.body(self.call("inbox", session="receiver"))["status"], "empty")

    def test_register_bootstraps_and_never_uses_daemon_context(self):
        self.start()
        registered = self.body(
            self.call("register", {"cwd": str(self.other), "harness": "gemini", "pid": os.getpid()}, session=None)
        )
        session = registered["session"]
        peer = mailbox.load(mailbox.peer_path(self.base, session))
        self.assertEqual(
            (peer["cwd"], peer["harness"], peer["pid"]), (str(self.other.resolve()), "gemini", os.getpid())
        )
        self.assertEqual(
            self.body(self.call("focus", {"text": "caller context"}, session=session)),
            {"status": "set", "focus": "caller context"},
        )
        unknown = self.call("focus", {"text": "invalid"}, session="unknown-caller")[1]
        self.assertTrue(unknown["result"]["isError"])
        self.assertFalse(mailbox.peer_path(self.base, "unknown-caller").exists())
        invalid = self.call("register", {"cwd": "relative", "harness": "gemini"}, session=None)[1]
        self.assertTrue(invalid["result"]["isError"])

    def test_hook_identity_and_expired_callers_are_protected(self):
        self.start()
        self.peer("native", cwd_source="hook")
        bad = self.call("register", {"cwd": str(self.other), "harness": "claude"}, session="native")[1]
        self.assertTrue(bad["result"]["isError"])
        peer = mailbox.load(mailbox.peer_path(self.base, "native"))
        self.assertEqual(peer["cwd"], str(self.project))
        mismatch = self.call("focus", {"text": "invalid"}, session="native", client="claude-code")[1]
        self.assertTrue(mismatch["result"]["isError"])
        mailbox.annotate(self.base, "native", pid=2147483647)
        stale = self.call("focus", {"text": "invalid"}, session="native")[1]
        self.assertTrue(stale["result"]["isError"])
        self.assertEqual(mailbox.load(mailbox.peer_path(self.base, "native"))["pid"], 2147483647)

    def test_native_first_and_project_filters_hold_for_reads_and_ack(self):
        self.start()
        self.peer("sender")
        self.peer("receiver", "claude", self.other)
        self.assertTrue(self.send()[1]["result"]["isError"])
        sent = self.body(self.send(scope="cross-project"))
        self.assertEqual(self.body(self.call("inbox", session="receiver"))["status"], "empty")
        seen = self.body(self.call("inbox", {"scope": "cross-project"}, session="receiver"))
        self.assertEqual([m["id"] for m in seen["messages"]], [sent["id"]])
        self.assertTrue(self.call("ack", {"ids": [sent["id"]]}, session="receiver")[1]["result"]["isError"])
        self.assertEqual(
            self.body(self.call("ack", {"ids": [sent["id"]], "scope": "cross-project"}, session="receiver"))[
                "acknowledged"
            ],
            [sent["id"]],
        )
        self.peer("claude-sender", "claude", self.other)
        native = self.call(
            "send",
            {
                "to": "receiver",
                "text": "native",
                "wake": False,
                "idempotencyKey": "native-refused",
                "scope": "cross-project",
            },
            session="claude-sender",
        )
        self.assertTrue(native[1]["result"]["isError"])

    def test_same_key_is_concurrent_safe_and_semantic_mismatches_fail(self):
        self.start()
        self.peer("sender")
        self.peer("receiver", "claude")
        self.peer("other-receiver", "claude")

        def attempt(i):
            return self.body(self.send(identifier=100 + i))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            values = list(pool.map(attempt, range(10)))
        self.assertEqual(len({v["id"] for v in values}), 1)
        self.assertEqual(len(mailbox.messages(self.base, "receiver", unread_only=False)), 1)
        self.assertTrue(self.send(text="changed")[1]["result"]["isError"])
        self.assertTrue(
            self.call("send", {"to": "other-receiver", "text": "hello", "wake": False, "idempotencyKey": "delivery"})[
                1
            ]["result"]["isError"]
        )
        self.assertEqual(len(mailbox.messages(self.base, "other-receiver")), 0)
        self.assertEqual(self.body(self.send(identifier=999))["id"], values[0]["id"])

    def test_crash_after_activation_before_response_recovers_without_duplicate_wake(self):
        counter, child = self.fault_service(crash=True)
        self.peer("sender")
        self.peer("receiver", "claude")
        with self.assertRaises((OSError, http.client.HTTPException)):
            self.call(
                "send", {"to": "receiver", "text": "crash challenge", "wake": True, "idempotencyKey": "crash-key"}
            )
        child.wait(timeout=5)
        persisted = mailbox.messages(self.base, "receiver")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(counter.read_text(), "1")
        original_url = self.state["url"]
        original_token = daemon.read_token(self.base)
        self.fault_service(crash=False)
        self.assertEqual((self.state["url"], daemon.read_token(self.base)), (original_url, original_token))
        replay = self.body(
            self.call(
                "send",
                {"to": "receiver", "text": "crash challenge", "wake": True, "idempotencyKey": "crash-key"},
                identifier=77,
            )
        )
        self.assertEqual(replay["id"], persisted[0]["id"])
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(len(mailbox.messages(self.base, "receiver", unread_only=False)), 1)

    def test_ten_callers_and_duplicate_rpc_ids_remain_isolated(self):
        self.start()
        for i in range(10):
            self.peer(f"caller-{i}")

        def wait(i):
            return self.body(self.call("wait", {"timeoutSeconds": 0.2}, session=f"caller-{i}", identifier=1))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            result = list(pool.map(wait, range(10)))
        self.assertEqual([r["status"] for r in result], ["timed_out"] * 10)
        for i in range(10):
            result = self.body(self.call("focus", {"text": f"focus-{i}"}, session=f"caller-{i}", identifier=1))
            self.assertEqual(result["focus"], f"focus-{i}")

    def test_disconnect_cancels_wait_without_consuming_mail(self):
        self.start()
        self.peer("sender")
        self.peer("receiver", "claude")
        parsed = urllib.parse.urlparse(self.state["url"])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "wait",
                "arguments": {"session": "receiver", "timeoutSeconds": 30},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        connection.request(
            "POST",
            "/mcp",
            json.dumps(body),
            {
                "Authorization": "Bearer " + daemon.read_token(self.base),
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "wait",
            },
        )
        until = time.monotonic() + 2
        while time.monotonic() < until:
            if mailbox.load(mailbox.peer_path(self.base, "receiver"))["state"] == "waiting":
                break
            time.sleep(0.02)
        self.assertEqual(mailbox.load(mailbox.peer_path(self.base, "receiver"))["state"], "waiting")
        connection.close()
        until = time.monotonic() + 2
        while time.monotonic() < until and mailbox.load(mailbox.peer_path(self.base, "receiver"))["state"] == "waiting":
            time.sleep(0.02)
        sent = self.body(self.send())
        self.assertEqual(self.body(self.call("inbox", session="receiver"))["messages"][0]["id"], sent["id"])
        self.assertEqual(mailbox.messages(self.base, "receiver")[0]["text"], "hello")

    def test_legacy_contexts_are_isolated_and_modern_discovery_needs_no_handshake(self):
        self.start()
        self.peer("codex-session")
        self.peer("claude-session", "claude")
        status, a, h1 = self.rpc(
            "initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "codex-mcp-client"}}, modern=False
        )
        status, b, h2 = self.rpc(
            "initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "claude-code"}}, modern=False
        )
        s1 = h1["Mcp-Session-Id"]
        s2 = h2["Mcp-Session-Id"]
        self.assertNotEqual(s1, s2)
        self.assertEqual(a["result"]["protocolVersion"], "2025-11-25")

        def focus(session, transport):
            return self.call(
                "focus", {"text": "legacy"}, session=session, modern=False, headers={"Mcp-Session-Id": transport}
            )

        self.assertEqual(self.body(focus("codex-session", s1))["focus"], "legacy")
        self.assertEqual(self.body(focus("claude-session", s2))["focus"], "legacy")
        self.assertTrue(focus("claude-session", s1)[1]["result"]["isError"])
        status, discovery, _ = self.rpc("server/discover")
        self.assertEqual(status, 200)
        self.assertEqual(discovery["result"]["supportedVersions"][0], "2026-07-28")
        self.assertEqual(self.rpc("tools/list", modern=False)[0], 404)

    def test_transport_headers_auth_and_origin_errors_are_actionable(self):
        self.start()
        self.assertEqual(self.rpc("tools/list", token="wrong")[0], 401)
        self.assertEqual(self.rpc("tools/list", headers={"Origin": "http://localhost.evil.invalid"})[0], 403)
        for headers in ({"Mcp-Method": None}, {"Mcp-Method": "tools/call"}, {"MCP-Protocol-Version": "2025-11-25"}):
            status, value, _ = self.rpc("tools/list", headers=headers)
            self.assertEqual((status, value["error"]["code"]), (400, -32020))
        status, value, _ = self.call("focus", {"text": "x"}, headers={"Mcp-Name": "send"})
        self.assertEqual((status, value["error"]["code"]), (400, -32020))
        status, value, _ = self.rpc(
            "tools/list",
            headers={"MCP-Protocol-Version": "2099-01-01"},
            params={"_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}},
        )
        self.assertEqual((status, value["error"]["code"]), (400, -32022))

    def test_http_reads_cannot_consume_and_discovery_describes_key_requirement(self):
        self.start()
        self.peer("sender")
        self.peer("receiver", "claude")
        result = self.rpc("tools/list")[1]["result"]
        send = next(t for t in result["tools"] if t["name"] == "send")
        self.assertIn("idempotencyKey", send["inputSchema"]["required"])
        self.body(self.send())
        bad = self.call("inbox", {"markRead": True}, session="receiver")[1]
        self.assertTrue(bad["result"]["isError"])
        self.assertEqual(len(mailbox.messages(self.base, "receiver")), 1)
        self.assertEqual((result["ttlMs"], result["cacheScope"]), (3600000, "public"))

    def test_state_credentials_and_health_do_not_trust_stale_files(self):
        self.start()
        actual = self.state.copy()
        self.assertEqual((daemon.daemon_base(self.base) / "token.txt").stat().st_mode & 0o777, 0o600)
        bogus = {**actual, "pid": os.getpid(), "instance": "bogus"}
        mailbox.write(daemon.daemon_base(self.base) / "state.json", bogus)
        health = daemon.check_daemon(actual["url"], daemon.read_token(self.base))
        self.assertEqual((health["pid"], health["instance"]), (actual["pid"], actual["instance"]))
        self.assertFalse(daemon.stop_service(self.base))
        mailbox.write(daemon.daemon_base(self.base) / "state.json", actual)
        self.assertEqual(daemon.ensure_running(self.base)["pid"], actual["pid"])
        for port in (3000, 3001, 3002, 4001, 4002, 8080, 9222, -1, 65536):
            with self.assertRaises(mailbox.MailboxError):
                daemon.ensure_running(self.base, port=port)
        self.assertIsNone(daemon.check_daemon("http://example.invalid/mcp", daemon.read_token(self.base)))

    def test_persisted_busy_endpoint_is_not_silently_replaced(self):
        self.start()
        old = self.state
        daemon.stop_service(self.base)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", urllib.parse.urlparse(old["url"]).port))
            occupied.listen()
            with self.assertRaises(mailbox.MailboxError):
                daemon.ensure_running(self.base)
            self.assertEqual(daemon.load_state(self.base)["url"], old["url"])
        self.start()
        self.assertEqual(self.state["url"], old["url"])

    def test_frozen_ten_question_evaluation_through_http_stays_unread(self):
        self.start()
        fixture = json.loads((Path(__file__).parent / "evaluations/snapshot.json").read_text())
        senders = {}
        for peer in fixture["peers"]:
            record = self.peer(peer["session"], peer["harness"])
            record = mailbox.annotate(self.base, peer["session"], name=peer["name"], focus=peer["focus"])
            senders[peer["session"]] = record
        recipient = self.peer("investigator")
        for item in fixture["messages"]:
            mailbox.write(
                mailbox.inbox_dir(self.base, "investigator") / (item["id"] + ".json"),
                {
                    "schema": mailbox.MAIL_SCHEMA,
                    "id": item["id"],
                    "from": mailbox.sender_address(senders[item["sender"]]),
                    "to": {"session": "investigator", "name": recipient["name"]},
                    "text": item["text"],
                    "sentAt": item["sentAt"],
                    "replyTo": item["replyTo"],
                    "readAt": None,
                    "scope": "project",
                },
            )
        peers = []
        messages = []
        for name, target, key in (("peers", peers, "peers"), ("inbox", messages, "messages")):
            offset = 0
            while True:
                value = self.body(
                    self.call(name, {"limit": 2, "offset": offset, "response_format": "json"}, session="investigator")
                )
                target.extend(value[key])
                if not value["has_more"]:
                    break
                offset = value["next_offset"]
        answers = mailbox_evaluation_server.evaluation_answers(peers, messages)
        pairs = ET.parse(Path(__file__).parent / "evaluations/evaluation.xml").findall("qa_pair")
        self.assertEqual(answers, [pair.findtext("answer") for pair in pairs])
        self.assertEqual(len(answers), 10)
        again = self.body(self.call("inbox", {"limit": 50, "response_format": "json"}, session="investigator"))
        self.assertEqual([m["id"] for m in again["messages"]], [m["id"] for m in messages])

    def test_committed_retry_survives_recipient_departure(self):
        self.start()
        self.peer("sender")
        self.peer("receiver", "claude")
        first = self.body(self.send(key="retained-delivery"))
        mailbox.unregister(self.base, "receiver")
        retry = self.body(self.send(key="retained-delivery", identifier=999))
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(len(mailbox.messages(self.base, "receiver", unread_only=False)), 1)
        mismatch = self.call(
            "send", {"to": "other-recipient", "text": "hello", "wake": False, "idempotencyKey": "retained-delivery"}
        )[1]
        self.assertTrue(mismatch["result"]["isError"])

    def test_graceful_shutdown_drains_committed_send_response(self):
        counter, child = self.fault_service(delay=1)
        self.peer("sender")
        self.peer("receiver", "claude")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self.call, "send", {"to": "receiver", "text": "drain", "wake": True, "idempotencyKey": "drain-key"}
            )
            deadline = time.monotonic() + 3
            while not counter.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(counter.read_text(), "1")
            self.assertTrue(daemon.stop_service(self.base))
            value = self.body(future.result(timeout=3))
        self.assertEqual(value["wake"]["status"], "scheduled")
        self.assertEqual(len(mailbox.messages(self.base, "receiver")), 1)
        child.wait(timeout=5)

    def test_journal_expiry_removes_payload_and_refuses_pending_resurrection(self):
        self.start()
        self.peer("sender")
        self.peer("receiver", "claude")
        self.body(self.send(text="private synthetic body", key="old-key"))
        operation = next((self.base / "journal").glob("*/*.json"))
        entry = mailbox.load(operation)
        old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=8)).isoformat().replace("+00:00", "Z")
        entry["message"]["sentAt"] = old
        entry.pop("response", None)
        mailbox.write(operation, entry)
        message_path = mailbox.inbox_dir(self.base, "receiver") / (entry["message"]["id"] + ".json")
        message = mailbox.load(message_path)
        message["sentAt"] = old
        mailbox.write(message_path, message)
        mailbox.prune(self.base)
        self.assertNotIn("private synthetic body", operation.read_text())
        self.assertEqual(mailbox.load(operation)["schema"], "agent-executor.send-operation.expired.v1")
        replay = self.send(text="private synthetic body", key="old-key")[1]
        self.assertTrue(replay["result"]["isError"])
        self.assertEqual(mailbox.messages(self.base, "receiver"), [])

    def test_registration_retry_reuses_single_matching_native_pid(self):
        self.start()
        arguments = {"cwd": str(self.project), "harness": "gemini", "pid": os.getpid()}
        first = self.body(self.call("register", arguments, session=None))
        again = self.body(self.call("register", arguments, session=None))
        self.assertEqual(first, again)
        self.assertEqual([peer["session"] for peer in mailbox.peers(self.base)], [first["session"]])

    def test_legacy_2025_03_batch_support(self):
        self.start()
        status, value, headers = self.rpc(
            "initialize", {"protocolVersion": "2025-03-26", "clientInfo": {"name": "contract-client"}}, modern=False
        )
        self.assertEqual(value["result"]["protocolVersion"], "2025-03-26")
        parsed = urllib.parse.urlparse(self.state["url"])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=4)
        try:
            body = [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ]
            connection.request(
                "POST",
                "/mcp",
                json.dumps(body),
                {
                    "Authorization": "Bearer " + daemon.read_token(self.base),
                    "Content-Type": "application/json",
                    "Mcp-Session-Id": headers["Mcp-Session-Id"],
                },
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual([v["id"] for v in result], [1, 2])
            self.assertEqual(result[0]["result"], {})
            self.assertIn("register", [t["name"] for t in result[1]["result"]["tools"]])
        finally:
            connection.close()

    def test_legacy_batch_shutdown_preserves_committed_responses(self):
        for following_method in ("ping", "tools/call"):
            with self.subTest(following_method=following_method):
                self.base = Path(self.temp.name) / following_method.replace("/", "-") / "mailbox-v1"
                (Path(self.temp.name) / "wake-count").unlink(missing_ok=True)
                self.peer("sender")
                self.peer("receiver", "claude")
                counter, child = self.fault_service(delay=0.5)
                _, _, headers = self.rpc("initialize", {"protocolVersion": "2025-03-26"}, modern=False)
                follow = {"jsonrpc": "2.0", "id": 2, "method": following_method}
                if following_method == "tools/call":
                    follow["params"] = {"name": "wait", "arguments": {"session": "sender", "timeoutSeconds": 3}}
                batch = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "send",
                            "arguments": {
                                "session": "sender",
                                "to": "receiver",
                                "text": "batch drain",
                                "idempotencyKey": "drain",
                            },
                        },
                    },
                    follow,
                ]

                def exchange(batch=batch, headers=headers):
                    parsed = urllib.parse.urlparse(self.state["url"])
                    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=4)
                    try:
                        connection.request(
                            "POST",
                            "/mcp",
                            json.dumps(batch),
                            {
                                "Authorization": "Bearer " + daemon.read_token(self.base),
                                "Content-Type": "application/json",
                                "Mcp-Session-Id": headers["Mcp-Session-Id"],
                            },
                        )
                        response = connection.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        connection.close()

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(exchange)
                    deadline = time.monotonic() + 3
                    while not counter.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(counter.exists())
                    self.assertTrue(daemon.stop_service(self.base))
                    status, responses = future.result(timeout=3)
                self.assertEqual(status, 200)
                self.assertEqual(responses[0]["result"]["structuredContent"]["wake"]["status"], "scheduled")
                self.assertEqual([response["id"] for response in responses], [1, 2])
                if following_method == "ping":
                    self.assertEqual(responses[1]["result"], {})
                else:
                    self.assertEqual(responses[1]["error"]["code"], -32000)
                self.assertEqual(len(mailbox.messages(self.base, "receiver")), 1)
                child.wait(timeout=5)
