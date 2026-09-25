"""Live subscription quota per executor route, read from ai-usage at decision time. Nothing is stored.

ai-usage (claude-whisperer/plugins/ai-usage) reports each account's rate-limit windows and ranks its
pools. This module maps an engine and model onto the pool that pays for it, so the conductor can see
whether a route can take a run now and the runner can refuse to launch into an exhausted one.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

SCHEMA_PREFIX = "ai-usage.snapshot."
LOW_USABLE_PCT = 10
EXIT_QUOTA_EXHAUSTED = 15


def ai_usage_command() -> str | None:
    override = os.environ.get("AI_USAGE_BIN")
    if override:
        return override
    found = shutil.which("ai-usage")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "ai-usage"
    return str(fallback) if os.access(fallback, os.X_OK) else None


def snapshot(timeout: float = 60) -> dict[str, Any] | None:
    """Current ai-usage view, or None when ai-usage is missing or fails. Unknown quota is never zero."""
    command = ai_usage_command()
    if not command:
        return None
    try:
        completed = subprocess.run(
            [command, "json"], capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"}, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        view = json.loads(completed.stdout)
    except ValueError:
        return None
    return view if str(view.get("schema", "")).startswith(SCHEMA_PREFIX) else None


def route_for(engine: str, model: str) -> tuple[str, str | None] | None:
    """(ai-usage provider, pool id) that pays for a run, or None for metered/unmonitored engines."""
    lowered = model.lower()
    if engine == "codex":
        return "codex", None
    if engine == "agy":
        if lowered.startswith("gemini"):
            return "antigravity", "gemini"
        if lowered.startswith(("claude", "gpt")):
            return "antigravity", "claude-gpt"
        return "antigravity", None
    if engine == "claude":
        return "claude", None
    return None


def pool_lanes(engine: str, model: str, view: dict[str, Any]) -> list[dict[str, Any]]:
    """Every account's lane for this route, best first (ai-usage ranks lanes)."""
    route = route_for(engine, model)
    if route is None:
        return []
    provider, pool = route
    accounts = {account["id"]: account for account in view.get("accounts", [])}
    lanes = [lane for lane in view.get("lanes", []) if accounts.get(lane.get("accountId"), {}).get("provider") == provider]
    chosen: list[dict[str, Any]] = []
    for account_id in dict.fromkeys(lane["accountId"] for lane in lanes):
        own = [lane for lane in lanes if lane["accountId"] == account_id]
        if pool:
            match = [lane for lane in own if lane.get("poolId") == pool]
        else:
            # A model-specific limit (Codex per-model ids, Claude's per-model weekly cap) binds before the
            # account-wide one, so prefer a pool named after the model.
            match = [lane for lane in own if lane.get("poolId") and lane["poolId"] in model.lower()]
            match = match or [lane for lane in own if lane.get("poolId") in ("codex", "all")] or own
        if match:
            chosen.append({**match[0], "billing": accounts[account_id].get("billing")})
    return chosen


def assess(lane: dict[str, Any]) -> str:
    if lane.get("status") == "blocked":
        return "blocked"
    if (lane.get("availableNowPct") or 0) < LOW_USABLE_PCT:
        return "low"
    return "available"


def check(engine: str, model: str, view: dict[str, Any] | None) -> dict[str, Any]:
    """Whether a run on engine/model can start now: available, low, blocked, unknown or not_applicable."""
    if route_for(engine, model) is None:
        return {"status": "not_applicable", "detail": f"{engine} bills per request; no subscription window to check"}
    if view is None:
        return {"status": "unknown", "detail": "ai-usage is not installed or did not answer"}
    lanes = pool_lanes(engine, model, view)
    if not lanes:
        return {"status": "unknown", "detail": f"ai-usage has no pool for {engine} {model}"}
    lane = lanes[0]
    return {"status": assess(lane), **summary(lane)}


def summary(lane: dict[str, Any]) -> dict[str, Any]:
    keys = ("label", "accountId", "poolId", "billing", "availableNowPct", "weeklyRemainingPct", "surplusPts", "blockedUntil", "advice", "stale")
    return {key: lane.get(key) for key in keys}


ROUTES = (
    ("codex", "gpt-*", "codex"),
    ("agy", "gemini-*", "gemini"),
    ("agy", "claude-* / gpt-oss-*", "claude-gpt"),
)


def route_table(view: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Quota for every route agent-executor can dispatch to, plus the native Claude accounts for reference."""
    rows = []
    for engine, models, sample in ROUTES:
        probe = {"codex": "gpt", "gemini": "gemini", "claude-gpt": "claude"}[sample]
        result = check(engine, probe, view)
        rows.append({"engine": engine, "models": models, **result})
    if view is not None:
        for lane in pool_lanes("claude", "claude", view):
            rows.append({"engine": "claude (native host)", "models": "claude-*", "status": assess(lane), **summary(lane)})
    rows.append({"engine": "opencode", "models": "provider/*", "status": "not_applicable", "detail": "metered providers; no subscription window"})
    return rows


def blocked_message(engine: str, model: str, result: dict[str, Any], view: dict[str, Any] | None) -> str:
    until = result.get("blockedUntil")
    when = f" until {until}" if until else ""
    alternatives = [
        f"--engine {row['engine']} ({row['models']}): {row['label']}, {round(row['availableNowPct'])}% usable now"
        for row in route_table(view)
        if row["engine"] in ("codex", "agy") and row["status"] in ("available", "low") and row.get("label") != result.get("label")
    ]
    text = f"{result.get('label', engine)} quota is exhausted{when}; a {engine} run on {model} would stall."
    if alternatives:
        text += " Routes with quota now: " + "; ".join(alternatives) + "."
    return text + " Pass --ignore-quota only when the user asks to launch anyway."


def access_records(view: dict[str, Any] | None, registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Observed-access records for conductor_policy.recommend(), built from live quota.

    One record per profile model and account. Personal accounts are marked personal_subscription so
    recommend() keeps excluding them unless the packet authorizes personal quota.
    """
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    records = []
    for profile in registry.get("profiles", []):
        for model in profile.get("model_ids", []):
            engine = "codex" if model.startswith("gpt") else "agy" if model.startswith("gemini") else "claude" if model.startswith("claude") else None
            if engine is None:
                continue
            lanes = pool_lanes(engine, model, view) if view is not None else []
            if not lanes:
                records.append({"model": model, "engine": engine, "available": True, "observed_at": observed_at,
                                "source": "ai-usage:unknown", "billing_mode": "unknown", "quota": None})
                continue
            for lane in lanes:
                billing = lane.get("billing")
                records.append({
                    "model": model, "engine": engine, "available": assess(lane) != "blocked", "observed_at": observed_at,
                    "source": f"ai-usage:{lane['accountId']}",
                    "billing_mode": "personal_subscription" if billing == "personal" else "subscription",
                    "quota": {"status": assess(lane), **summary(lane)},
                })
    return records
