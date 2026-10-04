"""Persistent Claude/Gemini streaming sessions, with mailbox work queued between turns."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import peer_mailbox as mailbox
from mailbox_wake import notification


def wake_directory(base: Path, session: str) -> Path:
    return base / "wakes" / mailbox.session_key(session)


def request_wake(base: Path | None, target: dict, message_id: str) -> dict:
    base = base or mailbox.root()
    current = mailbox.load(mailbox.peer_path(base, target["session"])) or {}
    listener = current.get("listener") or {}
    if listener.get("adapter") != "stream-json" or not mailbox.pid_alive(listener.get("pid")):
        return {"status": "unavailable", "reason": "start this harness with mailbox_listener.py to enable idle wakes"}
    if mailbox.process_started(listener["pid"]) != listener.get("pidBirth"):
        return {"status": "unavailable", "reason": "listener process ended; message remains queued"}
    # Only durable, unread mail in this project may produce a notification. Quiet sends never get a marker.
    eligible = {message["id"] for message in mailbox.incoming(base, target["session"])}
    if message_id not in eligible:
        return {"status": "queued", "reason": "message was already handled or is outside this project"}
    path = wake_directory(base, target["session"]) / f"{mailbox.session_key(message_id)}.json"
    with mailbox.locked(path):
        if not path.exists():
            mailbox.write(path, {"id": message_id, "requestedAt": mailbox.stamp(), "deliveredAt": None})
    return {"status": "scheduled", "reason": "listener admitted notification; busy turns finish before delivery"}


class StreamingSession:
    def __init__(self, process: subprocess.Popen, engine: str, base: Path, session: str):
        self.process = process
        self.engine = engine
        self.base = base
        self.session = session
        self.busy = False
        self.ready = False
        self.native_session: str | None = None

    def observe(self, event: dict) -> None:
        if self.engine == "agy":
            initialized = event.get("event") == "init"
            finished = event.get("event") == "result"
            native = event.get("conversation_id") or event.get("result", {}).get("conversation_id")
        else:
            initialized = event.get("type") == "system" and event.get("subtype") == "init"
            finished = event.get("type") == "result"
            native = event.get("session_id")
        if native:
            self.native_session = native
        if initialized or finished:
            self.ready = True
        if finished:
            self.busy = False
        if initialized or finished:
            self.presence()

    def presence(self) -> None:
        mailbox.register(
            self.base,
            session=self.session,
            harness="gemini" if self.engine == "agy" else "claude",
            cwd=os.getcwd(),
            pid=self.process.pid,
            state="busy" if self.busy else "idle",
        )
        mailbox.annotate(
            self.base,
            self.session,
            nativeSession=self.native_session,
            listener={"adapter": "stream-json", "pid": os.getpid(), "pidBirth": mailbox.process_started(os.getpid())},
        )

    def submit(self, text: str) -> None:
        if self.busy:
            raise RuntimeError("the current turn must finish before the next submission")
        payload = (
            {"event": "user", "message": {"content": text}}
            if self.engine == "agy"
            else {"type": "user", "message": {"role": "user", "content": text}}
        )
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        self.busy = True
        self.presence()

    def deliver_mail(self) -> bool:
        if self.busy or not self.ready:
            return False
        unread = {message["id"] for message in mailbox.incoming(self.base, self.session)}
        markers = []
        for path in sorted(wake_directory(self.base, self.session).glob("*.json")):
            item = mailbox.load(path)
            if item and not item.get("deliveredAt") and item.get("id") in unread:
                markers.append((path, item))
        if not markers:
            return False
        # Coalesce notices. The model reads the real mailbox; notification submission never claims mail.
        self.submit(notification(markers[0][1]["id"]))
        for path, item in markers:
            item["deliveredAt"] = mailbox.stamp()
            mailbox.write(path, item)
        return True


def read_lines(stream, events: queue.Queue, source: str) -> None:
    for line in stream:
        events.put((source, line))
    events.put((source, None))


def listen(engine: str, prompt: str | None, stay_open: bool, arguments: list[str]) -> int:
    session = str(uuid.uuid4())
    base = mailbox.root()
    command = [engine, "--input-format", "stream-json", "--output-format", "stream-json"]
    if engine == "claude":
        command += ["--print", "--verbose", "--session-id", session]
    # Reject switches that change the owned transport or create a different session identity.
    reserved = {"--input-format", "--output-format", "--session-id", "--print", "-p", "--prompt"}
    if any(argument.split("=", 1)[0] in reserved for argument in arguments):
        raise ValueError("listener owns input/output formats and the session identity; use --prompt for initial work")
    command += arguments
    environment = {**os.environ, "AGENT_MAILBOX_LISTENER_SESSION": session}
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1, env=environment
    ) as process:
        receiver = StreamingSession(process, engine, base, session)
        events: queue.Queue = queue.Queue()
        threading.Thread(target=read_lines, args=(process.stdout, events, "output"), daemon=True).start()
        threading.Thread(target=read_lines, args=(sys.stdin, events, "input"), daemon=True).start()
        pending = [prompt] if prompt else []
        closing = False
        receiver.presence()
        print(f"Mailbox listener: {session} ({engine}); enter prompts, EOF closes after queued work.", file=sys.stderr)
        try:
            # Claude emits init only after the first user input. An explicit prompt starts it; otherwise
            # its transport can accept the first mailbox notice without waiting for a handshake event.
            receiver.ready = engine == "claude"
            while process.poll() is None:
                try:
                    source, line = events.get(timeout=0.1)
                except queue.Empty:
                    source, line = "tick", ""
                if source == "output":
                    if line is None:
                        break
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        receiver.observe(event)
                elif source == "input":
                    if line is None:
                        closing = not stay_open
                    elif line.strip():
                        pending.append(line.rstrip("\n"))
                if not receiver.busy:
                    if pending:
                        receiver.submit(pending.pop(0))
                    elif receiver.deliver_mail():
                        pass
                    elif closing:
                        process.stdin.close()
                        break
            if not process.stdin.closed:
                process.stdin.close()
            return process.wait()
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            mailbox.annotate(base, session, listener=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, choices=("claude", "agy"))
    parser.add_argument("--prompt", help="initial authorized user task")
    parser.add_argument("--stay-open", action="store_true", help="keep listening for mailbox requests after stdin EOF")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="native CLI arguments after --")
    args = parser.parse_args(argv)
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    try:
        return listen(args.engine, args.prompt, args.stay_open, arguments)
    except (OSError, ValueError) as error:
        print(f"mailbox listener: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
