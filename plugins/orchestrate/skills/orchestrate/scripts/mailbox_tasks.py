"""Compact, replaceable work notes that outlive mailbox presence. They grant no task ownership."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

import peer_mailbox as mailbox

SCHEMA = "orchestrate.task-note.v1"
LIMITS = {"done": 160, "next": 240, "blocker": 160, "refs": 240}
STATUSES = ("active", "blocked", "complete")
RETENTION = dt.timedelta(days=30)


def validate(details: dict[str, Any]) -> dict[str, str]:
    result = {}
    for field, value in details.items():
        if field == "status":
            if value not in STATUSES:
                raise mailbox.MailboxError("task status must be active, blocked, or complete")
            result[field] = value
        elif field not in LIMITS or not isinstance(value, str):
            raise mailbox.MailboxError(f"invalid task note field: {field}")
        else:
            value = " ".join(value.split())
            if len(value) > LIMITS[field]:
                raise mailbox.MailboxError(f"task {field} must be at most {LIMITS[field]} characters")
            result[field] = value
    return result


def path_for(base: Path, peer: dict[str, Any]) -> Path:
    # Keep a session's notes in different projects separate, even after it changes directories.
    project = mailbox.project(peer)
    identity = project.get("repo") or peer.get("cwd") or ""
    key = hashlib.sha256(str(identity).encode()).hexdigest()[:24]
    return base / "tasks" / key / f"{mailbox.session_key(peer['session'])}.json"


def fresh(note: dict[str, Any] | None) -> bool:
    if not note or note.get("schema") != SCHEMA or not mailbox.valid_stamp(note.get("updatedAt")):
        return False
    return mailbox.now() - mailbox.parse_stamp(note["updatedAt"]) <= RETENTION


def save(base: Path, peer: dict[str, Any], **details: Any) -> dict[str, Any]:
    """Replace this session's current card, preserving omitted fields only while its task stays the same."""
    details = validate(details)
    if mailbox.is_run_peer(peer):
        raise mailbox.MailboxError("run sessions report progress to their conductor")
    task = str(peer.get("focus") or "").strip()
    if not task:
        raise mailbox.MailboxError("set a task focus before saving a work note")
    path = path_for(base, peer)
    with mailbox.locked(path):
        previous = mailbox.load(path)
        kept = (
            {key: previous[key] for key in (*LIMITS, "status") if key in previous}
            if fresh(previous) and previous.get("task") == task
            else {}
        )
        note = {
            "schema": SCHEMA,
            "owner": mailbox.sender_address(peer),
            "task": task[:200],
            "checkout": peer.get("checkout") or peer.get("cwd"),
            "branch": peer.get("branch"),
            "updatedAt": mailbox.stamp(),
            "status": "active",
            **kept,
            **details,
        }
        mailbox.write(path, note)
    prune(base)
    return note


def prune(base: Path) -> None:
    for path in (base / "tasks").glob("*/*.json"):
        with mailbox.locked(path):
            if not fresh(mailbox.load(path)):
                path.unlink(missing_ok=True)


def listing(
    base: Path,
    reader: dict[str, Any],
    *,
    scope: str = "project",
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    mailbox.check_scope(scope)
    if not 1 <= limit <= 50 or offset < 0:
        raise mailbox.MailboxError("tasks needs limit 1-50 and a nonnegative offset")
    notes = []
    for path in (base / "tasks").glob("*/*.json"):
        note = mailbox.load(path)
        if not fresh(note) or not isinstance(note.get("owner"), dict):
            continue
        if scope == "project" and not mailbox.same_project(reader, note["owner"]):
            continue
        owner = mailbox.load(mailbox.peer_path(base, note["owner"]["session"]))
        notes.append({**note, "ownerPresent": bool(mailbox.valid_peer(owner) and mailbox.is_live(owner))})
    notes.sort(key=lambda note: (note["updatedAt"], note["owner"]["session"]), reverse=True)
    page = notes[offset : offset + limit]
    more = offset + len(page) < len(notes)
    return {"tasks": page, "has_more": more, "next_offset": offset + len(page) if more else None}


def render(result: dict[str, Any]) -> str:
    lines = []
    for note in result["tasks"]:
        presence = "present" if note["ownerPresent"] else "not present"
        lines.append(f"{note['owner'].get('name')} · {note['status']} · {presence} · {note['updatedAt']}")
        lines.append(f"Task: {note['task']}")
        lines.append(f"Where: {note.get('checkout')} [{note.get('branch') or 'unknown branch'}]")
        for field in LIMITS:
            if note.get(field):
                lines.append(f"{field.capitalize()}: {note[field]}")
        lines.append("")
    if not lines:
        return "No task notes in scope."
    lines.append("Saved context only. Verify the checkout and coordinate ownership before continuing work.")
    if result["has_more"]:
        lines.append(f"More task notes: offset={result['next_offset']}.")
    return "\n".join(lines)
