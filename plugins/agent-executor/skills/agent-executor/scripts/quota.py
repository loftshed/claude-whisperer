"""Live subscription quota per executor route, read from ai-usage at decision time. Nothing is stored.

ai-usage (claude-whisperer/plugins/ai-usage) reports each account's rate-limit windows and ranks its
pools. This module maps an engine and model onto the pool that pays for it, so the conductor can see
whether a route can take a run now and the runner can refuse to launch into an exhausted one.

A pool whose included allowance is used up but that has a credit balance (Codex/ChatGPT) has status
"credits": it takes runs of any size, metered against the balance, so it is never refused.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import ssl
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA_PREFIX = "ai-usage.snapshot."
LOW_USABLE_PCT = 10
EXIT_QUOTA_EXHAUSTED = 15
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
LOW_CREDIT_USD = 1


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
            [command, "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
            check=False,
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
    # An "error" lane means ai-usage has no data for that account at all: unknown, not empty.
    lanes = [
        lane
        for lane in view.get("lanes", [])
        if accounts.get(lane.get("accountId"), {}).get("provider") == provider and lane.get("status") != "error"
    ]
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
    status = lane.get("status")
    if status in ("blocked", "credits"):
        return status
    if (lane.get("availableNowPct") or 0) < LOW_USABLE_PCT:
        return "low"
    return "available"


def check(engine: str, model: str, view: dict[str, Any] | None) -> dict[str, Any]:
    """Whether a run on engine/model can start now: available, low, credits, blocked, unknown or not_applicable."""
    if route_for(engine, model) is None:
        return {"status": "not_applicable", "detail": f"{engine} bills per request; no subscription window to check"}
    if view is None:
        return {"status": "unknown", "detail": "ai-usage is not installed or did not answer"}
    lanes = pool_lanes(engine, model, view)
    if not lanes:
        provider = route_for(engine, model)[0]
        providers = {account["id"]: account.get("provider") for account in view.get("accounts", [])}
        failed = [
            lane
            for lane in view.get("lanes", [])
            if lane.get("status") == "error" and providers.get(lane.get("accountId")) == provider
        ]
        detail = failed[0].get("advice") if failed else f"ai-usage has no pool for {engine} {model}"
        return {"status": "unknown", "detail": detail}
    lane = lanes[0]
    return {"status": assess(lane), **summary(lane)}


def summary(lane: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "label",
        "accountId",
        "poolId",
        "billing",
        "credits",
        "availableNowPct",
        "weeklyRemainingPct",
        "surplusPts",
        "blockedUntil",
        "expiring",
        "expiresAt",
        "burnPctPerHour",
        "advice",
        "stale",
    )
    return {key: lane.get(key) for key in keys}


def openrouter_key() -> str | None:
    """The OpenRouter key OpenCode uses: OPENROUTER_API_KEY, else OpenCode's stored auth."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    try:
        auth = json.loads((data_home / "opencode" / "auth.json").read_text())
    except (OSError, ValueError):
        return None
    key = auth.get("openrouter", {}).get("key") if isinstance(auth, dict) else None
    return key if isinstance(key, str) and key else None


def tls_context() -> ssl.SSLContext:
    """Default trust plus any extra CA bundle the environment names (corporate TLS inspection)."""
    context = ssl.create_default_context()
    for variable in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"):
        bundle = os.environ.get(variable)
        if bundle and os.path.isfile(bundle):
            context.load_verify_locations(bundle)
    return context


def openrouter_credit(timeout: float = 15) -> dict[str, Any] | None:
    """Remaining OpenRouter account credit in USD, or None when there is no key or the API fails."""
    key = openrouter_key()
    if not key:
        return None
    request = urllib.request.Request(OPENROUTER_CREDITS_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
            data = json.loads(response.read()).get("data", {})
        total, used = float(data["total_credits"]), float(data["total_usage"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return {"totalUsd": total, "usedUsd": used, "remainingUsd": total - used}


def credit_row(credit: dict[str, Any] | None) -> dict[str, Any]:
    row = {"engine": "opencode", "models": "openrouter/*"}
    if credit is None:
        return {
            **row,
            "status": "unknown",
            "detail": "OpenRouter credit unreadable (no key, or the API did not answer)",
        }
    remaining = credit["remainingUsd"]
    status = "blocked" if remaining <= 0 else "low" if remaining < LOW_CREDIT_USD else "available"
    return {**row, "status": status, **credit, "detail": f"${remaining:.2f} OpenRouter credit left"}


ROUTES = (
    ("codex", "gpt-*", "codex"),
    ("agy", "gemini-*", "gemini"),
    ("agy", "claude-* / gpt-oss-*", "claude-gpt"),
)


def route_table(view: dict[str, Any] | None, credit: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Quota for every route agent-executor can dispatch to, plus the native Claude accounts for reference.

    OpenCode's row is OpenRouter's remaining dollar credit (from openrouter_credit()), not a window.
    """
    rows = []
    for engine, models, sample in ROUTES:
        probe = {"codex": "gpt", "gemini": "gemini", "claude-gpt": "claude"}[sample]
        result = check(engine, probe, view)
        rows.append({"engine": engine, "models": models, **result})
    if view is not None:
        for lane in pool_lanes("claude", "claude", view):
            rows.append(
                {"engine": "claude (native host)", "models": "claude-*", "status": assess(lane), **summary(lane)}
            )
    rows.append(credit_row(credit))
    return rows


def credits_text(row: dict[str, Any]) -> str:
    """Describe a route running on a credit balance: "on credits, 62,168.59 cr left"."""
    credits = row.get("credits") or {}
    return f"on credits, {credits.get('text', 'balance unknown')} left"


def blocked_message(engine: str, model: str, result: dict[str, Any], view: dict[str, Any] | None) -> str:
    until = result.get("blockedUntil")
    when = f" until {until}" if until else ""
    alternatives = [
        f"--engine {row['engine']} ({row['models']}): {row['label']}, "
        + (credits_text(row) if row["status"] == "credits" else f"{round(row['availableNowPct'])}% usable now")
        for row in route_table(view)
        if row["engine"] in ("codex", "agy")
        and row["status"] in ("available", "low", "credits")
        and row.get("label") != result.get("label")
    ]
    text = f"{result.get('label', engine)} quota is exhausted{when}; a {engine} run on {model} would stall."
    if alternatives:
        text += " Routes with quota now: " + "; ".join(alternatives) + "."
    return text + " Pass --ignore-quota only when the user asks to launch anyway."


# Engines the runner can dispatch to from any host (a Claude host uses its native sub-agents for Claude).
DISPATCHABLE = ("codex", "agy", "claude")


def access_records(
    view: dict[str, Any] | None, registry: dict[str, Any], host: str | None = None
) -> list[dict[str, Any]]:
    """Observed-access records for conductor_policy.recommend(), built from live quota.

    One record per profile model and account, for routes this host can actually use: the runner's
    engines plus the host's own models. Personal accounts are marked personal_subscription so
    recommend() keeps excluding them unless the packet authorizes personal quota. A pool on credits is
    available with billing_mode "credits": every request is metered against a prepaid balance.
    """
    observed_at = dt.datetime.now(dt.UTC).isoformat()
    records = []
    for profile in registry.get("profiles", []):
        for model in profile.get("model_ids", []):
            engine = (
                "codex"
                if model.startswith("gpt")
                else "agy"
                if model.startswith("gemini")
                else "claude"
                if model.startswith("claude")
                else None
            )
            if engine is None or (engine not in DISPATCHABLE and engine != host):
                continue
            lanes = pool_lanes(engine, model, view) if view is not None else []
            if not lanes:
                records.append(
                    {
                        "model": model,
                        "engine": engine,
                        "available": True,
                        "observed_at": observed_at,
                        "source": "ai-usage:unknown",
                        "billing_mode": "unknown",
                        "quota": None,
                    }
                )
                continue
            for lane in lanes:
                billing = lane.get("billing")
                status = assess(lane)
                if billing == "personal":
                    billing_mode = "personal_subscription"
                elif status == "credits":
                    billing_mode = "credits"
                else:
                    billing_mode = "subscription"
                records.append(
                    {
                        "model": model,
                        "engine": engine,
                        "available": status != "blocked",
                        "observed_at": observed_at,
                        "source": f"ai-usage:{lane['accountId']}",
                        "billing_mode": billing_mode,
                        "quota": {"status": status, **summary(lane)},
                    }
                )
    return records
