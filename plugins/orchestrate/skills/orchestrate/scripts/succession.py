"""Who should succeed a team's lead, decided from facts only and never guessed.

The facts are monitor's quota pools with their account context (`billing`: work or personal), the route
table, and the live mailbox sessions. A pool is `accountId:poolId`. A deputy must draw on another account
(two pools of one login fail together) in the same context, with room now; worker pools stay off the lead's
and deputy's pools whenever another eligible pool exists, so the successor is not drained before it is
needed. Anything unknown refuses with a reason that says how to fix it.

The proposal is advice: appointing, offering and taking over stay with authority.py and the user.
"""

from __future__ import annotations

import datetime as dt
import functools
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_MAX_AGE_SECONDS = 180
# A lead's or deputy's turn hook looks at quota at most this often.
HOOK_CHECK_SECONDS = 300
HARNESS_ENGINE = {"claude": "claude", "codex": "codex", "gemini": "agy"}
HARNESS_PROVIDER = {"claude": "claude", "codex": "codex", "gemini": "antigravity"}
USABLE = ("available", "low")
EXHAUSTED = ("blocked", "credits")


@functools.cache
def module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location("succession_" + name, Path(__file__).with_name(name + ".py"))
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def monitor_config_path() -> Path:
    return Path(os.environ.get("MONITOR_CONFIG") or Path.home() / ".config" / "monitor" / "config.json").expanduser()


def monitor_config() -> dict[str, Any]:
    try:
        config = json.loads(monitor_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def max_age_seconds(config: dict[str, Any]) -> int:
    value = config.get("maxAgeSeconds")
    return value if isinstance(value, int) and value > 0 else DEFAULT_MAX_AGE_SECONDS


def fresh_view() -> dict[str, Any] | None:
    """monitor's view, refreshed by monitor itself when older than its own maxAgeSeconds."""
    return module("quota").snapshot()


def cached_view() -> dict[str, Any] | None:
    """monitor's last view without any network call, for turn hooks; None when nothing is cached yet."""
    cache = Path(os.environ.get("MONITOR_CACHE_DIR") or Path.home() / ".cache" / "monitor").expanduser()
    command = module("quota").monitor_command()
    if not command or not (cache / "snapshot.json").is_file():
        return None  # with no cache, --max-age would still fetch: a hook never waits on the network
    try:
        completed = subprocess.run(
            [command, "json", "--max-age", "1000000000"],
            capture_output=True,
            text=True,
            timeout=3,  # well inside the turn hook's own time limit
            env={**os.environ, "NO_COLOR": "1"},
            check=False,
        )
        view = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return view if isinstance(view, dict) and str(view.get("schema", "")).startswith("monitor.snapshot.") else None


def pools(view: dict[str, Any] | None, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every pool monitor reports, with its account's provider and context and an assessed status."""
    if not view:
        return []
    quota = module("quota")
    configured = {account.get("id"): account for account in config.get("accounts", []) if isinstance(account, dict)}
    accounts = {account["id"]: account for account in view.get("accounts", []) if isinstance(account, dict)}
    found = []
    for lane in view.get("lanes", []):
        account_id = lane.get("accountId")
        if lane.get("status") == "error" or account_id not in accounts:
            continue
        billing = accounts[account_id].get("billing") or (configured.get(account_id) or {}).get("billing")
        found.append(
            {
                "pool": f"{account_id}:{lane.get('poolId')}",
                "accountId": account_id,
                "poolId": lane.get("poolId"),
                "provider": accounts[account_id].get("provider"),
                "billing": billing if billing in ("work", "personal") else None,
                "status": quota.assess(lane),
                "availableNowPct": lane.get("availableNowPct"),
                "surplusPts": lane.get("surplusPts"),
                "subPoolOf": lane.get("subPoolOf"),
                "blockedUntil": lane.get("blockedUntil"),
                "stale": bool(lane.get("stale")),
                "label": lane.get("label"),
            }
        )
    return found


def account_of(harness: str, account_home: str | None, config: dict[str, Any]) -> tuple[str | None, str]:
    """The monitor account a session of this harness draws on, or None and why."""
    provider = HARNESS_PROVIDER.get(harness)
    if provider is None:
        return None, f"{harness} sessions have no monitored subscription pool"
    accounts = [a for a in config.get("accounts", []) if isinstance(a, dict) and a.get("provider") == provider]
    if provider != "claude":
        if len(accounts) == 1:
            return accounts[0]["id"], ""
        return None, f"monitor has {len(accounts)} {provider} accounts; cannot tell which one this session uses"
    if not account_home:
        return None, "this Claude session's config home (account) is unknown"
    home = Path(account_home).expanduser().resolve()
    matches = [
        account["id"]
        for account in accounts
        if Path(account.get("configDir") or "~/.claude").expanduser().resolve() == home
    ]
    if len(matches) == 1:
        return matches[0], ""
    return None, f"{len(matches)} monitor Claude accounts use {home}; set configDir on exactly one"


def pool_for(harness: str, model: str | None, account_id: str, view: dict[str, Any], config: dict[str, Any]) -> dict:
    """The pool a session pays from: the route's lane on its own account."""
    engine = HARNESS_ENGINE[harness]
    lanes = module("quota").pool_lanes(engine, model or engine, view)
    lane = next((lane for lane in lanes if lane.get("accountId") == account_id), None)
    if lane is None:
        return {}
    key = f"{account_id}:{lane.get('poolId')}"
    return next((pool for pool in pools(view, config) if pool["pool"] == key), {})


def rank(pool: dict[str, Any]) -> tuple:
    return (pool["status"] == "low", -(pool.get("surplusPts") or 0), -(pool.get("availableNowPct") or 0))


def sessions_on(
    account_id: str, provider: str | None, peers: list[dict[str, Any]], config: dict[str, Any], repository: str | None
) -> list:
    """Live interactive sessions in the team's repository that draw on this account."""
    found = []
    mailbox = module("peer_mailbox")
    for peer in peers:
        if (
            HARNESS_PROVIDER.get(peer.get("harness")) != provider
            or peer.get("delegatedRun")
            or mailbox.is_run_peer(peer)
        ):
            continue
        if not same_repository(peer.get("repo"), repository):
            continue
        if account_of(peer["harness"], peer.get("accountHome"), config)[0] == account_id:
            found.append({"session": peer["session"], "name": peer.get("name"), "harness": peer["harness"]})
    return found


def propose(
    lead: dict[str, Any],
    *,
    view: dict[str, Any] | None,
    config: dict[str, Any],
    routes: dict[str, Any],
    peers: list[dict[str, Any]],
) -> dict[str, Any]:
    """A lead's pool, a deputy on an independent pool in the same context, and worker pools that spare both."""
    refusals: list[str] = []
    result: dict[str, Any] = {"refusals": refusals, "lead": dict(lead), "deputy": None, "workers": {}}
    if view is None:
        refusals.append("monitor is not installed or did not answer: quota is unknown")
        return result
    available = pools(view, config)
    account_id, why = account_of(lead["harness"], lead.get("accountHome"), config)
    if account_id is None:
        refusals.append(why)
        return result
    lead_pool = pool_for(lead["harness"], lead.get("model"), account_id, view, config)
    if not lead_pool:
        refusals.append(f"monitor reports no pool for {lead['harness']} on account '{account_id}'")
        return result
    result["lead"].update(account=account_id, pool=lead_pool["pool"], status=lead_pool["status"])
    context = lead_pool["billing"]
    result["context"] = context
    if context is None:
        refusals.append(f"set billing (work or personal) on monitor account '{account_id}' in {monitor_config_path()}")
        return result
    untagged = sorted({pool["accountId"] for pool in available if pool["billing"] is None})
    if untagged:
        result["untagged_accounts"] = untagged  # never candidates until the user tags them
    candidates = sorted(
        (
            pool
            for pool in available
            if pool["accountId"] != account_id
            and pool["billing"] == context
            and pool["status"] in USABLE
            and not pool["subPoolOf"]
        ),
        key=rank,
    )
    if not candidates:
        refusals.append(
            f"no pool on another {context} account has room now"
            + (f"; untagged accounts that might qualify: {', '.join(untagged)}" if untagged else "")
        )
        return result
    deputy = candidates[0]
    result["deputy"] = {
        **deputy,
        "harness": next(harness for harness, provider in HARNESS_PROVIDER.items() if provider == deputy["provider"]),
        "sessions": sessions_on(deputy["accountId"], deputy["provider"], peers, config, lead.get("repo")),
    }
    if not result["deputy"]["sessions"]:
        result["deputy"]["note"] = "no live session draws on this pool: start one before you need it"
    result["alternatives"] = [pool["pool"] for pool in candidates[1:4]]
    reserved = {lead_pool["pool"], deputy["pool"]}
    quota = module("quota")
    for role in sorted(routes):
        eligible, shared = [], []
        for candidate in module("routes").route_candidates(role, routes):
            for lane in quota.pool_lanes(candidate["engine"], candidate["model"], view):
                key = f"{lane['accountId']}:{lane.get('poolId')}"
                pool = next((p for p in available if p["pool"] == key), None)
                if pool is None or pool["billing"] != context or pool["status"] not in USABLE:
                    continue
                entry = {"engine": candidate["engine"], "model": candidate["model"], "pool": key}
                (shared if key in reserved else eligible).append(entry)
        if eligible:
            result["workers"][role] = {"pools": eligible}
        elif shared:
            # Better a shared pool than none, said plainly: it drains the lead or the successor.
            result["workers"][role] = {"pools": shared, "note": "only the lead's or deputy's pool fits this role"}
    return result


def pid_replaced(peer: dict[str, Any] | None) -> bool:
    """Whether a session's recorded process is verifiably gone, or another process now has its pid.
    A process that cannot be checked counts as still running: unknown never takes authority away."""
    if not peer or not peer.get("pid"):
        return True
    mailbox = module("peer_mailbox")
    alive = mailbox.pid_alive(peer["pid"])
    if alive is False:
        return True
    if alive is None or not peer.get("pidBirth"):
        return False
    started = mailbox.process_started(peer["pid"])
    return started is not None and started != peer["pidBirth"]


def monitor_evidence(
    record: dict[str, Any],
    *,
    view: dict[str, Any] | None,
    config: dict[str, Any],
    moment: dt.datetime,
    fresh_required: bool = True,
) -> tuple[list[str], str | None]:
    """The pool half of a standing takeover's evidence (slow: it needs monitor), and the lead pool's status.
    A hook reading monitor's cache passes fresh_required=False: the takeover itself re-reads fresh data."""
    rule = record.get("standing")
    if not rule:
        return ["the appointment records no standing takeover rule; the user must approve a takeover"], None
    if view is None:
        return ["monitor did not answer: the lead's pool is unknown"], None
    by_key = {pool["pool"]: pool for pool in pools(view, config)}
    lead_pool = by_key.get(record["lead"].get("pool") or "")
    if lead_pool is None:
        return [f"monitor reports no pool {record['lead'].get('pool')} for the lead"], None
    if lead_pool["stale"] and fresh_required:
        # Exhaustion may have passed since: only fresh data takes authority from a lead.
        return ["the lead's pool is unknown: monitor's data for it is stale"], None
    reasons = []
    if lead_pool["status"] not in rule["statuses"]:
        reasons.append(f"the lead's pool is {lead_pool['status']}, not {' or '.join(rule['statuses'])}")
    until = parse_time(lead_pool.get("blockedUntil"))
    if lead_pool["status"] == "blocked" and until is not None and until <= moment:
        reasons.append("the lead's pool has already reset")
    deputy_pool = by_key.get((record.get("deputy") or {}).get("pool") or "")
    if deputy_pool is None or (deputy_pool["stale"] and fresh_required):
        reasons.append("the deputy's own pool is unknown")
    elif deputy_pool["status"] not in USABLE:
        reasons.append("the deputy's own pool has no room now")
    reasons += context_changes(record, by_key)
    return reasons, lead_pool["status"]


def local_evidence(record: dict[str, Any], *, lead_status: str | None, moment: dt.datetime) -> list[str]:
    """The fast half, re-read under the team lock: these change without a new generation."""
    rule = record.get("standing") or {"seconds": 0}
    reasons = []
    mailbox = module("peer_mailbox")
    lead_peer = mailbox.load(mailbox.peer_path(mailbox.root(), record["lead"]["session"]))
    seen = parse_time((lead_peer or {}).get("lastSeenAt"))
    if seen is not None and (moment - seen).total_seconds() < rule["seconds"]:
        reasons.append(f"the lead was active {int((moment - seen).total_seconds())} s ago")
    if lead_status == "credits" and not pid_replaced(lead_peer):
        # A lead on credits can still act, and a waiting lead looks idle: only a gone process counts.
        reasons.append("the lead's process is still running; on credits only the user can approve a takeover")
    for task in module("authority").tasks(record):
        updated = parse_time(task.get("updated_at"))
        live = any(run.get("status") in module("authority").LIVE_RUN for run in task.get("runs", []))
        if live and updated is not None and (moment - updated).total_seconds() < rule["seconds"]:
            reasons.append(f"task {task.get('task_id')} has a live run updated recently")
    offer = record.get("offer") or {}
    if offer.get("to"):
        reasons.append(f"a handover is already offered to {offer['to']['session']}")
    return reasons


def standing_evidence(
    record: dict[str, Any], *, view: dict[str, Any] | None, config: dict[str, Any], moment: dt.datetime | None = None
) -> list[str]:
    """Why a standing takeover is not justified now; an empty list means it is. Unknown never qualifies."""
    moment = moment or now()
    reasons, lead_status = monitor_evidence(record, view=view, config=config, moment=moment)
    if lead_status is None:
        return reasons
    return reasons + local_evidence(record, lead_status=lead_status, moment=moment)


def same_repository(left: str | None, right: str | None) -> bool:
    return bool(left and right) and Path(left).resolve() == Path(right).resolve()


def context_changes(record: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> list[str]:
    """Accounts whose work/personal context differs from what the appointment recorded."""
    changed = []
    for account_id, billing in (record.get("contexts") or {}).items():
        now_billing = next((pool["billing"] for pool in by_key.values() if pool["accountId"] == account_id), None)
        if now_billing != billing:
            changed.append(f"account '{account_id}' context changed since appointment ({billing} -> {now_billing})")
    return changed


def set_billing(account_id: str, billing: str) -> None:
    """Record an account's context in monitor's config, as the user answered; the only field touched."""
    if billing not in ("work", "personal"):
        raise ValueError("billing must be work or personal")
    path = monitor_config_path()
    config = json.loads(path.read_text(encoding="utf-8"))
    account = next((a for a in config.get("accounts", []) if a.get("id") == account_id), None)
    if account is None:
        raise ValueError(f"monitor has no account '{account_id}'")
    if account.get("billing") == billing:
        return
    if account.get("billing"):
        # Moving an account between work and personal is the user's own edit, never an agent's call.
        raise ValueError(f"account '{account_id}' is already {account['billing']}; change it by hand if it is wrong")
    account["billing"] = billing
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
