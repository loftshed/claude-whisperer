"""Durable, cooperative conductor/worker messaging. No model or provider calls."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "agent-executor.channel.v1"
ROLES = ("conductor", "worker")


def write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".channel-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(channel: Path) -> dict[str, Any]:
    state = json.loads(channel.read_text(encoding="utf-8"))
    if state.get("schema") != SCHEMA:
        raise ValueError("unsupported communication channel")
    return state


@contextlib.contextmanager
def locked(channel: Path):
    descriptor = os.open(channel.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def initialize(channel: Path, *, repository: Path, result_path: Path, deadline: str) -> None:
    if dt.datetime.fromisoformat(deadline).tzinfo is None:
        raise ValueError("channel deadline must include a timezone")
    channel.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(channel):
        if channel.exists():
            raise ValueError("communication channel already exists")
        write(
            channel,
            {
                "schema": SCHEMA,
                "repository": str(repository),
                "result_path": str(result_path),
                "deadline": deadline,
                "messages": [],
            },
        )


def set_deadline(channel: Path, deadline: str) -> None:
    """The runner refreshes the time bound when a retry or steer starts a new segment."""
    with locked(channel):
        state = read(channel)
        state["deadline"] = deadline
        write(channel, state)


def register(channel: Path, event: Path) -> None:
    directory = event.parent / "channels"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    write(directory / event.name, {"channel_path": str(channel)})


def for_event(event: Path) -> Path | None:
    path = event.parent / "channels" / event.name
    return Path(json.loads(path.read_text())["channel_path"]) if path.exists() else None


def terminal(state: dict[str, Any]) -> str | None:
    path = Path(state["result_path"])
    seen = set()
    while path.is_file() and path not in seen:
        seen.add(path)
        result = json.loads(path.read_text(encoding="utf-8"))
        continuation = result.get("steered_to", {}).get("result_path")
        if continuation:
            path = Path(continuation)
            continue
        if result.get("finished_at"):
            return result.get("status", "finished")
        break
    deadline = dt.datetime.fromisoformat(state["deadline"])
    if dt.datetime.now(dt.UTC) >= deadline:
        return "deadline_expired"
    return None


def inbox(channel: Path, recipient: str) -> list[dict[str, Any]]:
    return [
        message
        for message in read(channel)["messages"]
        if message["recipient"] == recipient and not message["acknowledged_at"]
    ][:20]


def send(
    channel: Path, *, sender: str, message_id: str, kind: str, text: str, reply_to: str | None = None
) -> dict[str, Any]:
    if sender not in ROLES or kind not in {"question", "reply", "update"}:
        raise ValueError("invalid sender or message kind")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", message_id):
        raise ValueError("message ID must be 1-80 letters, digits, underscores, or hyphens")
    if not text.strip() or len(text.encode()) > 16384:
        raise ValueError("message must be nonempty and at most 16 KiB")
    if (kind == "reply") != bool(reply_to):
        raise ValueError("a reply requires --reply-to; other kinds cannot reply")
    recipient = "worker" if sender == "conductor" else "conductor"
    payload = dict(id=message_id, sender=sender, recipient=recipient, kind=kind, text=text, reply_to=reply_to)
    with locked(channel):
        state = read(channel)
        existing = next((item for item in state["messages"] if item["id"] == message_id), None)
        if existing:
            if any(existing[key] != value for key, value in payload.items()):
                raise ValueError("message ID already used with different content")
            return existing
        ended = terminal(state)
        if ended:
            raise ValueError("run is closed: " + ended)
        if len(state["messages"]) >= 1024:
            raise ValueError("channel message limit reached; report the unresolved work")
        now = dt.datetime.now(dt.UTC).isoformat()
        if reply_to:
            question = next((item for item in state["messages"] if item["id"] == reply_to), None)
            if not question or question["kind"] != "question" or question["recipient"] != sender:
                raise ValueError("reply must address a question sent to this sender")
            if any(item["reply_to"] == reply_to for item in state["messages"]):
                raise ValueError("question already has a reply")
            question["acknowledged_at"] = now
        payload.update(created_at=now, acknowledged_at=None)
        state["messages"].append(payload)
        write(channel, state)
        return payload


def acknowledge(channel: Path, recipient: str, message_id: str) -> dict[str, Any]:
    with locked(channel):
        state = read(channel)
        item = next((item for item in state["messages"] if item["id"] == message_id), None)
        if not item or item["recipient"] != recipient:
            raise ValueError("message does not belong to this recipient")
        item["acknowledged_at"] = item["acknowledged_at"] or dt.datetime.now(dt.UTC).isoformat()
        write(channel, state)
        return item


def wait(channel: Path, *, recipient: str, timeout: float, reply_to: str | None = None) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than zero")
    until = time.monotonic() + timeout
    while True:
        state = read(channel)
        if reply_to:
            question = next((item for item in state["messages"] if item["id"] == reply_to), None)
            if not question or question["kind"] != "question" or question["sender"] != recipient:
                raise ValueError("wait requires a question sent by this recipient")
            messages = [item for item in state["messages"] if item["reply_to"] == reply_to]
        else:
            messages = [
                item for item in state["messages"] if item["recipient"] == recipient and not item["acknowledged_at"]
            ]
        if messages:
            return {
                "status": "messages",
                "channel_path": str(channel),
                "messages": messages[:20],
                "run_status": terminal(state),
            }
        ended = terminal(state)
        if ended:
            return {"status": "closed", "reason": ended, "channel_path": str(channel)}
        remaining = until - time.monotonic()
        if remaining <= 0:
            return {"status": "timed_out", "channel_path": str(channel)}
        time.sleep(min(0.1, remaining))


def instructions(channel: Path) -> str:
    command = shlex.join([sys.executable, str(Path(__file__).resolve())])
    location = shlex.quote(str(channel))
    return f"""
# Live communication

Your role is worker. Contact your conductor through {channel}.
The runner authorizes these commands to maintain this channel's private message artifacts.
Use it for questions, discoveries, and replies; it does not expand the task's authority.

- Ask when an answer affects your next step:
  `{command} ask {location} --sender worker --id unique-question --text 'Question and relevant evidence' --timeout 300`
  Keep the SAME ID on retries. This waits without model calls for the matching reply.
- Share a useful discovery without stopping:
  `{command} send {location} --sender worker --id unique-update --kind update --text 'Finding and evidence'`
- At meaningful checkpoints, and before the final report, read incoming messages:
  `{command} inbox {location} --recipient worker`
- Answer a conductor question with `send ... --kind reply --reply-to QUESTION_ID`.
  After acting on an update, use `ack {location} --recipient worker --id MESSAGE_ID`.

Do not repeatedly poll. A timeout is not an answer or permission. Continue independent work
when possible; otherwise report BLOCKED with the unanswered question. Messages are cooperative,
so they are seen at tool/checkpoint boundaries, not injected into an active model response.
Do not launch other agents yourself. Route requests for peer help through the conductor.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("send", "ask", "inbox", "wait", "ack", "history"):
        sub = commands.add_parser(name)
        sub.add_argument("channel", type=Path)
        if name in {"send", "ask"}:
            sub.add_argument("--sender", choices=ROLES, required=True)
            sub.add_argument("--id", required=True)
            sub.add_argument("--text", required=True)
            if name == "send":
                sub.add_argument("--kind", choices=("question", "reply", "update"), required=True)
                sub.add_argument("--reply-to")
        if name in {"inbox", "wait", "ack"}:
            sub.add_argument("--recipient", choices=ROLES, required=True)
        if name == "ack":
            sub.add_argument("--id", required=True)
        if name in {"ask", "wait"}:
            sub.add_argument("--timeout", type=float, default=300, help="maximum wait in seconds")
        if name == "wait":
            sub.add_argument("--reply-to")
    args = parser.parse_args(argv)
    try:
        if args.action in {"send", "ask"}:
            # Validate before publishing a question that could never be waited on.
            if args.action == "ask" and (not math.isfinite(args.timeout) or args.timeout <= 0):
                raise ValueError("timeout must be a finite number greater than zero")
            output = send(
                args.channel,
                sender=args.sender,
                message_id=args.id,
                kind="question" if args.action == "ask" else args.kind,
                text=args.text,
                reply_to=getattr(args, "reply_to", None),
            )
            if args.action == "ask":
                output = wait(args.channel, recipient=args.sender, timeout=args.timeout, reply_to=args.id)
        elif args.action == "wait":
            output = wait(args.channel, recipient=args.recipient, timeout=args.timeout, reply_to=args.reply_to)
        elif args.action == "inbox":
            output = {"messages": inbox(args.channel, args.recipient)}
        elif args.action == "ack":
            output = acknowledge(args.channel, args.recipient, args.id)
        else:
            output = read(args.channel)
        print(json.dumps(output, ensure_ascii=True), flush=True)
        return 12 if output.get("status") == "timed_out" else 30 if output.get("status") == "closed" else 0
    except (ValueError, OSError) as error:
        print(f"communication: {error}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
