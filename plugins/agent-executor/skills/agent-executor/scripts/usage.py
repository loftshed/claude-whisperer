"""Conservative token accounting. Provider counters are evidence, not invoices."""

from __future__ import annotations

import json
import math
import datetime as dt
import re
from typing import Any

BUCKETS = ("fresh_input", "cache_read", "cache_write", "output")


def count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def empty() -> dict[str, Any]:
    return {
        "schema": "agent-executor.usage.v1",
        "status": "unknown",
        "totals": dict.fromkeys(BUCKETS),
        "raw": [],
        "cumulative": None,
        "request_context_tokens": None,
        "requests": None,
        "cache_write_class": None,
        "cache_ttl_seconds": None,
        "model_observations": [],
        "native_cost_usd": None,
        "coverage": "reported_counters_only; completeness_not_independently_established",
        "gaps": [],
    }


def events(text: str) -> list[dict[str, Any]]:
    values = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def codex_buckets(raw: dict[str, Any]) -> dict[str, int | None]:
    total = count(raw.get("input_tokens"))
    read = count(raw.get("cached_input_tokens"))
    write = count(raw.get("cache_write_input_tokens"))
    fresh = total - read - write if None not in (total, read, write) else None
    if fresh is not None and fresh < 0:
        fresh = None
    return {"fresh_input": fresh, "cache_read": read, "cache_write": write, "output": count(raw.get("output_tokens"))}


def agy_events(text: str) -> list[dict[str, Any]]:
    """AGY 1.2.6 wire envelope; retain older documented flat examples too."""
    result = []
    for event in events(text):
        kind = event.get("event")
        if kind in {"init", "step_update", "result"} and isinstance(event.get(kind), dict):
            result.append({**event[kind], "type": kind})
        else:
            result.append(event)
    return result


def normalize_usage(
    engine: str, text: str, *, resumed: bool = False, baseline: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = empty()
    parsed = events(text)
    if engine == "codex":
        raw = [event["usage"] for event in parsed if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict)]
        result["raw"] = raw
        result["accounting"] = "conversation_cumulative"
        if raw:
            result["cumulative"] = raw[-1]
            current = codex_buckets(raw[-1])
            if resumed:
                if baseline is None:
                    result["gaps"].append("resumed_session_without_usage_baseline")
                else:
                    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
                    pairs = {field: (count(raw[-1].get(field)), count(baseline.get(field))) for field in fields}
                    if any(new is not None and old is not None and new < old for new, old in pairs.values()):
                        result["gaps"].append("cumulative_counters_reset")
                    else:
                        delta = {field: new - old if None not in (new, old) else None for field, (new, old) in pairs.items()}
                        result["totals"] = codex_buckets(delta)
            else:
                result["totals"] = current
            result["gaps"].append("per_request_context_sizes_not_reported")
    elif engine == "opencode":
        records: dict[str, dict[str, Any]] = {}
        unidentified = False
        for event in parsed:
            part = event.get("part")
            if event.get("type") != "step_finish" or not isinstance(part, dict):
                continue
            result["raw"].append(part)
            if not isinstance(part.get("id"), str) or not part["id"]:
                unidentified = True
                continue
            identity = json.dumps([event.get("sessionID"), part.get("messageID"), part["id"]])
            records[identity] = part
        result["accounting"] = "per_step"
        if records and not unidentified:
            totals: dict[str, int | None] = dict.fromkeys(BUCKETS, 0)
            contexts = []
            native_cost: float | None = 0.0
            requests = []
            for part in records.values():
                tokens = part.get("tokens", {})
                tokens = tokens if isinstance(tokens, dict) else {}
                cache = tokens.get("cache", {})
                cache = cache if isinstance(cache, dict) else {}
                output, reasoning = count(tokens.get("output")), count(tokens.get("reasoning"))
                buckets = {
                    "fresh_input": count(tokens.get("input")),
                    "cache_read": count(cache.get("read")),
                    "cache_write": count(cache.get("write")),
                    "output": output + reasoning if None not in (output, reasoning) else None,
                }
                for name in BUCKETS:
                    totals[name] = totals[name] + buckets[name] if None not in (totals[name], buckets[name]) else None
                inputs = [buckets[name] for name in BUCKETS[:3]]
                contexts.append(sum(inputs) if None not in inputs else None)
                requests.append({"request_id": part["id"], "message_id": part.get("messageID"),
                    "context_tokens": contexts[-1], "totals": buckets, "raw": tokens,
                    "cache_write_class": None, "cache_ttl_seconds": None})
                cost = part.get("cost")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 and native_cost is not None:
                    native_cost += cost
                else:
                    native_cost = None
            result.update(totals=totals, request_context_tokens=contexts, requests=requests, native_cost_usd=native_cost)
        if unidentified:
            result["gaps"].append("step_usage_without_identity_cannot_be_deduplicated")
    elif engine == "claude":
        finals = [event for event in parsed if event.get("type") == "result" and isinstance(event.get("usage"), dict)]
        result["raw"] = [event["usage"] for event in finals]
        result["accounting"] = "process_session_cumulative"
        if finals:
            raw = finals[-1]["usage"]
            result["cumulative"] = raw
            result["totals"] = {"fresh_input": count(raw.get("input_tokens")), "cache_read": count(raw.get("cache_read_input_tokens")),
                                "cache_write": count(raw.get("cache_creation_input_tokens")), "output": count(raw.get("output_tokens"))}
            cost = finals[-1].get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
                result["native_cost_usd"] = cost
        result["gaps"].append("per_request_context_sizes_not_reported")
    elif engine == "agy":
        snapshots = {}
        final = []
        for event in agy_events(text):
            kind = event.get("type")
            if kind == "init" and event.get("model"):
                result["model_observations"].append({"model": event["model"], "source": "init.model", "meaning": "configured_not_backend_resolved"})
            if not isinstance(event.get("usage"), dict):
                continue
            result["raw"].append({"type": kind, "conversation_id": event.get("conversation_id"),
                                   "step_index": event.get("step_index"), "usage": event["usage"]})
            if kind == "step_update" and event.get("conversation_id") and type(event.get("step_index")) is int:
                snapshots[(event["conversation_id"], event["step_index"])] = event["usage"]
            elif kind == "result":
                final.append(event["usage"])
        result["accounting"] = "resumed_scope_unverified" if resumed else "process_session_cumulative"
        result["cumulative"] = final[-1] if final else None
        result["step_snapshots"] = [{"conversation_id": key[0], "step_index": key[1], "usage": value}
                                    for key, value in snapshots.items()]
        result["gaps"].extend(["AGY_exclusive_billing_semantics_unverified", "cache_write_class_TTL_and_request_context_not_reported"])
        if resumed:
            result["gaps"].append("separate_process_resume_counter_scope_unverified")
    else:
        result["accounting"] = "unavailable"
        result["raw"] = [event["usage"] for event in parsed if isinstance(event.get("usage"), dict)]
        result["gaps"].append("adapter_has_no_verified_incremental_usage_contract")
    known = sum(value is not None for value in result["totals"].values())
    result["status"] = "known" if known == len(BUCKETS) else "partial" if known else "unknown"
    if not result["raw"]:
        result["gaps"].append("usage_not_reported")
    elif known != len(BUCKETS):
        result["gaps"].append("unknown_counters_are_not_zero")
    return result


def combine_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    result = empty()
    if not attempts:
        return result
    for name in BUCKETS:
        values = [attempt["totals"][name] for attempt in attempts]
        result["totals"][name] = sum(values) if None not in values else None
    result["attempts"] = attempts
    result["cumulative"] = attempts[-1]["cumulative"]
    result["gaps"] = list(dict.fromkeys(gap for attempt in attempts for gap in attempt["gaps"]))
    known = sum(value is not None for value in result["totals"].values())
    result["status"] = "known" if known == len(BUCKETS) else "partial" if known else "unknown"
    costs = [attempt["native_cost_usd"] for attempt in attempts]
    result["native_cost_usd"] = sum(costs) if None not in costs else None
    if all(attempt.get("requests") is not None for attempt in attempts):
        result["requests"] = [request for attempt in attempts for request in attempt["requests"]]
        result["request_context_tokens"] = [request["context_tokens"] for request in result["requests"]]
    result["model_observations"] = [observation for attempt in attempts for observation in attempt.get("model_observations", [])]
    return result


def estimate_tariff(
    usage: dict[str, Any], model: str, registry: dict[str, Any], *, billing_mode: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Estimate reported counters only. Refuse stale or unresolvable tariff tiers."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result: dict[str, Any] = {
        "kind": "standard_api_reference_for_reported_usage", "model": model,
        "model_basis": "command_bound_not_provider_verified", "billing_mode": billing_mode,
        "estimated_usd": None, "actual_charge_usd": None,
        "tariff_verified_at": registry.get("verified_at"), "gaps": [],
    }
    models = registry.get("models", {})
    # AGY encodes effort in the model slug (gemini-3.8-flash-high); the tariff is per base model.
    entry = models.get(model) or models.get(re.sub(r"-(low|medium|high)$", "", model))
    if entry is None:
        result["gaps"].append("model_has_no_verified_tariff")
        return result
    result["sources"] = entry["sources"]
    verified = dt.datetime.fromisoformat(registry["verified_at"].replace("Z", "+00:00"))
    age = (now - verified).total_seconds()
    if not 0 <= age <= registry["max_age_seconds"] or (entry.get("revalidate_on") and now.date().isoformat() >= entry["revalidate_on"]):
        result["gaps"].append("tariff_requires_live_refresh")
        return result
    totals = usage.get("totals", {})
    if any(count(totals.get(name)) is None for name in BUCKETS):
        result["gaps"].append("incomplete_usage")
        return result
    rates = dict(entry["rates_per_million"])
    if any(totals[name] and rates[name] is None for name in BUCKETS):
        result["gaps"].append("cache_write_class_or_storage_usage_required")
        return result
    tier = entry.get("long_context")
    if tier and sum(totals[name] for name in BUCKETS[:3]) > tier["above_input_tokens"]:
        # A thread total alone cannot select a per-request context tier.
        contexts = [context for attempt in usage.get("attempts", [usage]) for context in (attempt.get("request_context_tokens") or [None])]
        if any(context is None for context in contexts) or any(context > tier["above_input_tokens"] for context in contexts):
            result["gaps"].append("per_request_tariff_tier_not_resolved")
            return result
    result["estimated_usd"] = round(sum(totals[name] * (rates[name] or 0) for name in BUCKETS) / 1_000_000, 10)
    result["gaps"].append("tools_storage_service_tier_and_unreported_usage_excluded")
    return result
