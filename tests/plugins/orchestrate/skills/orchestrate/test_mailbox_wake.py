"""Observable wake behavior: native idle activation, busy deferral and durable fallback."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
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


class AgyServer:
    """A loopback stand-in for the Antigravity language server."""

    def __init__(self, status="CASCADE_RUN_STATUS_IDLE", trajectory=None):
        self.status = status
        self.trajectory = trajectory
        self.calls = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append((self.path.rsplit("/", 1)[-1], self.headers.get("x-codeium-csrf-token"), body))
                if self.path.endswith("GetCascadeTrajectory"):
                    reply = {"status": outer.status}
                    if outer.trajectory is not None:
                        reply["trajectory"] = outer.trajectory
                else:
                    reply = {}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()

    def sends(self):
        return [call for call in self.calls if call[0] == "SendUserCascadeMessage"]


class AntigravityWakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.sender = peer_mailbox.register(self.base, session="sender", harness="codex", cwd=str(SCRIPTS))
        self.target = peer_mailbox.register(self.base, session="agy-target", harness="gemini", cwd=str(SCRIPTS))

    def control(self, address, token="tok"):
        path = self.base / "wakes" / peer_mailbox.session_key("agy-target") / "agy-control.json"
        peer_mailbox.write(path, {"lsAddress": address, "csrfToken": token})

    def mail(self, key):
        return peer_mailbox.send(self.base, sender=self.sender, to="agy-target", text=key)

    def test_idle_session_is_woken_once_through_the_native_queue(self):
        with AgyServer() as server:
            self.control(server.address)
            self.mail("zero")
            self.mail("one")
            ids = [m["id"] for m in peer_mailbox.incoming(self.base, "agy-target")]
            result = mailbox_wake.wake(self.target, ids[0], base=self.base)
            again = mailbox_wake.wake(self.target, ids[1], base=self.base)
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(again["status"], "scheduled")
        sends = server.sends()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1], "tok")
        self.assertEqual(sends[0][2]["cascadeId"], "agy-target")
        self.assertEqual(sends[0][2]["deliveryStrategy"], "MESSAGE_DELIVERY_STRATEGY_WHEN_IDLE")
        self.assertFalse(sends[0][2]["blocking"])
        self.assertIn(ids[0], sends[0][2]["items"][0]["text"])

    def test_idle_session_wake_preserves_cascade_config_and_agent_spec(self):
        user_config = {"plannerConfig": {"planModel": "MODEL_PLACEHOLDER_M418"}}
        agent_spec = {"builtinAgent": {"defaultAgent": {"surface": "AGENT_SURFACE_CLI"}}}
        trajectory = {
            "steps": [
                {"userInput": {"userConfig": user_config, "userAgentSpec": agent_spec}},
                {"userInput": {"lastUserConfig": user_config}},
            ],
        }
        with AgyServer(trajectory=trajectory) as server:
            self.control(server.address)
            self.mail("wake-with-config")
            message_id = peer_mailbox.incoming(self.base, "agy-target")[0]["id"]
            result = mailbox_wake.wake(self.target, message_id, base=self.base)
        self.assertEqual(result["status"], "scheduled")
        sends = server.sends()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][2]["cascadeConfig"], user_config)
        self.assertEqual(sends[0][2]["customAgentSpec"], agent_spec)

    def test_busy_session_keeps_mail_for_the_stop_hook(self):
        with AgyServer("CASCADE_RUN_STATUS_RUNNING") as server:
            self.control(server.address)
            self.mail("busy")
            message = peer_mailbox.incoming(self.base, "agy-target")[0]["id"]
            result = mailbox_wake.wake(self.target, message, base=self.base)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(server.sends(), [])

    def test_unusable_control_endpoints_leave_mail_queued(self):
        self.mail("x")
        message = peer_mailbox.incoming(self.base, "agy-target")[0]["id"]
        self.assertEqual(mailbox_wake.wake(self.target, message, base=self.base)["status"], "unavailable")
        self.control("example.com:80")
        self.assertEqual(mailbox_wake.wake(self.target, message, base=self.base)["status"], "unavailable")
        self.control("127.0.0.1:1")
        self.assertEqual(mailbox_wake.wake(self.target, message, base=self.base)["status"], "unavailable")
        self.assertEqual(len(peer_mailbox.incoming(self.base, "agy-target")), 1)

    def test_stream_json_listener_sessions_still_use_the_listener(self):
        self.mail("x")
        message = peer_mailbox.incoming(self.base, "agy-target")[0]["id"]
        with patch(
            "mailbox_listener.request_wake", return_value={"status": "scheduled", "reason": "listener"}
        ) as listener:
            peer_mailbox.annotate(self.base, "agy-target", listener={"adapter": "stream-json", "pid": 1})
            result = mailbox_wake.wake(self.target, message, base=self.base)
        self.assertEqual(result["reason"], "listener")
        listener.assert_called_once()


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

    def test_claude_interactive_session_wakes_over_local_uds_and_defers_when_busy(self):
        import hashlib
        import os
        import socket

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "mailbox"
            config_dir = Path(temporary) / "claude-config"
            sessions_dir = config_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            target = peer_mailbox.register(
                base, session="claude-interactive", harness="claude", cwd=str(SCRIPTS), pid=os.getpid()
            )
            sock_path = Path(f"/tmp/cc-wake-test-{os.getpid()}.sock")
            sock_path.unlink(missing_ok=True)
            received: list[str] = []
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(sock_path))
            server.listen(4)

            def accept_loop():
                while True:
                    try:
                        conn, _ = server.accept()
                    except OSError:
                        break
                    with conn:
                        received.append(conn.recv(8192).decode("utf-8"))
                        received_event.set()

            received_event = threading.Event()
            thread = threading.Thread(target=accept_loop, daemon=True)
            thread.start()
            try:
                canonical = os.path.abspath(str(sock_path))
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                token = "a" * 32
                peer_mailbox.write(
                    sessions_dir / f"{os.getpid()}.{digest}.key",
                    {"peerToken": token, "pidDomain": "darwin"},
                )
                meta_path = sessions_dir / f"{os.getpid()}.json"
                peer_mailbox.write(
                    meta_path,
                    {
                        "pid": os.getpid(),
                        "sessionId": "claude-interactive",
                        "messagingSocketPath": str(sock_path),
                        "status": "idle",
                    },
                )
                with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
                    first = peer_mailbox.send(base, sender=sender, to="claude-interactive", text="one", wake=True)
                    second = peer_mailbox.send(base, sender=sender, to="claude-interactive", text="two", wake=True)
                    self.assertEqual(first["wake"]["status"], "scheduled")
                    self.assertEqual(second["wake"]["status"], "scheduled")
                    self.assertTrue(received_event.wait(1.0))
                    self.assertEqual(len(received), 1)
                    lines = [json.loads(line) for line in received[0].splitlines()]
                    self.assertEqual(lines[0], {"type": "auth", "token": token})
                    self.assertEqual(lines[1]["type"], "user")
                    self.assertEqual(lines[1]["session_id"], "claude-interactive")
                    self.assertIn(first["id"], lines[1]["message"]["content"])
                    peer_mailbox.write(
                        meta_path,
                        {
                            "pid": os.getpid(),
                            "sessionId": "claude-interactive",
                            "messagingSocketPath": str(sock_path),
                            "status": "busy",
                        },
                    )
                    busy = mailbox_wake.wake(target, second["id"], base=base)
                    self.assertEqual(busy["status"], "queued")
                    # A Claude session in "waiting" status (e.g. waiting for input) is also wakeable.
                    peer_mailbox.write(
                        meta_path,
                        {
                            "pid": os.getpid(),
                            "sessionId": "claude-interactive",
                            "messagingSocketPath": str(sock_path),
                            "status": "waiting",
                        },
                    )
                    # Once a turn hook runs, the wake deduplication marker is cleared so a new wake can fire.
                    peer_mailbox.hook(
                        {"hook_event_name": "Stop", "session_id": "claude-interactive", "cwd": str(SCRIPTS)},
                        harness="claude",
                        base=base,
                    )
                    received_event.clear()
                    third = peer_mailbox.send(base, sender=sender, to="claude-interactive", text="three", wake=True)
                    self.assertEqual(third["wake"]["status"], "scheduled")
                    self.assertTrue(received_event.wait(1.0))
                    self.assertEqual(len(received), 2)
            finally:
                server.close()
                sock_path.unlink(missing_ok=True)

    def test_post_stop_turn_ending_grace_waits_for_native_idle_and_stop_continuation_stays_busy(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sender = peer_mailbox.register(base, session="sender", harness="gemini", cwd=str(SCRIPTS))
            peer_mailbox.register(
                base, session="target", harness="codex", cwd=str(SCRIPTS), state="idle", cwd_source="hook"
            )
            native = NativeQueue(state="active")
            reads = 0
            original = native.request

            def request(method, params):
                nonlocal reads
                if method == "thread/read":
                    reads += 1
                    if reads >= 2:
                        native.state = "idle"
                return original(method, params)

            native.request = request
            with patch.object(mailbox_wake, "CodexSocket", return_value=native):
                sent = peer_mailbox.send(base, sender=sender, to="target", text="Right after Stop", wake=True)
            self.assertEqual(sent["wake"]["status"], "scheduled")
            self.assertGreaterEqual(reads, 2)

            # When Stop hook delivers unread mail and continues the turn, peer state remains busy.
            peer_mailbox.send(base, sender=sender, to="target", text="Continue turn", wake=False)
            out = peer_mailbox.hook(
                {"hook_event_name": "Stop", "session_id": "target", "cwd": str(SCRIPTS)},
                harness="codex",
                base=base,
            )
            self.assertEqual(out["decision"], "block")
            after = peer_mailbox.load(peer_mailbox.peer_path(base, "target"))
            self.assertEqual(after["state"], "busy")


if __name__ == "__main__":
    unittest.main()
