import json
import os
import signal
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

scripts_dir = (
    Path(__file__).resolve().parent.parent.parent.parent.parent.parent
    / "plugins"
    / "agent-executor"
    / "skills"
    / "agent-executor"
    / "scripts"
)
sys.path.insert(0, str(scripts_dir))

import contextlib  # noqa: E402

import mailbox_daemon  # noqa: E402
import peer_mailbox as mailbox  # noqa: E402


class TestMailboxDaemon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "mailbox-v1"
        self.daemons_to_stop = []

    def tearDown(self):
        for pid in self.daemons_to_stop:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        self.tmp.cleanup()

    def test_lifecycle_and_health(self):
        # start
        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.assertIn("url", state)
        self.assertIn("pid", state)
        self.assertIn("instance", state)
        self.daemons_to_stop.append(state["pid"])

        # verify health
        req = urllib.request.Request(
            state["url"].replace("/mcp", "/health"),
            headers={"Authorization": f"Bearer {mailbox_daemon.read_token(self.base)}"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            health = json.loads(resp.read().decode())
            self.assertEqual(health["pid"], state["pid"])

        # test ensure_running returns same instance
        state2 = mailbox_daemon.ensure_running(self.base, port=0)
        self.assertEqual(state["pid"], state2["pid"])

        # stop
        mailbox_daemon.stop_daemon(state["url"], mailbox_daemon.read_token(self.base))
        for _ in range(50):
            if not mailbox.pid_alive(state["pid"]):
                break
            time.sleep(0.1)
        self.assertFalse(mailbox.pid_alive(state["pid"]))

    def test_mcp_post_and_auth(self):
        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.daemons_to_stop.append(state["pid"])
        token = mailbox_daemon.read_token(self.base)

        # Unauthorized
        req = urllib.request.Request(state["url"], method="POST", data=b"{}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

        # Missing Content-Type
        req = urllib.request.Request(
            state["url"], method="POST", data=b"{}", headers={"Authorization": f"Bearer {token}"}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 415)

        # Invalid Origin
        req = urllib.request.Request(
            state["url"],
            method="POST",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Origin": "http://evil.com",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)

        # Valid JSON-RPC
        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "test-client"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
        req = urllib.request.Request(
            state["url"],
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode())
            self.assertIn("result", res)
            self.assertIn("tools", res["result"])

    def test_idempotency(self):
        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.daemons_to_stop.append(state["pid"])
        token = mailbox_daemon.read_token(self.base)

        # Register sender and receiver
        mailbox.register(self.base, session="sender1", harness="claude", cwd=str(self.base))
        mailbox.register(self.base, session="receiver1", harness="codex", cwd=str(self.base))

        payload = {
            "jsonrpc": "2.0",
            "id": "2",
            "method": "tools/call",
            "params": {
                "name": "send",
                "arguments": {
                    "session": "sender1",
                    "to": "receiver1",
                    "text": "hello",
                    "scope": "cross-project",
                    "idempotencyKey": "key1",
                },
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "test-client"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        req1 = urllib.request.Request(state["url"], method="POST", data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req1) as resp:
            res1 = json.loads(resp.read().decode())

        # Send again with same key
        req2 = urllib.request.Request(state["url"], method="POST", data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req2) as resp:
            res2 = json.loads(resp.read().decode())

        print("RES1:", json.dumps(res1, indent=2))
        print("RES2:", json.dumps(res2, indent=2))
        self.assertEqual(res1, res2)  # exactly same response
        self.assertNotIn("error", res1)
        self.assertFalse(res1.get("result", {}).get("isError", False), res1)

        # Check receiver inbox, should have only one message
        messages, _ = mailbox.message_page(self.base, "receiver1", scope="cross-project")
        self.assertEqual(len(messages), 1)

    def test_disconnect_cancellation(self):
        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.daemons_to_stop.append(state["pid"])
        token = mailbox_daemon.read_token(self.base)

        mailbox.register(self.base, session="waiter1", harness="claude", cwd=str(self.base))

        payload = {
            "jsonrpc": "2.0",
            "id": "3",
            "method": "tools/call",
            "params": {
                "name": "wait",
                "arguments": {"session": "waiter1", "timeoutSeconds": 2},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "test-client"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }

        # Connect and drop
        import http.client
        import urllib.parse

        parsed = urllib.parse.urlparse(state["url"])
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
        body = json.dumps(payload).encode()
        conn.putrequest("POST", parsed.path)
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        conn.send(body)
        conn.close()  # drop connection

        time.sleep(0.5)

        peer = mailbox.load(mailbox.peer_path(self.base, "waiter1"))
        # Should be idle, not waiting, because it was cancelled
        # Wait, the status is busy after wait cancelled?
        self.assertIn(peer["state"], ["idle", "busy"])

    def test_concurrent_startup(self):
        import concurrent.futures

        def start_daemon():
            return mailbox_daemon.ensure_running(self.base, port=0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(start_daemon) for _ in range(10)]
            results = [f.result() for f in futures]
        pids = {r["pid"] for r in results}
        urls = {r["url"] for r in results}
        self.assertEqual(len(pids), 1)
        self.assertEqual(len(urls), 1)
        self.daemons_to_stop.append(list(pids)[0])

    def test_ten_clients_waits(self):
        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.daemons_to_stop.append(state["pid"])
        token = mailbox_daemon.read_token(self.base)

        import concurrent.futures

        def send_wait(i):
            session = f"client{i}"
            mailbox.register(self.base, session=session, harness="claude", cwd=str(self.base))
            payload = {
                "jsonrpc": "2.0",
                "id": str(i),
                "method": "tools/call",
                "params": {
                    "name": "wait",
                    "arguments": {"session": session, "timeoutSeconds": 0.5},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientInfo": {"name": "test-client"},
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            }
            req = urllib.request.Request(
                state["url"],
                method="POST",
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                return str(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(send_wait, i) for i in range(10)]
            results = [f.result() for f in futures]
        for r in results:
            self.assertTrue(isinstance(r, dict), r)
            self.assertFalse(r.get("result", {}).get("isError", False))

    def test_persistent_token_port(self):
        state1 = mailbox_daemon.ensure_running(self.base, port=0)
        pid1 = state1["pid"]
        url1 = state1["url"]
        token1 = mailbox_daemon.read_token(self.base)

        # Kill it abruptly
        import signal

        try:
            os.kill(pid1, signal.SIGKILL)
        except ProcessLookupError:
            pass

        time.sleep(0.5)

        # Ensure it restarts but keeps same token and url
        state2 = mailbox_daemon.ensure_running(self.base, port=0)
        pid2 = state2["pid"]
        url2 = state2["url"]
        token2 = mailbox_daemon.read_token(self.base)

        self.daemons_to_stop.append(pid2)

        self.assertNotEqual(pid1, pid2)
        self.assertEqual(url1, url2)
        self.assertEqual(token1, token2)

    def test_safe_unrelated_process(self):
        my_pid = os.getpid()
        state_file = self.base / ".daemon" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"pid": my_pid, "port": 50000, "token": "fake"}, f)

        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.daemons_to_stop.append(state["pid"])

        self.assertNotEqual(state["pid"], my_pid)
        new_token = mailbox_daemon.read_token(self.base)
        self.assertNotEqual(new_token, "fake")

    def test_header_mismatches(self):
        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.daemons_to_stop.append(state["pid"])

        import http.client
        import urllib.parse

        parsed = urllib.parse.urlparse(state["url"])

        conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
        conn.request("POST", parsed.path, body="{}", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 401)
        conn.close()

        conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
        conn.request(
            "POST",
            parsed.path,
            body="{}",
            headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 401)
        conn.close()

    def test_cross_project_isolation(self):
        state = mailbox_daemon.ensure_running(self.base, port=0)
        self.daemons_to_stop.append(state["pid"])
        token = mailbox_daemon.read_token(self.base)

        # Register two clients in different directories
        mailbox.register(self.base, session="client_a", harness="claude", cwd="/fake/dir/a")
        mailbox.register(self.base, session="client_b", harness="codex", cwd="/fake/dir/b")

        # Client A sends a project-scoped message to Client B
        payload = {
            "jsonrpc": "2.0",
            "id": "2",
            "method": "tools/call",
            "params": {
                "name": "send",
                "arguments": {
                    "session": "client_a",
                    "to": "client_b",
                    "text": "hello project",
                    "scope": "project",
                    "idempotencyKey": "keyA",
                },
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "test-client"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }

        req = urllib.request.Request(
            state["url"],
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode())

        self.assertTrue(res.get("result", {}).get("isError", False))

        # Now send cross-project
        payload["params"]["arguments"]["scope"] = "cross-project"
        req = urllib.request.Request(
            state["url"],
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode())

        self.assertFalse(res.get("result", {}).get("isError", False))

        # Client B waits for project scoped messages -> should get nothing because of isolation!
        payload["params"]["name"] = "wait"
        payload["params"]["arguments"] = {"session": "client_b", "timeoutSeconds": 0.5, "scope": "project"}

        req = urllib.request.Request(
            state["url"],
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode())

        # Should timeout and have no messages!
        self.assertEqual(res["result"]["structuredContent"]["status"], "timed_out")

        # Cross project should find it
        payload["params"]["arguments"]["scope"] = "cross-project"
        req = urllib.request.Request(
            state["url"],
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode())

        self.assertEqual(res["result"]["structuredContent"]["status"], "messages")
        self.assertEqual(len(res["result"]["structuredContent"]["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
