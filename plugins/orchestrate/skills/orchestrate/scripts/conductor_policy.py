"""Bounded next actions, invocation accounting, and reusable decision records."""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ARCHETYPE_ROLES = frozenset(
    {
        "debug-invariants",
        "structural-refactor",
        "ui-design-system",
        "architecture-conductor",
        "mechanical-sweep",
        "independent-review",
    }
)


@functools.cache
def _sibling_module(name: str) -> Any:
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("executor_policy_" + name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load policy dependency {name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _quota_order(candidate: dict[str, Any]) -> tuple:
    quota = candidate.get("quota") or {}
    return (
        quota.get("status") == "low",
        quota.get("status") == "credits",
        not quota.get("expiring"),
        not candidate.get("native"),
        -(quota.get("surplusPts") or 0),
    )


def _route_profile(model: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            profile
            for profile in registry.get("profiles", [])
            if model in profile.get("selectors", []) or model in profile.get("model_ids", [])
        ),
        None,
    )


def _recommend_archetype(
    packet: dict[str, Any],
    registry: dict[str, Any],
    access: list[dict[str, Any]],
    *,
    route_config: dict[str, dict[str, Any]] | None = None,
    telemetry_summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    role = packet["role"]
    routes = _sibling_module("routes")
    effective_routes = route_config if route_config is not None else routes.load_routes()
    if role not in effective_routes:
        return None
    candidates = routes.route_candidates(role, effective_routes)

    host = packet.get("host")
    personal_authorized = packet.get("personal_quota_authorized", False)
    output = []
    for route in candidates:
        profile = _route_profile(route["model"], registry)
        profile_models = set(profile.get("model_ids", [])) if profile else set()
        matching_access = [
            observed
            for observed in access
            if observed.get("engine") == route["engine"]
            and (not profile_models or observed.get("model") in profile_models)
        ]
        # Some routed tiers have no workload profile yet (for example Gemini Pro). The monitor pool
        # belongs to the engine family, so any live observation for that engine supplies quota only.
        if not matching_access and not profile_models:
            matching_access = [observed for observed in access if observed.get("engine") == route["engine"]]

        seen_pools = set()
        for observed in matching_access:
            if not observed.get("available"):
                continue
            source = observed.get("source")
            observed_at = observed.get("observed_at")
            if not isinstance(source, str) or not source.strip() or not isinstance(observed_at, str):
                continue
            try:
                when = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None or not 0 <= (dt.datetime.now(dt.UTC) - when).total_seconds() <= 86400:
                continue
            billing_mode = observed.get("billing_mode", "unknown")
            if billing_mode == "personal_subscription" and not personal_authorized:
                continue
            quota = observed.get("quota")
            pool_identity = (source, (quota or {}).get("poolId"), billing_mode)
            if pool_identity in seen_pools:
                continue
            seen_pools.add(pool_identity)
            output.append(
                {
                    **route,
                    "profile": profile["id"] if profile else None,
                    "native": route["engine"] == host,
                    "effort_candidate": route.get("effort") or (profile or {}).get("effort_candidate"),
                    "prompt_adjustment": (profile or {}).get("prompt_adjustment", ""),
                    "billing_mode": billing_mode,
                    "access_source": source,
                    "quota": quota,
                    "quota_pace": quota.get("status", "unknown") if isinstance(quota, dict) else "unknown",
                }
            )

    if not output:
        return {
            "status": "provisional_candidates",
            "role": role,
            "candidates": [],
            "selection_owner": "conductor",
            "ranking": "No accessible routed candidate has been observed in the current host quota pools.",
        }

    telemetry = _sibling_module("telemetry")
    summary = (
        telemetry_summary if telemetry_summary is not None else telemetry.summarize_runs(telemetry.default_runs_dir())
    )
    ordered = telemetry.rank_candidates_by_telemetry(output, role, summary=summary)

    ranking = (
        "Each candidate reports live quota pace and local outcome evidence; missing evidence is marked "
        "no_telemetry. Within healthy quota pools, candidates reorder only when both have at least three "
        "matching runs and their 95% Wilson lower bounds differ by more than 0.05; otherwise configured route "
        "order is retained. Recovery rate is accepted outcomes after a non-first-pass attempt divided by "
        "non-first-pass attempts."
    )
    return {
        "status": "provisional_candidates",
        "role": role,
        "candidates": ordered,
        "selection_owner": "conductor",
        "ranking": ranking,
    }


def invocation_budget(limit: int) -> dict[str, Any]:
    if type(limit) is not int or limit < 1:
        raise ValueError("invocation allowance must be a positive integer")
    return {
        "unit": "executor_invocations",
        "limit": limit,
        "reserved": {},
        "used": 0,
        "meaning": "One supervised native or CLI invocation; CLI automatic retries disabled. Not provider subscription quota or API dollars.",
    }


def reserve(state: dict[str, Any], request_id: str) -> None:
    budget = state["budget"]
    if request_id in budget["reserved"]:
        return
    if budget["used"] + sum(budget["reserved"].values()) >= budget["limit"]:
        raise ValueError("task invocation allowance exhausted; conductor takes over")
    budget["reserved"][request_id] = 1


def settle(state: dict[str, Any], run: dict[str, Any], result: dict[str, Any]) -> None:
    budget = state.get("budget")
    if budget is not None and run["request_id"] in budget["reserved"]:
        # Keep charged even for preflight failures: recovery must never infer
        # that an ambiguous remote operation was free or refund and replay it.
        budget["used"] += budget["reserved"].pop(run["request_id"])
    ledger = state.setdefault("cost_ledger", {})
    ledger[run["run_id"]] = {
        "kind": run["kind"],
        "outcome": result["status"],
        "usage": result.get("usage"),
        "billing_mode": result.get("billing_mode", "unknown"),
        "api_reference_estimate": result.get("tariff_estimate"),
        "native_estimate_usd": (result.get("usage") or {}).get("native_cost_usd"),
        "duration_seconds": result.get("duration_seconds"),
        "human_repair_minutes": None,
        "provider_quota_remaining": None,
    }


def next_action(state: dict[str, Any], observation: dict[str, Any], remaining: float) -> dict[str, Any]:
    """Return a host action, not another autonomous model invocation."""
    if state["status"] == "accepted":
        return {"action": "done", "owner": "conductor"}
    unresolved = [run for run in state["runs"] if run["status"] not in {"finished", "cancelled"}]
    if unresolved:
        run = unresolved[-1]
        action = (
            "interrupt"
            if remaining <= 0
            else "wait"
            if run["status"] in {"dispatching", "active", "reserved"}
            else "recover"
        )
        return {
            "action": action,
            "run_id": run["run_id"],
            "handle": run.get("native_handle") or run.get("pid"),
            "event_id": run.get("event_id"),
            "owner": "conductor",
            "relaunch": False,
        }
    budget = state.get("budget", {})
    if state["status"] != "active" or remaining <= 0 or budget.get("used", 0) >= budget.get("limit", 5):
        return {"action": "takeover", "owner": "conductor", "reason": "task_limit_or_explicit_takeover"}
    if observation.get("authority_missing"):
        return {"action": "ask_user", "owner": "conductor", "reason": observation["authority_missing"]}
    if observation.get("uncertain_fact") or observation.get("version_conflict") or observation.get("stale_evidence"):
        return {
            "action": "pointdexter",
            "owner": "current",
            "live_fetch_required": True,
            "resources": list(state.get("evidence", {}).get("resources", {}).values()),
        }
    consultations = sum(run["kind"] in {"consultation", "challenge"} for run in state["runs"])
    if observation.get("weak_answer"):
        if consultations >= state["limits"]["consultations"] or any(
            run["kind"] == "challenge" for run in state["runs"]
        ):
            return {"action": "takeover", "owner": "conductor", "reason": "peer_budget_exhausted"}
        return {
            "action": "experiment" if observation.get("discriminating_test") else "challenge",
            "owner": "current",
            "weakness": observation["weak_answer"],
        }
    if observation.get("review_id"):
        if any(run["kind"] == "correction" for run in state["runs"]):
            return {"action": "takeover", "owner": "conductor", "reason": "correction_budget_exhausted"}
        return {"action": "resume", "owner": "original_worker", "review_id": observation["review_id"]}
    if observation.get("question"):
        return {
            "action": "consult" if consultations < state["limits"]["consultations"] else "takeover",
            "owner": "current",
            "question": observation["question"],
        }
    unreviewed = [
        run for run in state["runs"] if not any(decision["run_id"] == run["run_id"] for decision in state["decisions"])
    ]
    if unreviewed:
        return {"action": "review", "owner": "conductor", "run_id": unreviewed[-1]["run_id"]}
    return {"action": "continue", "owner": "current"}


def decision(state: dict[str, Any], packet: dict[str, Any], evidence: dict[str, Any] | None) -> dict[str, Any]:
    for name in ("question", "chosen_action", "owner", "invalidation_trigger"):
        if not isinstance(packet.get(name), str) or not packet[name].strip():
            raise ValueError("decision requires " + name)
    if not isinstance(packet.get("applicability"), dict) or not packet["applicability"]:
        raise ValueError("decision needs applicability/version constraints")
    alternatives = packet.get("rejected_alternatives")
    if not isinstance(alternatives, list) or any(
        not value.get("alternative") or not value.get("why") for value in alternatives
    ):
        raise ValueError("rejected alternatives need an alternative and why")
    if packet.get("reusable") and evidence is None:
        raise ValueError("unverified claims cannot become reusable knowledge")
    if packet.get("reusable") and packet["applicability"].get("versions") != evidence["versions"]:
        raise ValueError("reusable decision applicability must match the verified versions")
    entry = {
        **packet,
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "evidence_review_id": evidence["review_id"] if evidence else None,
        "invalidated": None,
    }
    encoded = json.dumps(entry, sort_keys=True).encode()
    entry["decision_id"] = hashlib.sha256(encoded).hexdigest()[:24]
    state.setdefault("knowledge", []).append(entry)
    return entry


def recommend(
    packet: dict[str, Any],
    registry: dict[str, Any],
    *,
    route_config: dict[str, dict[str, Any]] | None = None,
    telemetry_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Filter provisional role candidates by observed access, never API price alone."""
    role = packet.get("role", "implementation")
    access = packet.get("access")
    if not isinstance(access, list):
        raise ValueError("profile recommendation requires observed access records")
    if role in ARCHETYPE_ROLES:
        routed = _recommend_archetype(
            {**packet, "role": role},
            registry,
            access,
            route_config=route_config,
            telemetry_summary=telemetry_summary,
        )
        if routed is not None:
            return routed
    candidates = []
    for profile in registry["profiles"]:
        if role not in profile["roles"]:
            continue
        for observed in access:
            when = dt.datetime.fromisoformat(observed["observed_at"].replace("Z", "+00:00"))
            if when.tzinfo is None or not 0 <= (dt.datetime.now(dt.UTC) - when).total_seconds() <= 86400:
                continue
            if (
                not observed.get("source")
                or observed.get("model") not in profile["model_ids"]
                or not observed.get("available")
            ):
                continue
            if observed.get("billing_mode") == "personal_subscription" and not packet.get("personal_quota_authorized"):
                continue
            candidates.append(
                {
                    "profile": profile["id"],
                    "model": observed["model"],
                    "engine": observed["engine"],
                    "native": observed["engine"] == packet.get("host"),
                    "effort_candidate": profile["effort_candidate"],
                    "prompt_adjustment": profile["prompt_adjustment"],
                    "billing_mode": observed.get("billing_mode", "unknown"),
                    "access_source": observed["source"],
                    "quota": observed.get("quota"),
                }
            )
    quota_known = any(candidate["quota"] for candidate in candidates)
    if quota_known:
        # Pools with room first (a run started in a nearly empty pool tends to stall); then pools near their
        # weekly rollover with capacity left (lost unless spent); then the host's own models (native first);
        # then unused capacity. Pools on credits never get here: they bill the user's balance, so access
        # marks them unavailable. This orders otherwise-suitable candidates; it says nothing about quality.
        candidates.sort(key=_quota_order)
    ranking = (
        "Ordered by quota: pools with room first, then expiring (use it or lose it), then native, then unused "
        "capacity (surplusPts); nearly empty pools last; pools on credits are left out; not quality; "
        if quota_known
        else "No empirical model ranking; "
    ) + "use task/context value gate before handoff."
    return {
        "status": "provisional_candidates",
        "role": role,
        "candidates": candidates,
        "selection_owner": "conductor",
        "ranking": ranking,
    }
