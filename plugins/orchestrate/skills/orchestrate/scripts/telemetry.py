#!/usr/bin/env python3
"""Summarize local task outcomes by archetype and executor route."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path
from typing import Any

Z_95 = 1.959963984540054
FAILURE_STATUSES = {"FAILED", "FAILURE", "VERIFICATION_FAILED", "PARTIAL"}


def default_runs_dir() -> Path:
    """Match run_agent.py's cache root and versioned result directory."""
    executor_home = os.environ.get("AGENT_EXECUTOR_HOME")
    if executor_home:
        cache = Path(executor_home).expanduser()
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        cache = (Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache") / "agent-executor"
    return cache / "runs-v1"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _field(result: dict[str, Any], key: str) -> Any:
    for source in (result, _mapping(result.get("route")), _mapping(result.get("request"))):
        value = source.get(key)
        if value is not None:
            return value
    return None


def _label(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("bound", value.get("effort"))
    if value is None or not str(value).strip():
        return "unspecified"
    return str(value).strip()


def _task_archetype(result: dict[str, Any]) -> str:
    for key in ("task_archetype", "role"):
        value = _field(result, key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "unspecified"


def _accepted(result: dict[str, Any]) -> bool:
    review = _mapping(result.get("review"))
    outcomes = (review.get("task_outcome"), result.get("task_outcome"), review.get("status"), result.get("status"))
    return any(isinstance(value, str) and value.strip().upper() in {"COMPLETE", "ACCEPTED"} for value in outcomes)


def _verification_failed(result: dict[str, Any]) -> bool:
    review = _mapping(result.get("review"))
    for verification in (result.get("verification"), review.get("verification")):
        if verification is False:
            return True
        if not isinstance(verification, dict):
            if isinstance(verification, str) and verification.strip().upper() in FAILURE_STATUSES:
                return True
            continue
        if verification.get("passed") is False:
            return True
        status = verification.get("status")
        if isinstance(status, str) and status.strip().upper() in FAILURE_STATUSES:
            return True
    return False


def _attempt_count(result: dict[str, Any]) -> int | None:
    attempts = result.get("attempts", result.get("executor_attempts"))
    if isinstance(attempts, list):
        return len(attempts)
    if type(attempts) is int and attempts >= 0:
        return attempts
    return None


def _first_pass(result: dict[str, Any], accepted: bool) -> bool:
    if not accepted or _verification_failed(result):
        return False
    attempts = _attempt_count(result)
    return not result.get("resumed_from") or attempts == 1


def _duration_ms(result: dict[str, Any]) -> float | None:
    timing = _mapping(result.get("timing"))
    candidates = (
        result.get("duration_ms"),
        result.get("elapsed_ms"),
        timing.get("duration_ms"),
    )
    for value in candidates:
        if type(value) in {int, float} and math.isfinite(value) and value >= 0:
            return float(value)
    seconds = result.get("duration_seconds")
    if type(seconds) in {int, float} and math.isfinite(seconds) and seconds >= 0:
        return float(seconds) * 1000
    return None


def wilson_lower_bound(successes: int, trials: int, z: float = Z_95) -> float | None:
    """Return the two-sided Wilson 95% lower bound, or None without observations."""
    if trials <= 0:
        return None
    rate = successes / trials
    z_squared = z * z
    denominator = 1 + z_squared / trials
    center = rate + z_squared / (2 * trials)
    margin = z * math.sqrt(rate * (1 - rate) / trials + z_squared / (4 * trials * trials))
    return max(0.0, (center - margin) / denominator)


def _read_results(runs_dir: Path) -> tuple[list[dict[str, Any]], int]:
    root = runs_dir.expanduser().resolve()
    results = []
    skipped = 0
    if not root.is_dir():
        return results, skipped
    for path in sorted(root.glob("*/result.json")):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                skipped += 1
                continue
            resolved = path.resolve(strict=True)
            if resolved.parent.parent != root or not resolved.is_file():
                skipped += 1
                continue
            value = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(value, dict):
            skipped += 1
            continue
        results.append(value)
    return results, skipped


def summarize_runs(runs_dir: Path | str) -> dict[str, Any]:
    """Group result receipts by task archetype, engine, model, and effort."""
    results, skipped = _read_results(Path(runs_dir))
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for result in results:
        key = (
            _task_archetype(result),
            _label(_field(result, "engine")),
            _label(_field(result, "model")),
            _label(_field(result, "effort")),
        )
        groups.setdefault(key, []).append(result)

    rows = []
    for (archetype, engine, model, effort), values in sorted(groups.items()):
        accepted_count = sum(_accepted(value) for value in values)
        first_pass_count = sum(_first_pass(value, _accepted(value)) for value in values)
        durations = [duration for value in values if (duration := _duration_ms(value)) is not None]
        run_count = len(values)
        bound = wilson_lower_bound(accepted_count, run_count)
        rows.append(
            {
                "task_archetype": archetype,
                "engine": engine,
                "model": model,
                "effort": effort,
                "runs": run_count,
                "first_pass_rate": round(first_pass_count / run_count, 6),
                "accepted_rate": round(accepted_count / run_count, 6),
                "wilson_lb_95": round(bound, 6) if bound is not None else None,
                "median_duration_ms": round(statistics.median(durations), 3) if durations else None,
            }
        )
    return {
        "schema": "agent-executor.telemetry.v1",
        "runs": len(results),
        "skipped_results": skipped,
        "groups": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    summarize = commands.add_parser("summarize", help="summarize local result receipts")
    summarize.add_argument("--runs-dir", type=Path, default=default_runs_dir())
    summarize.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    print(json.dumps(summarize_runs(args.runs_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
