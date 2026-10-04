"""Run both adapters against persistent protocol processes and observe completed turns."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/agent-executor/skills/agent-executor/scripts"
sys.path.insert(0, str(SCRIPTS))
import mailbox_mcp  # noqa: E402
import mailbox_wake  # noqa: E402
import peer_mailbox as mailbox  # noqa: E402

PROTOCOL_PROCESS = r"""#!/usr/bin/env python3
import json, os, sys, time
engine = os.path.basename(sys.argv[0])
session = os.environ["AGENT_MAILBOX_LISTENER_SESSION"]
def emit(event):
    print(json.dumps(event), flush=True)
if engine == "agy":
    emit({"event": "init", "conversation_id": "native-gemini"})
for line in sys.stdin:
    payload = json.loads(line)
    if engine == "agy":
        assert payload["event"] == "user"
    else:
        assert payload["type"] == "user" and payload["message"]["role"] == "user"
        emit({"type": "system", "subtype": "init", "session_id": session})
    text = payload["message"]["content"]
    if text == "hold":
        while not os.path.exists(os.environ["TURN_RELEASE"]):
            time.sleep(0.01)
    if engine == "agy":
        emit({"event": "result", "result": {"status": "SUCCESS", "response": text, "conversation_id": "native-gemini"}})
    else:
        emit({"type": "result", "subtype": "success", "result": text, "session_id": session})
"""


def until(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("timed out waiting for observable listener state")


class ListenerTests(unittest.TestCase):
    def test_both_engines_wake_idle_defer_busy_and_keep_one_conversation(self):
        for engine in ("agy", "claude"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                binary = directory / engine
                binary.write_text(PROTOCOL_PROCESS)
                binary.chmod(0o755)
                base = directory / "mailbox-v1"
                released = directory / "released"
                environment = {
                    **os.environ,
                    "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                    "AGENT_EXECUTOR_HOME": temporary,
                    "TURN_RELEASE": str(released),
                }
                with subprocess.Popen(
                    [sys.executable, str(SCRIPTS / "run_agent.py"), "listen", "--engine", engine, "--prompt", "hold"],
                    cwd=SCRIPTS,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ) as listener:
                    events: queue.Queue = queue.Queue()

                    def read(events=events):
                        for line in listener.stdout:
                            events.put(json.loads(line))

                    threading.Thread(target=read, daemon=True).start()
                    try:
                        target = until(
                            lambda base=base: next(
                                (
                                    p
                                    for p in mailbox.peers(base, prune_dead=False)
                                    if p.get("listener") and p["state"] == "busy"
                                ),
                                None,
                            )
                        )
                        sender = mailbox.register(base, session="sender", harness="codex", cwd=str(SCRIPTS))
                        quiet = mailbox.send(base, sender=sender, to=target["session"], text="for later", wake=False)
                        first = mailbox.send(base, sender=sender, to=target["session"], text="review", wake=True)
                        second = mailbox.send(
                            base, sender=sender, to=target["session"], text="another finding", wake=True
                        )
                        self.assertEqual(
                            [first["wake"]["status"], second["wake"]["status"]], ["scheduled", "scheduled"]
                        )
                        self.assertNotIn("wake", quiet)
                        self.assertEqual(len(mailbox.incoming(base, target["session"])), 3)
                        released.touch()
                        results = []
                        while len(results) < 2:
                            event = events.get(timeout=8)
                            if event.get("event") == "result" or event.get("type") == "result":
                                results.append(event)
                        texts = [e["result"]["response"] if engine == "agy" else e["result"] for e in results]
                        self.assertEqual(texts[0], "hold")
                        self.assertIn("Local peer mailbox notification", texts[1])
                        # Notification admission keeps actual mail unread until the recipient reads it.
                        self.assertEqual(
                            [m["text"] for m in mailbox.incoming(base, target["session"])],
                            ["for later", "review", "another finding"],
                        )
                        until(
                            lambda base=base, target=target: (
                                (mailbox.load(mailbox.peer_path(base, target["session"])) or {}).get("state") == "idle"
                            )
                        )
                        third = mailbox.send(base, sender=sender, to=target["session"], text="idle ping", wake=True)
                        self.assertEqual(third["wake"]["status"], "scheduled")
                        while True:
                            event = events.get(timeout=8)
                            if event.get("event") == "result" or event.get("type") == "result":
                                break
                        response = event["result"]["response"] if engine == "agy" else event["result"]
                        self.assertIn(third["id"], response)
                        known = mailbox.load(mailbox.peer_path(base, target["session"]))
                        self.assertEqual(known["pid"], target["pid"])
                        self.assertEqual(
                            known["nativeSession"], "native-gemini" if engine == "agy" else target["session"]
                        )
                        listener.stdin.close()
                        self.assertEqual(listener.wait(timeout=8), 0)
                        self.assertEqual(mailbox_wake.wake(target, third["id"], base=base)["status"], "unavailable")
                    finally:
                        released.touch()
                        if not listener.stdin.closed:
                            listener.stdin.close()
                        listener.wait(timeout=8)

    def test_listener_identity_is_shared_with_mcp_tools(self):
        server = mailbox_mcp.Server(environ={"AGENT_MAILBOX_LISTENER_SESSION": "owned-session"})
        server.harness = "gemini"
        self.assertEqual(server.session({}), "owned-session")
        self.assertEqual(server.session({"sessionId": "other-id"}), "owned-session")


if __name__ == "__main__":
    unittest.main()
