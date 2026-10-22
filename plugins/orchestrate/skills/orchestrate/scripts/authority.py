#!/usr/bin/env python3
"""One accountable lead per team, a numbered authority generation, and verified handoffs.

The record names who may change a team's coordinator tasks. Every change is a compare-and-swap on the
generation, and coordinator mutations check the caller's generation under a shared lock while they record
their run, so a superseded lead's dispatch, review or decision is refused. A planned handover is two-phase:
the lead offers and stays in charge until the successor accepts. A takeover (the lead is presumed gone)
fences the old lead at once and leaves the new generation pending until the successor accepts. Accepting
binds to a digest of the computed handoff state the successor was shown.

These are guard rails against confused or superseded agents, not security: any local process can pass any
session ID. Only coordinator operations on tasks registered with the team are fenced.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = "orchestrate.authority.v1"
TEAM = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
# The coordinator host that a lead of each harness supervises from.
HARNESS_HOST = {"claude": "claude", "codex": "codex", "gemini": "agy", "agy": "agy", "opencode": "opencode"}
LIVE_RUN = {"dispatching", "reserved", "active", "uncertain"}
# Coordinator actions hold the team lock only briefly; an authority change waits at most this long.
LOCK_WAIT_SECONDS = 30.0
WRITING_KINDS = {"implementation", "correction", "verification"}


class AuthorityError(ValueError):
    pass


@functools.cache
def module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location("authority_" + name, Path(__file__).with_name(name + ".py"))
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def cache_dir() -> Path:
    home = os.environ.get("AGENT_EXECUTOR_HOME")
    if home:
        return Path(home).expanduser()
    root = os.environ.get("XDG_CACHE_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".cache") / "agent-executor"


def team_dir(team: str) -> Path:
    if not isinstance(team, str) or not re.fullmatch(TEAM, team):
        raise AuthorityError("team must be 1-64 letters, digits, underscores or hyphens")
    return cache_dir() / "authority-v1" / team


def member(session: str, harness: str) -> dict[str, str]:
    if not isinstance(session, str) or not session.strip():
        raise AuthorityError("a member needs its native session ID")
    if harness not in HARNESS_HOST:
        raise AuthorityError(f"unknown harness {harness!r}")
    return {"session": session.strip(), "harness": "gemini" if harness == "agy" else harness}


@contextlib.contextmanager
def locked(directory: Path, *, exclusive: bool, wait_seconds: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "authority.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        until = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise AuthorityError(
                        "a coordinator action holds the team lock; retry in a moment"
                        if exclusive
                        else "an authority change holds the team lock; retry in a moment"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def load(directory: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((directory / "authority.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise AuthorityError(f"unsupported authority record in {directory}")
    return record


def save(directory: Path, record: dict[str, Any]) -> None:
    record["updated_at"] = now()
    temporary = directory / f"authority.json.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, directory / "authority.json")


def require(directory: Path) -> dict[str, Any]:
    record = load(directory)
    if record is None:
        raise AuthorityError("no lead has been appointed for this team")
    return record


def expect(record: dict[str, Any], generation: int | None) -> None:
    if generation != record["generation"]:
        raise AuthorityError(
            f"authority generation is {record['generation']} (lead {record['lead']['session']}), not {generation}; "
            "read it with show and retry with the current generation"
        )


def host_of(record: dict[str, Any]) -> str:
    """The coordinator host of the current lead, which decides native versus executor transport."""
    return HARNESS_HOST[record["lead"]["harness"]]


def log(record: dict[str, Any], event: str, **detail: Any) -> None:
    record.setdefault("log", []).append({"at": now(), "event": event, "generation": record["generation"], **detail})


def git(repository: str, *args: str) -> str | None:
    completed = subprocess.run(["git", "-C", repository, *args], capture_output=True, text=True, check=False)
    return completed.stdout if completed.returncode == 0 else None


def heads(record: dict[str, Any]) -> dict[str, str]:
    """HEAD of every checkout the team's tasks work in."""
    found = {}
    for task in tasks(record):
        repository = task["repository"]
        if repository not in found and (head := git(repository, "rev-parse", "HEAD")):
            found[repository] = head.strip()
    return found


def tasks(record: dict[str, Any]) -> list[dict[str, Any]]:
    loaded = []
    for directory in record.get("tasks", []):
        with contextlib.suppress(OSError, ValueError):
            state = json.loads((Path(directory) / "task.json").read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("repository"), str):
                loaded.append({**state, "task_dir": directory})
    return loaded


def computed_state(record: dict[str, Any], successor_harness: str | None = None) -> dict[str, Any]:
    """What a successor must see before taking over, and the digest that accepting binds to.

    Only decision-relevant state is digested, so it does not move on its own: checkouts' HEAD and whether
    the handoff HEAD is still an ancestor, live runs by identity and status, and uncommitted paths inside
    the team's scope, minus the paths a live writing run owns (an inherited run may still be editing them).
    """
    native = module("native_bridge")
    successor_host = HARNESS_HOST.get(successor_harness or "")
    loaded = tasks(record)
    runs, owned, scope = [], {}, {}
    for task in loaded:
        spec = task.get("task_spec") or {}
        repository = task["repository"]
        scope.setdefault(repository, set()).update(spec.get("allow_paths", []) + spec.get("track_paths", []))
        for run in task.get("runs", []):
            if run.get("status") not in LIVE_RUN:
                continue
            if run.get("kind") in WRITING_KINDS:
                owned.setdefault(repository, set()).update(spec.get("allow_paths", []))
            entry = {
                "task_id": task.get("task_id"),
                "task_dir": task["task_dir"],
                "run_id": run["run_id"],
                "kind": run.get("kind"),
                "engine": run.get("engine"),
                "transport": run.get("transport"),
                "status": run.get("status"),
                # A native guard outlives its worker: its liveness says nothing about the worker's.
                "guard" if run.get("transport") == "native" else "process": native.process_state(run),
                "worker": "unknown" if run.get("transport") == "native" else native.process_state(run),
                "conductor_mailbox": (run.get("mailbox") or {}).get("conductor"),
            }
            if (
                run.get("transport") == "native"
                and successor_host
                and successor_host != run.get("host", task.get("host"))
            ):
                entry["correction"] = "unavailable: native to the predecessor's harness; accept, reject or take over"
            runs.append(entry)
    checkouts = {}
    for repository in sorted(scope):
        status = git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        head = git(repository, "rev-parse", "HEAD")
        if status is None or head is None:
            # Fail closed: an unreadable checkout is unknown, never clean.
            checkouts[repository] = {"head": None, "branch": None, "dirty_in_scope": None, "error": "git failed"}
            continue
        paths = porcelain_paths(status)
        inside = [
            path
            for path in paths
            if any(path == root or path.startswith(root.rstrip("/") + "/") for root in scope[repository])
            and not any(path == root or path.startswith(root.rstrip("/") + "/") for root in owned.get(repository, ()))
        ]
        checkouts[repository] = {
            "head": head.strip(),
            "branch": (git(repository, "branch", "--show-current") or "").strip(),
            "dirty_in_scope": inside,
        }
    handoff_heads = (record.get("offer") or {}).get("heads") or (record.get("handoff") or {}).get("heads") or {}
    ancestry = {
        repository: checkouts[repository]["head"] is not None
        and git(repository, "merge-base", "--is-ancestor", head, checkouts[repository]["head"]) is not None
        for repository, head in handoff_heads.items()
        if repository in checkouts
    }
    relevant = {
        "generation": record["generation"],
        "status": record["status"],
        "lead": record["lead"],
        "offer_to": (record.get("offer") or {}).get("to"),
        "checkouts": checkouts,
        "ancestry": ancestry,
        "runs": [{key: run[key] for key in ("run_id", "status", "kind", "transport")} for run in runs],
    }
    return {
        "digest": hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest(),
        "checkouts": checkouts,
        "ancestry": ancestry,
        "runs": runs,
        "recent_decisions": {task["task_dir"]: task.get("decisions", [])[-10:] for task in loaded},
        "handoff": record.get("handoff"),
        "offer": record.get("offer"),
    }


def porcelain_paths(status: str) -> list[str]:
    """Paths from `git status --porcelain=v1 -z`: a rename or copy entry is followed by its original path."""
    items, paths, index = status.split("\0"), set(), 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) < 4:
            continue
        paths.add(item[3:])
        if item[0] in "RC" or item[1] in "RC":
            if index < len(items) and items[index]:
                paths.add(items[index])
            index += 1
    return sorted(paths)


def show(team: str, successor_harness: str | None = None) -> dict[str, Any]:
    directory = team_dir(team)
    with locked(directory, exclusive=False):
        record = require(directory)
        return {"authority": record, "state": computed_state(record, successor_harness)}


def pool_facts(lead_pool: str | None, deputy_pool: str | None) -> dict[str, Any]:
    """Each named pool's account and context from monitor; refused when either is unknown or they differ."""
    if not lead_pool and not deputy_pool:
        return {}
    succession = module("succession")
    config = succession.monitor_config()
    by_key = {pool["pool"]: pool for pool in succession.pools(succession.fresh_view(), config)}
    contexts = {}
    for name, key in (("lead", lead_pool), ("deputy", deputy_pool)):
        if not key:
            continue
        pool = by_key.get(key)
        if pool is None:
            raise AuthorityError(f"monitor reports no pool {key} for the {name}")
        if pool["billing"] is None:
            raise AuthorityError(
                f"set billing (work or personal) on monitor account '{pool['accountId']}' before appointing"
            )
        contexts[pool["accountId"]] = pool["billing"]
    if lead_pool and deputy_pool:
        lead, deputy = by_key[lead_pool], by_key[deputy_pool]
        if lead["accountId"] == deputy["accountId"]:
            raise AuthorityError("the deputy must draw on another account: one login's pools fail together")
        if lead["billing"] != deputy["billing"]:
            raise AuthorityError("the deputy's account is in another context (work versus personal)")
    return {"contexts": contexts, "pools": by_key}


def parse_standing(value: str | None) -> dict[str, Any] | None:
    """`blocked,credits:900`: the lead pool statuses and the inactivity that allow a takeover without the user."""
    if not value:
        return None
    statuses, _, seconds = value.partition(":")
    names = [name.strip() for name in statuses.split(",") if name.strip()]
    if not names or any(name not in ("blocked", "credits") for name in names) or not seconds.isdigit():
        raise AuthorityError("standing takeover reads STATUSES:SECONDS, e.g. blocked,credits:900")
    return {"statuses": names, "seconds": int(seconds)}


def appoint(
    team: str,
    lead: dict[str, str],
    *,
    deputy: dict[str, Any] | None = None,
    allow_credits: bool = False,
    expect_generation: int | None = None,
    standing: dict[str, Any] | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    """Appoint the lead (the user's act). Re-appointing needs the current generation and fences the old lead.

    A lead and deputy pool (`accountId:poolId`, see `propose`) record each account's context, so a later
    takeover refuses when it changed. A standing rule lets the deputy take over without the user only when
    the lead's pool is exhausted and the lead is gone (succession.standing_evidence)."""
    facts = pool_facts(lead.get("pool"), (deputy or {}).get("pool"))
    if standing and not (lead.get("pool") and (deputy or {}).get("pool")):
        raise AuthorityError("a standing takeover rule needs both the lead's and the deputy's pool")
    if standing and not repository:
        raise AuthorityError("a standing takeover rule needs the team's repository (run appoint inside it)")
    directory = team_dir(team)
    with locked(directory, exclusive=True):
        record = load(directory)
        if record is None:
            if expect_generation not in (None, 0):
                raise AuthorityError("this team has no lead yet; appoint without --expect-generation")
            record = {"schema": SCHEMA, "team": team, "generation": 0, "tasks": [], "log": []}
        else:
            expect(record, expect_generation)
        previous = record.get("lead")
        record.update(
            generation=record["generation"] + 1,
            status="active",
            lead=lead,
            deputy=deputy,
            allow_credits=bool(allow_credits),
            standing=standing,
            repository=repository,
            contexts=facts.get("contexts", {}),
            offer=None,
            handoff={"reason": "appoint", "from": previous, "heads": heads(record), "at": now()},
        )
        log(record, "appoint", lead=lead, previous=previous)
        save(directory, record)
        return record


def offer(team: str, caller: str, generation: int, to: dict[str, str], note: str = "") -> dict[str, Any]:
    """A planned handover: the lead stays in charge until the successor accepts."""
    directory = team_dir(team)
    with locked(directory, exclusive=True):
        record = require(directory)
        expect(record, generation)
        if record["status"] != "active" or record["lead"]["session"] != caller:
            raise AuthorityError("only the active lead can offer a handover")
        record["offer"] = {"to": to, "note": note.strip(), "heads": heads(record), "offered_at": now()}
        log(record, "offer", to=to)
        save(directory, record)
        return record


def takeover(
    team: str,
    successor: dict[str, str],
    expect_generation: int,
    *,
    user_approved: bool,
    standing: bool = False,
    successor_account: str | None = None,
    successor_repository: str | None = None,
) -> dict[str, Any]:
    """The lead is presumed gone: fence it now; the successor's generation stays pending until it accepts.

    It needs the user's approval, or `standing` evidence under the appointment's rule. The evidence is read
    before the lock (monitor may take a while) and holds only for the generation it was read at."""
    if not (user_approved or standing):
        raise AuthorityError("a takeover needs the user's approval (--user-approved) or --standing evidence")
    directory = team_dir(team)
    by_standing = standing and not user_approved
    lead_status = None
    if by_standing:
        succession = module("succession")
        with locked(directory, exclusive=False):
            snapshot = require(directory)
        expect(snapshot, expect_generation)
        named = (snapshot.get("deputy") or {}).get("session")
        if named and named != successor["session"]:
            raise AuthorityError("only the named deputy session can take over")
        if successor_account != (snapshot.get("deputy") or {}).get("account"):
            raise AuthorityError("a standing takeover is only for a session on the named deputy's account")
        if not succession.same_repository(successor_repository, snapshot.get("repository")):
            # One Codex or Antigravity login serves every repository: only the team's own may succeed it.
            raise AuthorityError("a standing takeover is only for a session in the team's repository")
        # Only the monitor read happens outside the lock; the rest is re-read under it below.
        reasons, lead_status = succession.monitor_evidence(
            snapshot, view=succession.fresh_view(), config=succession.monitor_config(), moment=succession.now()
        )
        if reasons:
            raise AuthorityError("no standing takeover: " + "; ".join(reasons))
    with locked(directory, exclusive=True):
        record = require(directory)
        expect(record, expect_generation)
        if by_standing:
            reasons = module("succession").local_evidence(
                record, lead_status=lead_status, moment=module("succession").now()
            )
            if reasons:
                raise AuthorityError("no standing takeover: " + "; ".join(reasons))
        deputy = record.get("deputy") or {}
        if deputy.get("harness") and deputy["harness"] != successor["harness"]:
            raise AuthorityError(f"the named deputy runs on {deputy['harness']}, not {successor['harness']}")
        if deputy.get("session") and deputy["session"] != successor["session"]:
            raise AuthorityError("only the named deputy can take over")
        previous = record["lead"]
        record.update(
            generation=record["generation"] + 1,
            status="pending",
            # The deputy is now the lead: a new deputy (and standing rule) is the user's next appointment.
            lead={**successor, **({"pool": deputy["pool"]} if deputy.get("pool") else {})},
            deputy=None,
            standing=None,
            offer=None,
            handoff={"reason": "takeover", "from": previous, "heads": heads(record), "at": now()},
        )
        log(record, "takeover", lead=successor, previous=previous, standing=standing and not user_approved)
        save(directory, record)
        return record


def accept(team: str, caller: dict[str, str], expect_generation: int, digest: str) -> dict[str, Any]:
    """The successor accepts the handoff it was shown; refused when that state changed or history was rewritten."""
    directory = team_dir(team)
    with locked(directory, exclusive=True):
        record = require(directory)
        expect(record, expect_generation)
        planned = (
            record["status"] == "active"
            and (record.get("offer") or {}).get("to", {}).get("session") == caller["session"]
        )
        pending = record["status"] == "pending" and record["lead"]["session"] == caller["session"]
        if not (planned or pending):
            raise AuthorityError("no handover is offered to this session and no takeover by it is pending")
        state = computed_state(record, caller["harness"])
        if digest != state["digest"]:
            raise AuthorityError("the handoff state changed since it was shown; run show again and review it")
        unknown = sorted(repository for repository, checkout in state["checkouts"].items() if checkout.get("error"))
        if unknown:
            raise AuthorityError("cannot read the checkout state of: " + ", ".join(unknown))
        rewritten = sorted(repository for repository, ancestor in state["ancestry"].items() if not ancestor)
        if rewritten:
            raise AuthorityError(
                "history was rewritten since the handoff (its HEAD is no longer an ancestor) in: "
                + ", ".join(rewritten)
            )
        if planned:
            previous = record["lead"]
            record.update(
                generation=record["generation"] + 1,
                lead=caller,
                handoff={
                    "reason": "handover",
                    "from": previous,
                    "heads": record["offer"]["heads"],
                    "note": record["offer"]["note"],
                    "at": now(),
                },
                offer=None,
            )
        record["status"] = "active"
        log(record, "accept", lead=caller, digest=digest)
        save(directory, record)
        return record


def register_task(team: str, task_dir: Path, session: str | None, generation: int | None) -> dict[str, Any]:
    """Put a task under the team before the task exists, so the fence and every handoff see the same tasks."""
    directory = team_dir(team)
    with locked(directory, exclusive=True):
        record = require(directory)
        check(record, session, generation)
        path = str(task_dir.resolve())
        if path not in record["tasks"]:
            record["tasks"].append(path)
            log(record, "task", task_dir=path)
            save(directory, record)
        return record


def check(record: dict[str, Any], session: str | None, generation: int | None, *, allow_pending: bool = False) -> None:
    if session is None or generation is None:
        raise AuthorityError(
            "this task belongs to a team: pass --lead-session and --lead-generation (read them with authority show)"
        )
    if record["generation"] != generation or record["lead"]["session"] != session:
        raise AuthorityError(
            f"authority generation {record['generation']} belongs to {record['lead']['session']}; "
            f"{session} at generation {generation} was superseded: stop, and read the handoff with authority show"
        )
    if record["status"] != "active" and not allow_pending:
        raise AuthorityError("this generation is pending: accept the handoff first (only show and abandon run before)")


class Fence:
    """The caller's authority over one team task, checked under brief shared holds of the team lock.

    It checks when the action starts and again around the action's first durable write (its commit point),
    so a long action (verification, a capture, a guard's audit) never blocks a handover, and an action
    whose lead was superseded meanwhile cannot commit. Authority changes never take a task lease, so a
    hold inside the task lease cannot deadlock.
    """

    def __init__(
        self,
        team: str,
        session: str | None,
        generation: int | None,
        *,
        allow_pending: bool = False,
        task_dir: Path | None = None,
        store: str | None = None,
    ):
        self.directory = team_dir(team)
        if store is not None and Path(store).resolve() != self.directory.resolve():
            raise AuthorityError(
                f"this task's team record lives in {store}, but this process resolves {self.directory}: "
                "run with the same AGENT_EXECUTOR_HOME / XDG_CACHE_HOME as the team"
            )
        self.session, self.generation, self.allow_pending = session, generation, allow_pending
        self.task_dir = str(task_dir.resolve()) if task_dir is not None else None
        self.committed = False
        with self.held():
            pass

    @contextlib.contextmanager
    def held(self) -> Iterator[dict[str, Any]]:
        with locked(self.directory, exclusive=False):
            self.record = require(self.directory)
            check(self.record, self.session, self.generation, allow_pending=self.allow_pending)
            if self.task_dir is not None and self.task_dir not in self.record.get("tasks", []):
                raise AuthorityError("this task is not registered with its team; it cannot be changed under it")
            yield self.record

    def release(self) -> None:
        """Kept for callers that release before a long wait; holds are already brief."""


def session_facts(session: str, harness: str, model: str | None, account_home: str | None) -> dict[str, Any]:
    """A session's harness, model and account home: from its mailbox record unless given explicitly."""
    mailbox = module("peer_mailbox")
    peer = mailbox.load(mailbox.peer_path(mailbox.root(), session)) or {}
    return {
        "session": session,
        "harness": "gemini" if harness == "agy" else harness,
        "model": model or peer.get("model"),
        "accountHome": account_home or peer.get("accountHome"),
        "repo": peer.get("repo") or repository_here(),
    }


def propose_for(lead: dict[str, Any]) -> dict[str, Any]:
    succession, mailbox = module("succession"), module("peer_mailbox")
    proposal = succession.propose(
        lead,
        view=succession.fresh_view(),
        config=succession.monitor_config(),
        routes=module("routes").load_routes(),
        peers=mailbox.peers(mailbox.root()),
    )
    deputy = proposal.get("deputy")
    if not proposal["refusals"] and deputy:
        proposal["appoint"] = (
            f"authority.py appoint --team TEAM --lead-session {lead['session']} --lead-harness {lead['harness']} "
            f"--lead-pool {proposal['lead']['pool']} --deputy-harness {deputy['harness']} "
            f"--deputy-pool {deputy['pool']} [--deputy-session SESSION] [--standing-takeover blocked:900]"
        )
    return proposal


def repository_here() -> str | None:
    """The shared Git directory of the current checkout, which every worktree of the repository has in common."""
    common = git(os.getcwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(common.strip()).resolve()) if common else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("appoint", "offer", "takeover", "accept", "show", "propose", "set-billing"):
        sub = commands.add_parser(name)
        sub.add_argument("--team", required=name not in {"propose", "set-billing"})
        if name in {"appoint", "offer", "takeover", "accept"}:
            sub.add_argument("--expect-generation", type=int)
        if name == "appoint":
            sub.add_argument("--lead-session", required=True)
            sub.add_argument("--lead-harness", choices=sorted(HARNESS_HOST), required=True)
            sub.add_argument("--lead-pool", help="accountId:poolId the lead pays from (see propose)")
            sub.add_argument("--deputy-harness", choices=sorted(HARNESS_HOST))
            sub.add_argument("--deputy-session")
            sub.add_argument("--deputy-pool", help="accountId:poolId on another account in the same context")
            sub.add_argument(
                "--standing-takeover", help="STATUSES:SECONDS, e.g. blocked:900; only on the user's say-so"
            )
            sub.add_argument("--allow-credits", action="store_true", help="only when the user allows spending credits")
            sub.add_argument("--repository", help="the team's Git directory (default: the current checkout's)")
        elif name == "offer":
            sub.add_argument("--session", required=True, help="the lead's own session")
            sub.add_argument("--to-session", required=True)
            sub.add_argument("--to-harness", choices=sorted(HARNESS_HOST), required=True)
            sub.add_argument("--note", default="")
        elif name in {"takeover", "accept", "show", "propose"}:
            sub.add_argument("--session", required=name != "show", help="the successor's (or proposed lead's) session")
            sub.add_argument("--harness", choices=sorted(HARNESS_HOST), required=name != "show")
            if name == "takeover":
                sub.add_argument("--user-approved", action="store_true")
                sub.add_argument("--standing", action="store_true", help="under the appointment's standing rule")
            if name == "accept":
                sub.add_argument("--digest", required=True, help="the digest show printed for the state you reviewed")
            if name == "propose":
                sub.add_argument("--model", help="the lead's model, when its mailbox record lacks it")
                sub.add_argument("--account-home", help="the lead's config home, when its mailbox record lacks it")
        elif name == "set-billing":
            sub.add_argument("--account", required=True, help="monitor account id")
            sub.add_argument("--billing", choices=("work", "personal"), required=True, help="as the user answered")
    args = parser.parse_args(argv)
    try:
        if args.action == "appoint":
            deputy = None
            if args.deputy_harness:
                deputy = {
                    "harness": "gemini" if args.deputy_harness == "agy" else args.deputy_harness,
                    "session": args.deputy_session,
                    "pool": args.deputy_pool,
                    "account": args.deputy_pool.split(":", 1)[0] if args.deputy_pool else None,
                }
            lead = member(args.lead_session, args.lead_harness)
            if args.lead_pool:
                lead["pool"] = args.lead_pool
            output = appoint(
                args.team,
                lead,
                deputy=deputy,
                allow_credits=args.allow_credits,
                expect_generation=args.expect_generation,
                standing=parse_standing(args.standing_takeover),
                repository=str(Path(args.repository).resolve()) if args.repository else repository_here(),
            )
        elif args.action == "offer":
            output = offer(
                args.team, args.session, args.expect_generation, member(args.to_session, args.to_harness), args.note
            )
        elif args.action == "takeover":
            facts = session_facts(args.session, args.harness, None, None)
            account = module("succession").account_of(
                facts["harness"], facts["accountHome"], module("succession").monitor_config()
            )[0]
            output = takeover(
                args.team,
                member(args.session, args.harness),
                args.expect_generation,
                user_approved=args.user_approved,
                standing=args.standing,
                successor_account=account,
                successor_repository=facts["repo"],
            )
        elif args.action == "accept":
            output = accept(args.team, member(args.session, args.harness), args.expect_generation, args.digest)
            if not output.get("deputy"):
                # Accepting leaves the team without a deputy until the user names one: propose it now.
                output = {**output, "next": "no deputy: ask the user to appoint one from this proposal"}
                with contextlib.suppress(AuthorityError, OSError, ValueError):
                    output["proposal"] = propose_for(session_facts(args.session, args.harness, None, None))
        elif args.action == "propose":
            output = propose_for(session_facts(args.session, args.harness, args.model, args.account_home))
        elif args.action == "set-billing":
            module("succession").set_billing(args.account, args.billing)
            output = {"account": args.account, "billing": args.billing}
        else:
            output = show(args.team, args.harness)
        print(json.dumps(output, indent=2, ensure_ascii=True))
        return 0
    except (AuthorityError, OSError, ValueError) as error:
        print(f"authority: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
