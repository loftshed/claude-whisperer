#!/usr/bin/env python3
"""Record comparable real task outcomes without inventing missing measurements.

The conductor runs each task through its native facilities or coordinator. This
ledger validates those receipts and cannot promote a route on self-ratings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

STRATEGIES = ("current_owner", "fresh_worker", "owner_plus_consultant")


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def validate_manifest(manifest: dict[str, Any]) -> None:
    tasks = manifest.get("tasks", [])
    if not 12 <= len(tasks) <= 20 or len({task["task_id"] for task in tasks}) != len(tasks):
        raise ValueError("calibration requires 12–20 distinct representative tasks")
    if sum(task.get("split") == "held_out" for task in tasks) < 3:
        raise ValueError("reserve at least three held-out tasks before running comparisons")
    if sum(task.get("repeat") is True for task in tasks) < 2:
        raise ValueError("repeat at least two tasks to expose variance")
    for task in tasks:
        for key in ("task_id", "category", "objective", "start_revision", "acceptance_criteria", "context_packet", "provenance"):
            if not task.get(key):
                raise ValueError("calibration task requires " + key)
        if task.get("split") not in {"training", "held_out"}:
            raise ValueError("task split must be fixed before results are observed")


def record(folder: Path, packet: dict[str, Any]) -> dict[str, Any]:
    manifest = read(folder / "manifest.json")
    task = next((task for task in manifest["tasks"] if task["task_id"] == packet.get("task_id")), None)
    if task is None or packet.get("strategy") not in STRATEGIES or packet.get("repeat_index") not in {0, 1}:
        raise ValueError("unknown task/strategy/repeat")
    if packet["repeat_index"] and not task.get("repeat"):
        raise ValueError("this task was not selected for a repeat")
    if not packet.get("candidate") or not packet.get("context_setup"):
        raise ValueError("record the candidate route and how owner context was established")
    state_path = Path(packet["task_state"]).resolve(strict=True)
    state = read(state_path)
    if state["task_spec"]["acceptance_criteria"] != task["acceptance_criteria"]:
        raise ValueError("comparison changed the original acceptance criteria")
    if not state["runs"] or any(run["status"] != "finished" for run in state["runs"]):
        raise ValueError("complete or explicitly resolve every attempt before calibration")
    if state["status"] not in {"accepted", "conductor_takeover", "rejected"}:
        raise ValueError("conductor must finish outcome review before calibration")
    results = []
    for run in state["runs"]:
        path = Path(run["result_path"])
        if sha(path) != run["result_sha256"]:
            raise ValueError("result changed after host collection")
        result = read(path)
        if (result.get("git_identity_before") or {}).get("head") != task["start_revision"]:
            raise ValueError("comparison did not use the fixed starting revision")
        results.append(result)
    if packet["strategy"] == "owner_plus_consultant" and not any(run["kind"] == "consultation" for run in state["runs"]):
        raise ValueError("consultant strategy must include its actual consultation and cost")
    if state["status"] == "accepted" and not any(decision["decision"] == "accept" for decision in state["decisions"]):
        raise ValueError("accepted outcome has no independent host decision")
    for key in ("regressions", "missed_constraints", "review_false_positives"):
        if not isinstance(packet.get(key), list):
            raise ValueError("conductor review must record " + key)
    # None means unmeasured, never zero. Native tool telemetry is often absent.
    for key in ("human_repair_minutes", "reconstruction_minutes", "wall_seconds", "provider_quota_units"):
        if key not in packet or (packet[key] is not None and (type(packet[key]) not in {int, float} or not math.isfinite(packet[key]) or packet[key] < 0)):
            raise ValueError("supply a nonnegative measurement or null for " + key)
    value = {**packet, "task_state_sha256": sha(state_path), "accepted": state["status"] == "accepted",
             "attempts": [{"model_requested": result.get("requested_model", result.get("model")),
                           "model_bound": result.get("model"), "model_observed": result.get("resolved_model"),
                           "effort": result.get("effort"), "engine": result["engine"],
                           "cli_version": result.get("executor_version"), "status": result["status"],
                           "usage": result.get("usage"), "tariff_estimate": result.get("tariff_estimate"),
                           "duration_seconds": result.get("duration_seconds")}
                          for result in results]}
    name = hashlib.sha256(json.dumps([packet["task_id"], packet["strategy"], packet["candidate"], packet["repeat_index"]]).encode()).hexdigest()
    path = folder / "results" / (name + ".json")
    if path.exists() and read(path) != value:
        raise ValueError("comparison already recorded; do not replace an unfavorable outcome")
    if not path.exists():
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(value, indent=2) + "\n")
    return value


def summarize(folder: Path) -> dict[str, Any]:
    manifest = read(folder / "manifest.json")
    rows = [read(path) for path in sorted((folder / "results").glob("*.json"))]
    groups = {}
    for row in rows:
        groups.setdefault((row["candidate"], row["strategy"]), []).append(row)
    result = {"schema": "agent-executor.calibration-report.v1", "groups": [], "promotion": "not_established"}
    for (candidate, strategy), values in groups.items():
        expected = {(task["task_id"], repeat) for task in manifest["tasks"] for repeat in range(2 if task["repeat"] else 1)}
        seen = {(value["task_id"], value["repeat_index"]) for value in values}
        accepted = sum(value["accepted"] for value in values)
        costs = [attempt.get("tariff_estimate", {}).get("estimated_usd") if attempt.get("tariff_estimate") else 0 if (attempt.get("usage") or {}).get("coverage") == "host_checks_only; no_model_invocation" else None for value in values for attempt in value["attempts"]]
        elapsed = [value["wall_seconds"] for value in values]
        group = {"candidate": candidate, "strategy": strategy, "trials": len(values), "accepted": accepted,
            "complete": expected == seen, "missing_trials": sorted(expected - seen),
            "regressions": sum(len(value["regressions"]) for value in values),
            "missed_constraints": sum(len(value["missed_constraints"]) for value in values),
            "review_false_positives": sum(len(value["review_false_positives"]) for value in values),
            "api_reference_cost_per_accepted_task": sum(costs) / accepted if costs and None not in costs and accepted else None,
            "mean_wall_seconds": statistics.mean(elapsed) if elapsed and None not in elapsed else None,
            "unknown_usage_trials": sum(any(not attempt.get("usage") or attempt["usage"]["status"] != "known" for attempt in value["attempts"]) for value in values),
            "human_repair_minutes": sum(value["human_repair_minutes"] for value in values) if all(value["human_repair_minutes"] is not None for value in values) else None,
            "provider_quota_units": sum(value["provider_quota_units"] for value in values) if all(value["provider_quota_units"] is not None for value in values) else None}
        result["groups"].append(group)
    result["comparison_ready"] = bool(groups) and all(group["complete"] for group in result["groups"]) and set(group["strategy"] for group in result["groups"]) == set(STRATEGIES)
    result["promotion_rule"] = "Conductor must review held-out outcomes, misses, repairs, variance, and relevant measured cost. No automatic brand ranking or self-rating promotion."
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "record", "summarize"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--packet", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            manifest = read(args.packet)
            validate_manifest(manifest)
            args.directory.mkdir(parents=True, mode=0o700, exist_ok=False)
            (args.directory / "results").mkdir(mode=0o700)
            write(args.directory / "manifest.json", manifest)
            result = {"status": "initialized", "tasks": len(manifest["tasks"]), "strategies": STRATEGIES}
        elif args.action == "record":
            result = record(args.directory, read(args.packet))
        else:
            result = summarize(args.directory)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as error:
        print(f"calibration: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
