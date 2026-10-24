"""Durable, bounded recovery for wakes that did not reach an idle recipient.

A send that asked to wake its recipient records one pending intent per recipient. The mailbox daemon
reconciles the intents every few seconds: it forgets mail the recipient has read, retries a wake only
while the recipient is strictly idle, backs off between attempts, and past its limits marks the intent
recovery-needed until `mailbox wake-retry`. Quiet mail never creates an intent. A retry never
interrupts a turn, submits through an approval wait, or sets going a conversation whose last turn
ended in a stop that forbids continuing (a user cancel, a run budget, an unclassified error); neither
does `wake-retry`.
"""

from __future__ import annotations

import datetime as dt
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mailbox_wake
import peer_mailbox as mailbox

# Seconds to wait after each attempt that sent nothing; the last value repeats.
BACKOFF = (2, 5, 15, 60, 300)
MAX_ATTEMPTS = 6
MAX_AGE = dt.timedelta(minutes=30)
# A notice whose mail is still unread after this has unconfirmed delivery, so it may be submitted again.
ACCEPT_WINDOW = dt.timedelta(seconds=20)
RECONCILE_EVERY = 3.0
# Stops after which automatic activation must wait for the recipient's user: a cancel, a run-budget
# limit, or an error nobody has classified as transient. Only that user's next prompt clears them.
PARKED = frozenset(mailbox.ANTIGRAVITY_NO_CONTINUE)
MARKERS = ("claude-wake.json", "agy-wake.json")
# Phases after which a notice may already be on its way: wait out the accept window before sending again.
MAYBE_SENT = {"submitted", "unknown"}


def intent_path(base: Path, session: str) -> Path:
    return base / "wakes" / mailbox.session_key(session) / "pending.json"


def later(moment: dt.datetime, seconds: float) -> str:
    return mailbox.stamp(moment + dt.timedelta(seconds=seconds))


def record_intent(base: Path, target: dict[str, Any], message_id: str, result: dict[str, Any]) -> None:
    """Fold a wake-requesting message into its recipient's pending intent, after the first attempt."""
    if result.get("status") == "unsupported" or not mailbox_wake.unread(base, target, message_id):
        return
    path = intent_path(base, target["session"])
    moment = mailbox.now()
    with mailbox.locked(path):
        intent = mailbox.load(path) or {}
        messages = [m for m in intent.get("messages", []) if isinstance(m, str) and m != message_id]
        submitted = result.get("phase") in MAYBE_SENT
        fresh = {
            "session": target["session"],
            # A new request gets its own bounded chance, even after an earlier intent ran out.
            "createdAt": mailbox.stamp(moment),
            "attempts": 0,
            "messages": [*messages, message_id],
            "state": "submitted" if submitted else "pending",
            "lastStatus": result.get("status"),
            "lastReason": result.get("reason"),
        }
        if submitted:
            fresh.update(submittedAt=mailbox.stamp(moment), submittedFor=message_id)
        elif intent.get("state") == "submitted" and intent.get("submittedAt"):
            # An earlier notice is still within its window; keep waiting on it rather than resubmitting.
            fresh.update(state="submitted", submittedAt=intent["submittedAt"], submittedFor=intent.get("submittedFor"))
        else:
            fresh["nextAt"] = later(moment, BACKOFF[0])
        mailbox.write(path, fresh)


def parse(value: Any) -> dt.datetime | None:
    try:
        return mailbox.parse_stamp(value)
    except (AttributeError, TypeError, ValueError):
        return None


def eligible(peer: dict[str, Any]) -> tuple[bool, str]:
    """Whether a retry may go out now: strictly idle, and its last turn did not end in a parking stop."""
    if (reason := peer.get("lastStopReason")) in PARKED:
        return False, f"its last turn ended with {reason}; waiting for its user's next prompt"
    if peer.get("state") != "idle":
        return False, f"recipient is {peer.get('state') or 'in an unknown state'}"
    return True, ""


def lift_unconfirmed_markers(base: Path, session: str, messages: list[str], moment: dt.datetime) -> None:
    """Remove a notice marker for this mail once its accept window passed with the mail unread (delivery
    unconfirmed, not proven lost), so a retry can submit again."""
    directory = base / "wakes" / mailbox.session_key(session)
    for name in MARKERS:
        marker = mailbox.load(directory / name) or {}
        requested = parse(marker.get("requestedAt"))
        if marker.get("id") in messages and requested and moment - requested >= ACCEPT_WINDOW:
            (directory / name).unlink(missing_ok=True)


def reconcile_one(
    base: Path, path: Path, moment: dt.datetime, wake: Callable[..., dict[str, Any]]
) -> dict[str, Any] | None:
    """One step for one recipient's intent, under its lock. Returns what happened, or None if nothing did."""
    with mailbox.locked(path):
        intent = mailbox.load(path)
        session = intent.get("session") if isinstance(intent, dict) else None
        if not isinstance(session, str):
            path.unlink(missing_ok=True)
            return None
        peer = mailbox.load(mailbox.peer_path(base, session))
        if not mailbox.valid_peer(peer) or mailbox.is_run_peer(peer) or not mailbox.is_live(peer, moment):
            path.unlink(missing_ok=True)
            return {"session": session, "outcome": "recipient gone"}
        unread = {message["id"] for message in mailbox.incoming(base, session)}
        messages = [m for m in intent.get("messages", []) if m in unread]
        if not messages:
            path.unlink(missing_ok=True)
            return {"session": session, "outcome": "delivered"}
        intent["messages"] = messages
        state = intent.get("state")
        if state == "recovery-needed":
            mailbox.write(path, intent)
            return None
        created = parse(intent.get("createdAt")) or moment
        if moment - created > MAX_AGE or intent.get("attempts", 0) >= MAX_ATTEMPTS:
            intent.update(state="recovery-needed", recoveryNeededAt=mailbox.stamp(moment))
            mailbox.write(path, intent)
            return {"session": session, "outcome": "recovery-needed"}
        if state == "submitted":
            submitted = parse(intent.get("submittedAt"))
            if submitted and moment - submitted < ACCEPT_WINDOW:
                mailbox.write(path, intent)
                return None
        else:
            due = parse(intent.get("nextAt"))
            if due and moment < due:
                mailbox.write(path, intent)
                return None
        ready, reason = eligible(peer)
        if not ready:
            # Not an attempt: check again later without spending the attempt budget.
            intent.update(state="pending", nextAt=later(moment, BACKOFF[0]), lastReason=reason)
            mailbox.write(path, intent)
            return {"session": session, "outcome": "waiting", "reason": reason}
        lift_unconfirmed_markers(base, session, messages, moment)
        result = wake(peer, messages[-1], base=base, retry=True)
        attempts = int(intent.get("attempts", 0)) + 1
        intent.update(attempts=attempts, lastStatus=result.get("status"), lastReason=result.get("reason"))
        if result.get("phase") in MAYBE_SENT:
            intent.update(state="submitted", submittedAt=mailbox.stamp(moment), submittedFor=messages[-1])
            intent.pop("nextAt", None)
        else:
            intent.update(state="pending", nextAt=later(moment, BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]))
        mailbox.write(path, intent)
        return {"session": session, "outcome": "attempted", "attempts": attempts, **result}


def reconcile(
    base: Path,
    *,
    moment: dt.datetime | None = None,
    wake: Callable[..., dict[str, Any]] | None = None,
    sessions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One pass over every pending intent, or only these recipients' (chosen before anything is sent);
    returns what happened, for logs and tests."""
    moment = moment or mailbox.now()
    wake = wake or mailbox_wake.wake
    only = None if sessions is None else {mailbox.session_key(session) for session in sessions}
    outcomes = []
    for path in sorted(base.glob("wakes/*/pending.json")):
        if only is not None and path.parent.name not in only:
            continue
        try:
            outcome = reconcile_one(base, path, moment, wake)
        except (OSError, ValueError, mailbox.MailboxError) as error:
            outcome = {"session": path.parent.name, "outcome": "error", "reason": str(error)}
        if outcome:
            outcomes.append(outcome)
    return outcomes


def reopen(base: Path, sessions: list[str] | None = None) -> list[str]:
    """Give recovery-needed intents (all, or these recipients') a fresh bounded chance. Returns sessions."""
    reopened = []
    moment = mailbox.now()
    for path in sorted(base.glob("wakes/*/pending.json")):
        with mailbox.locked(path):
            intent = mailbox.load(path) or {}
            if intent.get("state") != "recovery-needed" or (
                sessions is not None and intent.get("session") not in sessions
            ):
                continue
            intent.update(state="pending", attempts=0, createdAt=mailbox.stamp(moment), nextAt=mailbox.stamp(moment))
            intent.pop("recoveryNeededAt", None)
            mailbox.write(path, intent)
            reopened.append(intent["session"])
    return reopened


def summary(base: Path, session: str) -> dict[str, Any] | None:
    """The pending intent for one recipient, without message bodies, for wake-health."""
    intent = mailbox.load(intent_path(base, session))
    if not isinstance(intent, dict) or not intent.get("messages"):
        return None
    return {
        "state": intent.get("state"),
        "messages": len(intent["messages"]),
        "attempts": intent.get("attempts", 0),
        "lastReason": intent.get("lastReason"),
        "since": intent.get("createdAt"),
    }


def run(base: Path, stopping: threading.Event) -> None:
    """The daemon's reconciler loop, until the daemon stops. A failed pass never stops the loop."""
    while not stopping.wait(RECONCILE_EVERY):
        try:
            reconcile(base)
        except Exception as error:  # noqa: BLE001 -- the daemon keeps serving whatever one pass hits
            print(f"wake recovery: {error}", file=sys.stderr, flush=True)
