"""Task channels: who in a project hears a post, so separate teams can work side by side.

An agent joins the channels of its task (`retrieval`, `graph`) and a channel post reaches only their live
members, each as an ordinary inbox message. Two audiences are reserved: `all` (everyone in the project,
worktrees and clones included) and `worktree` (everyone in the sender's checkout). A channel-less agent hears
`all`, `worktree` and direct messages, never a task channel. Channels are notification boundaries, not
privacy: anyone in the project may read a channel's history on request, and nothing is pushed to them.

A post freezes its recipients when it is accepted. With an idempotency key, a replay finishes the deliveries
it missed without reaching anyone who joined since and without delivering twice. Posts are quiet unless the
sender asks for a wake. They go through the mailbox to every member, including peers of the sender's own
harness and account: no native messaging tool has channels.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

import peer_mailbox as mailbox

SLUG = mailbox.CHANNEL
EVERYONE = "all"
CHECKOUT = "worktree"
RESERVED = {EVERYONE, CHECKOUT}
POST_SCHEMA = "agent-executor.channel-post.v1"
# Set by whoever launches a session to give it its first channels; read once, at its first registration.
LAUNCH_VARIABLE = "AGENT_MAILBOX_CHANNELS"


def check(channel: Any, *, audience: bool = False) -> str:
    """A valid channel name. `audience` also admits the reserved `all` and `worktree`."""
    if not isinstance(channel, str) or not re.fullmatch(SLUG, channel):
        raise mailbox.MailboxError("a channel name is 1-40 lowercase letters, digits, hyphens or underscores")
    if channel in RESERVED and not audience:
        raise mailbox.MailboxError(f"`{channel}` is a reserved audience, not a channel to join")
    return channel


def memberships(peer: dict[str, Any] | None) -> list[str]:
    channels = (peer or {}).get("channels")
    if not isinstance(channels, list):
        return []
    return sorted({c for c in channels if isinstance(c, str) and re.fullmatch(SLUG, c) and c not in RESERVED})


def launch_channels(environ: dict[str, str] | None = None) -> list[str]:
    """The channels a launcher assigned through the environment; invalid names are ignored."""
    raw = (os.environ if environ is None else environ).get(LAUNCH_VARIABLE, "")
    return sorted(
        {name.strip() for name in raw.split(",") if re.fullmatch(SLUG, name.strip()) and name.strip() not in RESERVED}
    )


def update(base: Path, session: str, *, join: list[str] = (), leave: list[str] = ()) -> dict[str, Any]:
    """Join and leave channels for this session; returns its record."""
    joining = {check(c) for c in join}
    leaving = {check(c) for c in leave}
    peer = mailbox.load(mailbox.peer_path(base, session))
    if not mailbox.valid_peer(peer) or mailbox.is_run_peer(peer):
        raise mailbox.MailboxError("register this interactive session before joining channels")
    current = set(memberships(peer))
    return mailbox.annotate(base, session, channels=sorted((current | joining) - leaving))


def audience(base: Path, sender: dict[str, Any], channel: str) -> list[dict[str, Any]]:
    """Who a post to this channel reaches right now: live project peers, never the sender or a run session."""
    check(channel, audience=True)
    peers = [
        peer
        for peer in mailbox.scoped_peers(base, sender)
        if peer["session"] != sender["session"] and not mailbox.is_run_peer(peer)
    ]
    if channel == EVERYONE:
        return peers
    if channel == CHECKOUT:
        here = sender.get("checkout") or sender.get("cwd")
        return [peer for peer in peers if (peer.get("checkout") or peer.get("cwd")) == here]
    return [peer for peer in peers if channel in memberships(peer)]


def post_path(base: Path, channel: str, post_id: str) -> Path:
    return base / "channels" / channel / f"{post_id}.json"


def delivery_key(post_id: str, session: str) -> str:
    """The per-recipient idempotency key: a replay of a post never delivers to anyone twice."""
    return "ch-" + hashlib.sha256(f"{post_id}\0{session}".encode()).hexdigest()[:40]


def post(
    base: Path,
    *,
    sender: dict[str, Any],
    channel: str,
    text: str,
    reply_to: str | None = None,
    wake: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Publish to a channel: freeze its recipients, keep the post, deliver one copy to each of them."""
    check(channel, audience=True)
    if mailbox.is_run_peer(sender):
        raise mailbox.MailboxError("run sessions report to their conductor, not to channels")
    if not text.strip():
        raise mailbox.MailboxError("message text is empty")
    if len(text.encode()) > mailbox.MAX_BYTES:
        raise mailbox.MailboxError(f"message is over {mailbox.MAX_BYTES // 1024} KiB; send a path to a file instead")
    if reply_to is not None and not re.fullmatch(mailbox.MESSAGE_ID, reply_to):
        raise mailbox.MailboxError(f"invalid replyTo id: {reply_to!r}")
    reply_to = mailbox.thread_id(base, sender["session"], reply_to)
    fingerprint = {"channel": channel, "text": text, "replyTo": reply_to, "wake": bool(wake)}
    operation = None
    if idempotency_key is not None:
        if not re.fullmatch(mailbox.CLIENT_ID, idempotency_key):
            raise mailbox.MailboxError("idempotencyKey must be 1-80 letters, digits, underscores or hyphens")
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        operation = base / "channel-ops" / mailbox.session_key(sender["session"]) / f"{digest}.json"
    with mailbox.locked(operation) if operation else _unlocked():
        entry = None
        if operation and operation.exists():
            entry = mailbox.load(operation)
            if entry is None:
                raise mailbox.MailboxError("channel post journal is unreadable; inspect storage before retrying")
            if entry.get("expired") or expired(entry):
                raise mailbox.MailboxError(
                    "idempotencyKey replay window expired after seven days; do not retry the old post"
                )
            if entry.get("fingerprint") != fingerprint:
                raise mailbox.MailboxError("that idempotencyKey was used for a different post; use a new key")
        if not entry:
            entry = {
                "postId": mailbox.message_id(),
                "fingerprint": fingerprint,
                "sender": snapshot(sender),
                "recipients": [{"session": p["session"], "name": p["name"]} for p in audience(base, sender, channel)],
                "acceptedAt": mailbox.stamp(),
            }
            if operation:
                mailbox.write(operation, entry)  # persisted before anything is published
        # A replay completes the post as it was accepted, in its original sender's context, from wherever it runs.
        publisher = entry.get("sender") or snapshot(sender)
        canonical = post_path(base, channel, entry["postId"])
        if not canonical.exists():
            mailbox.write(canonical, canonical_post(entry, publisher))
        outcomes = []
        for recipient in entry["recipients"]:
            try:
                sent = mailbox.send(
                    base,
                    sender=publisher,
                    to=recipient["session"],
                    text=text,
                    reply_to=reply_to,
                    wake=bool(wake),
                    idempotency_key=delivery_key(entry["postId"], recipient["session"]),
                    channel=channel,
                    post_id=entry["postId"],
                )
                outcomes.append(
                    {"to": recipient["name"], "id": sent["id"], **({"wake": sent["wake"]} if "wake" in sent else {})}
                )
            except mailbox.MailboxError as failure:
                outcomes.append({"to": recipient["name"], "error": str(failure)})
    return {
        "postId": entry["postId"],
        "channel": channel,
        "wakeRequested": bool(wake),
        "delivered": sum("id" in o for o in outcomes),
        "recipients": outcomes,
    }


SENDER_FIELDS = ("session", "name", "harness", "cwd", "checkout", "branch", "repo", "remotes", "accountHome")


def snapshot(sender: dict[str, Any]) -> dict[str, Any]:
    """The sender context a post was accepted in, kept for its replays."""
    return {key: sender[key] for key in SENDER_FIELDS if key in sender}


def expired(entry: dict[str, Any]) -> bool:
    try:
        return mailbox.now() - mailbox.parse_stamp(entry["acceptedAt"]) > mailbox.MAX_AGE
    except (KeyError, TypeError, ValueError, AttributeError):
        return True


def canonical_post(entry: dict[str, Any], publisher: dict[str, Any]) -> dict[str, Any]:
    """The post as kept for history, built from its accepted operation."""
    accepted = entry["fingerprint"]
    return {
        "schema": POST_SCHEMA,
        "postId": entry["postId"],
        "channel": accepted["channel"],
        "from": mailbox.sender_address(publisher),
        # Frozen with the post: a later change of directory never changes who `worktree` meant.
        "checkout": publisher.get("checkout") or publisher.get("cwd"),
        "text": accepted["text"],
        "replyTo": accepted["replyTo"],
        "wake": accepted["wake"],
        "sentAt": entry["acceptedAt"],
        "recipients": [recipient["name"] for recipient in entry["recipients"]],
    }


EXPIRED_SCHEMA = "agent-executor.channel-operation.expired.v1"


def prune_operations(base: Path) -> None:
    """Retire post operations past mail retention. A live sender keeps a body-free tombstone, so an old key
    is refused instead of posting again; the tombstone goes when its sender does."""
    for operation in (base / "channel-ops").glob("*/*.json"):
        with mailbox.locked(operation):
            entry = mailbox.load(operation)
            if entry is None:
                with contextlib.suppress(OSError):
                    if time.time() - operation.stat().st_mtime > mailbox.MAX_AGE.total_seconds():
                        operation.unlink()
                continue
            tombstone = entry.get("schema") == EXPIRED_SCHEMA
            if not tombstone and not expired(entry):
                continue
            session = entry.get("session") if tombstone else (entry.get("sender") or {}).get("session")
            peer = (
                mailbox.load(mailbox.peer_path(base, session))
                if isinstance(session, str) and re.fullmatch(mailbox.SESSION_ID, session)
                else None
            )
            if not (mailbox.valid_peer(peer) and mailbox.is_live(peer)):
                operation.unlink(missing_ok=True)
            elif not tombstone:
                mailbox.write(
                    operation,
                    {
                        "schema": EXPIRED_SCHEMA,
                        "expired": True,
                        "session": session,
                        "postId": entry.get("postId"),
                        "expiredAt": mailbox.stamp(),
                    },
                )


class _unlocked:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def visible(post: dict[str, Any] | None, reader: dict[str, Any]) -> bool:
    """A post this reader's project may read on request (a `worktree` post only in its own checkout)."""
    if not isinstance(post, dict) or post.get("schema") != POST_SCHEMA or not isinstance(post.get("from"), dict):
        return False
    if not mailbox.same_project(reader, post["from"]):
        return False
    if post.get("channel") == CHECKOUT:
        return (reader.get("checkout") or reader.get("cwd")) == post.get("checkout")
    return True


def history(
    base: Path, reader: dict[str, Any], channel: str, *, limit: int = 20, before: str | None = None
) -> dict[str, Any]:
    """Posts to a channel in the reader's project, newest first, a page at a time. Reading delivers nothing and
    wakes nobody; posts past mail retention are gone, which `oldestKept` makes visible."""
    check(channel, audience=True)
    posts = []
    for path in (base / "channels" / channel).glob("*.json"):
        item = mailbox.load(path)
        if visible(item, reader):
            posts.append(item)
    posts.sort(key=lambda item: (item.get("sentAt", ""), item.get("postId", "")), reverse=True)
    if before:
        posts = [item for item in posts if item.get("postId", "") < before]
    page = posts[: max(1, min(limit, 50))]
    keys = ("postId", "channel", "from", "text", "replyTo", "wake", "sentAt", "recipients")
    return {
        "channel": channel,
        "posts": [{key: item.get(key) for key in keys} for item in page],
        "more": len(posts) > len(page),
        "oldestKept": posts[-1]["sentAt"] if posts else None,
    }


def overview(base: Path, me: dict[str, Any]) -> list[dict[str, Any]]:
    """The named channels in this project: their live members, and whether this session is one of them."""
    members: dict[str, list[str]] = {}
    for peer in mailbox.scoped_peers(base, me):
        if mailbox.is_run_peer(peer):
            continue
        for channel in memberships(peer):
            members.setdefault(channel, []).append(peer["name"])
    mine = set(memberships(me))
    return [
        {"channel": channel, "members": sorted(names), "count": len(names), "joined": channel in mine}
        for channel, names in sorted(members.items())
    ]


def split(me: dict[str, Any], others: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Channel-mates (sharing a channel; or, for a channel-less session, the other channel-less ones), and how
    many sessions each other channel has ("" counts the channel-less ones). Only the mates matter for roster
    notices."""
    mine = set(memberships(me))
    mates, elsewhere = [], {}
    for peer in others:
        theirs = set(memberships(peer))
        if (mine and theirs & mine) or (not mine and not theirs):
            mates.append(peer)
            continue
        for channel in sorted(theirs) or [""]:
            elsewhere[channel] = elsewhere.get(channel, 0) + 1
    return mates, elsewhere


def roster_key(me: dict[str, Any], mates: list[dict[str, Any]]) -> list[str]:
    """What a roster notice is about: the mates and their channels, and this session's own channels. It equals
    the plain list of mate sessions while nobody uses channels, so upgrading notifies no one."""
    key = sorted(peer["session"] + (":" + ",".join(memberships(peer)) if memberships(peer) else "") for peer in mates)
    if memberships(me):
        key.append("#" + ",".join(memberships(me)))
    return key


def join_hint(me: dict[str, Any], others: list[dict[str, Any]]) -> str:
    """At session start, a channel-less agent learns the project's task channels, without being joined to any."""
    if memberships(me):
        return ""
    counts: dict[str, int] = {}
    for peer in others:
        for channel in memberships(peer):
            counts[channel] = counts.get(channel, 0) + 1
    if not counts:
        return ""
    listed = ", ".join(f"{channel} ({count})" for channel, count in sorted(counts.items()))
    return (
        f"Task channels in this project: {listed}. Posts to a channel reach only its members; join the one for "
        "your task with the peer mailbox `focus` tool (`channels=[...]`) or `mailbox join <channel>`, so the others "
        "stay quiet."
    )


def reads_at_stop(message: dict[str, Any]) -> bool:
    """Mail worth extending a turn for at Stop: a direct message, or a channel post that asked for a wake.
    Quiet channel posts wait for the next turn, so busy channels never keep each other going."""
    return not message.get("channel") or bool(message.get("wake"))
