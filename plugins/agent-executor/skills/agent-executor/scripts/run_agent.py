#!/usr/bin/env python3
"""Run one bounded external-agent delegation and retain structured evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to atomic replacement.
    fcntl = None  # type: ignore[assignment]


DEFAULT_ENGINE = "codex"
EXECUTION_ENGINES = ("codex", "agy", "opencode", "claude")
CATALOG_ENGINES = EXECUTION_ENGINES
PREFERRED_MODELS = {
    "codex": "gpt-5.6-luna",
    "agy": "gemini-3.8-flash-medium",
    "opencode": "openrouter/z-ai/glm-5.3-flash",
    "claude": "opus",
}
# Claude Code has no model-list command. Its aliases resolve to the current model of each tier; exact
# `claude-*` IDs are passed through and recorded as unverified.
CLAUDE_MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")
CLAUDE_MODEL_ID = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*(\[1m\])?$")
# Host session state that must not leak into an executor: Claude Code messaging tokens and the host's
# account profile (an executor could otherwise act inside, or bill to, the host session).
HOST_SESSION_ENV = re.compile(r"^(CLAUDECODE|CLAUDE_CODE_.*|CLAUDE_CONFIG_DIR|AI_AGENT)$")
# Provider messages that mean the account is out of quota or credits, not that the task failed.
PROVIDER_QUOTA_MARKERS = ("resource_exhausted", "code 429", "http 429", "status 429", "error 429",
                          "rate limit", "rate-limit", "quota exceeded", "usage limit", "insufficient credits",
                          "insufficient_quota", "credit balance")
EXIT_PROVIDER_QUOTA = 16
EXIT_ROUTE_NOT_ALLOWED = 17

DEFAULT_OPENCODE_VARIANT = "high"
OPENCODE_RUNTIME_CONFIG = {
    "permission": "allow",
    "agent": {"build": {"permission": "allow"}},
    "share": "disabled",
    "autoupdate": False,
}
DEFAULT_TIMEOUT = "15m"
DEFAULT_VERIFY_TIMEOUT = "10m"
DEFAULT_HEARTBEAT_SECONDS = 0.0
DEFAULT_EVENT_WAIT_TIMEOUT = "30m"
DEFAULT_EVENT_POLL_SECONDS = 0.5
DEFAULT_CATALOG_MAX_AGE_SECONDS = 24 * 60 * 60
MAX_FINAL_MESSAGE_CHARS = 4000
MAX_COMPLETION_SIGNAL_ERROR_CHARS = 500
MAX_VERIFICATION_TAIL_BYTES = 64 * 1024
MAX_VERIFICATION_TAIL_LINE_CHARS = 500
RUNNER_VERSION = "0.10.0"
RESULT_SCHEMA = "agent-executor.result.v2"
MODEL_CATALOG_SCHEMA = "agent-executor.models.v1"
MODEL_CACHE_SCHEMA = "agent-executor.model-cache.v1"
COMPLETION_EVENT_SCHEMA = "agent-executor.completion.v1"
COMPLETION_LIST_SCHEMA = "agent-executor.completion-list.v1"
COMPLETION_SIGNAL_SCHEMA = "agent-executor.completion-signal.v1"
NOTIFICATION_MODES = ("none", "desktop")
REPORT_HEADINGS = (
    "STATUS",
    "FILES CHANGED",
    "COMMANDS RUN",
    "VERIFICATION",
    "RISKS OR BLOCKERS",
)
REPORT_CONTRACT = """

EXECUTION AND REPORT CONTRACT
- Work directly in the provided workspace and follow the approved brief exactly.
- You have full tool permissions for this run. That capability does not authorize broader scope.
- Preserve all pre-existing and unrelated user changes.
- Do not start nested peer CLI runners inside this audited job. Request needed consultation and stop so the conductor can finish the audit first.
- Do not commit, push, merge, reset, clean, stash, checkout, or switch branches.
- Do not access secrets or unrelated private files.
- If correct completion requires destructive action, external publication, credentials, or scope beyond the brief, stop and report the blocker.
- Run every verification command named in the brief. Do not claim a command passed unless you observed its exit status.
- Do not edit plans, checklists, status ledgers, or completion claims unless the brief explicitly names the exact file and required evidence.
- A passing command proves only its named verification scope. Do not infer or declare broader project completion.
- Do not narrate tool calls or repeat the plan in the final response.
- End with a compact report using exactly these headings:
  STATUS
  FILES CHANGED
  COMMANDS RUN
  VERIFICATION
  RISKS OR BLOCKERS
- Under STATUS, put exactly one canonical outcome on the first non-empty line:
  COMPLETE, BLOCKED, or FAILED.
- Use BLOCKED when correct completion needs authority, information, credentials, or scope
  that the brief does not provide. Use FAILED when the attempted work is incomplete for
  another reason. Never label either condition COMPLETE.
- For a technical blocker, you may put one optional fenced block tagged exactly
  ```consultation-request under RISKS OR BLOCKERS. Its JSON object has exactly these fields:
  question and decision_needed are nonempty strings; observations, attempts, versions,
  and hypotheses are arrays of strings. Include observations and at least one hypothesis.
  Do not use a generic ```json tag or add approval/verified fields. This only requests advice.
- Keep the report concise. Do not paste complete files, large diffs, or raw logs.
""".strip()


class RunnerError(Exception):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cwd", type=Path, default=Path.cwd(), help="Target Git repository root"
    )
    task_input = parser.add_mutually_exclusive_group(required=True)
    task_input.add_argument(
        "--brief", help="Handwritten Markdown brief file path, or '-' to read stdin"
    )
    task_input.add_argument(
        "--task-spec",
        help=(
            "Compact JSON task spec file, or '-' to read stdin. Its repository_root and "
            "scope are authoritative."
        ),
    )
    parser.add_argument(
        "--route",
        help="Role from the route config (implementation, consultation, gemini, claude, opencode, or your "
        "own): fills in engine, model and effort. Explicit --engine/--model/--effort still win",
    )
    parser.add_argument(
        "--engine",
        choices=EXECUTION_ENGINES,
        help="Local executor CLI (default: the --route's engine, else codex)",
    )
    parser.add_argument(
        "--model",
        help="Exact live model ID; an omitted value uses a validated policy preference",
    )
    parser.add_argument(
        "--refresh-models",
        action="store_true",
        help="Bypass the model cache before execution",
    )
    parser.add_argument(
        "--claude-config-dir",
        type=Path,
        help="Claude account profile (CLAUDE_CONFIG_DIR) for --engine claude; default is Claude Code's default "
        "profile. Choosing an account chooses its billing: follow the user's rules",
    )
    parser.add_argument(
        "--ignore-quota",
        action="store_true",
        help="Launch even when ai-usage reports the route's subscription quota as exhausted",
    )
    parser.add_argument(
        "--variant",
        help="OpenCode model variant (default: high); invalid for other engines",
    )
    parser.add_argument("--effort", help="Catalog-validated Codex effort or AGY model-slug effort")
    parser.add_argument(
        "--billing-mode", choices=("unknown", "subscription", "api"), default="unknown",
        help="Declared billing context; never inferred from credentials",
    )
    parser.add_argument(
        "--resume-result", type=Path,
        help="Previous result.json for exact-session correction and cumulative usage baseline",
    )
    parser.add_argument(
        "--timeout",
        default=DEFAULT_TIMEOUT,
        help="Outer executor duration such as 15m or 1h",
    )
    parser.add_argument("--deadline", help="Optional task-wide ISO-8601 deadline with timezone")
    parser.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help=(
            "Non-mutating POSIX-shell command run by the runner after a clean "
            "executor handoff; repeatable and fail-fast"
        ),
    )
    parser.add_argument(
        "--verify-timeout",
        default=DEFAULT_VERIFY_TIMEOUT,
        help="Per-command post-verification timeout such as 10m or 1h",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        type=Path,
        help="Additional workspace directory; repeatable",
    )
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        help="Repository-relative file or directory prefix the executor may change; repeatable",
    )
    parser.add_argument(
        "--track-path",
        action="append",
        default=[],
        help="Repository-relative ignored or generated deliverable to fingerprint; repeatable and implicitly allowed",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="Artifact directory; defaults outside the repository",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Launch a private background job and return without polling it",
    )
    parser.add_argument(
        "--job-dir",
        type=Path,
        help="Detached-job directory; valid only with --detach and created exclusively",
    )
    parser.add_argument(
        "--notify",
        choices=NOTIFICATION_MODES,
        help="Detached completion notification (default: desktop; the event inbox is always written)",
    )
    parser.add_argument(
        "--completion-hook",
        type=Path,
        help="Trusted executable called once with the completion-event path; detached jobs only",
    )
    parser.add_argument(
        "--completion-event",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--expect-changes",
        action="store_true",
        help="Fail when the Git worktree has no net content change",
    )
    parser.add_argument(
        "--retry-read-only",
        type=int,
        default=0,
        metavar="COUNT",
        help=(
            "Retry a transient provider failure at most COUNT times. Valid only for a "
            "read-only run with empty scope (maximum 3)."
        ),
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
        help="Print elapsed-time progress while the executor runs; disabled by default",
    )
    session = parser.add_mutually_exclusive_group()
    session.add_argument("--session", help="Resume a specific executor session ID")
    session.add_argument(
        "--continue",
        dest="continue_last",
        action="store_true",
        help="Resume the most recent executor session (discouraged; prefer --session)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print the command without calling the model",
    )
    return parser.parse_args(argv)


def parse_models_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} models",
        description="Retrieve normalized live model catalogs without invoking a model.",
    )
    parser.add_argument(
        "--engine",
        action="append",
        choices=CATALOG_ENGINES,
        help="Catalog to query; repeatable (default: every platform)",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include provider-hidden/internal models in table output",
    )
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Case-insensitive model-ID substring filter; repeatable",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ask providers that support it to refresh their local catalog",
    )
    return parser.parse_args(argv)


def parse_events_args(argv: list[str] | None = None) -> argparse.Namespace:
    arguments = list(argv) if argv is not None else None
    action = (
        arguments.pop(0)
        if arguments and arguments[0] in ("wait", "follow")
        else "list"
    )
    if action in ("wait", "follow"):
        parser = argparse.ArgumentParser(
            prog=f"{Path(sys.argv[0]).name} events {action}",
            description=(
                "Block in this process until the requested detached-job completion "
                "event appears. No model calls are made while waiting."
            ),
        )
        parser.add_argument(
            "event",
            nargs="+" if action == "follow" else None,
            metavar="EVENT_ID_OR_PATH",
            help=(
                "Exact event ID or AGENT_EVENT path"
                + ("; repeatable" if action == "follow" else "")
            ),
        )
        parser.add_argument(
            "--cwd",
            type=Path,
            help="Require every event to belong to this Git repository root",
        )
        parser.add_argument(
            "--timeout",
            default=DEFAULT_EVENT_WAIT_TIMEOUT,
            help=f"Maximum blocking duration (default: {DEFAULT_EVENT_WAIT_TIMEOUT})",
        )
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=DEFAULT_EVENT_POLL_SECONDS,
            help=(
                "Internal filesystem check interval; this never invokes a model "
                f"(default: {DEFAULT_EVENT_POLL_SECONDS})"
            ),
        )
        parser.add_argument(
            "--format",
            choices=("json", "table"),
            default="json",
            help=(
                "Compact signal output format (default: json"
                + (
                    "; one JSON object is emitted per completed event)"
                    if action == "follow"
                    else ")"
                )
            ),
        )
        parsed = parser.parse_args(arguments)
        parsed.action = action
        return parsed

    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} events",
        description="List or acknowledge durable detached-job completion events.",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        help="Only include events for this repository root",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include previously acknowledged events",
    )
    acknowledgement = parser.add_mutually_exclusive_group()
    acknowledgement.add_argument(
        "--ack",
        metavar="EVENT_ID",
        help="Acknowledge one exact event ID and exit",
    )
    acknowledgement.add_argument(
        "--ack-all",
        action="store_true",
        help="Acknowledge every matching unread event and exit",
    )
    parsed = parser.parse_args(arguments)
    parsed.action = action
    return parsed


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_duration(value: str, *, option: str = "--timeout") -> float:
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    position = 0
    seconds = 0.0
    for match in re.finditer(r"(\d+(?:\.\d+)?)(d|h|m|s)", value):
        if match.start() != position:
            raise RunnerError(f"invalid {option}: {value!r}")
        seconds += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(value) or seconds <= 0:
        raise RunnerError(f"invalid {option}: {value!r}")
    return seconds


def deadline_remaining(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        deadline = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError("timezone required")
        return (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
    except ValueError as exc:
        raise RunnerError("--deadline must be an ISO-8601 timestamp with timezone", 4) from exc


def run_checked(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 30,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(
            f"command timed out during preflight: {command[0]}", 12
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RunnerError(
            f"preflight command failed ({result.returncode}): {' '.join(command)}\n{detail}",
            11,
        )
    return result


def cli_version(executable: str, environment: dict[str, str]) -> str:
    result = run_checked(
        [executable, "--version"], timeout=15, environment=environment
    )
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    return stdout or stderr or "unknown"


def normalized_option_values(
    values: Any, *, keys: tuple[str, ...]
) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        candidate: Any = value
        if isinstance(value, dict):
            candidate = next(
                (value[key] for key in keys if isinstance(value.get(key), str)),
                None,
            )
        if isinstance(candidate, str) and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def normalized_codex_model(item: dict[str, Any]) -> dict[str, Any] | None:
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    reasoning_efforts = normalized_option_values(
        item.get("supportedReasoningEfforts"),
        keys=("reasoningEffort", "effort", "value", "id"),
    )
    service_tiers = normalized_option_values(
        item.get("supportedServiceTiers"),
        keys=("serviceTier", "tier", "value", "id"),
    )
    return {
        "id": model_id,
        "display_name": item.get("displayName") or model_id,
        "description": item.get("description") or "",
        "default": bool(item.get("isDefault", False)),
        "hidden": bool(item.get("hidden", False)),
        "reasoning_efforts": reasoning_efforts,
        "default_reasoning_effort": item.get("defaultReasoningEffort"),
        "service_tiers": service_tiers,
    }


def read_json_rpc_response(
    responses: queue.Queue[str | None],
    request_id: int,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunnerError("Codex model catalog request timed out", 12)
        try:
            line = responses.get(timeout=remaining)
        except queue.Empty as exc:
            raise RunnerError("Codex model catalog request timed out", 12) from exc
        if line is None:
            raise RunnerError("Codex app-server closed before returning its catalog", 11)
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if message.get("error"):
            raise RunnerError(
                f"Codex model catalog failed: {json.dumps(message['error'])}", 11
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise RunnerError("Codex model catalog returned an invalid response", 11)
        return result


def codex_model_records(
    executable: str, environment: dict[str, str]
) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        [executable, "app-server", "--stdio"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        terminate_process(process)
        raise RunnerError("failed to open Codex app-server streams", 11)

    responses: queue.Queue[str | None] = queue.Queue()
    stderr_lines: list[str] = []

    def collect_stdout() -> None:
        try:
            for line in process.stdout:
                responses.put(line)
        finally:
            responses.put(None)

    def collect_stderr() -> None:
        stderr_lines.extend(process.stderr)

    stdout_thread = threading.Thread(target=collect_stdout, daemon=True)
    stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def send(payload: dict[str, Any]) -> None:
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            detail = "".join(stderr_lines).strip()
            suffix = f": {detail}" if detail else ""
            raise RunnerError(f"Codex app-server write failed{suffix}", 11) from exc

    records: list[dict[str, Any]] = []
    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "agent-executor",
                        "version": RUNNER_VERSION,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        read_json_rpc_response(responses, 1, timeout=20)
        send({"method": "initialized"})

        request_id = 2
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100, "includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            send({"id": request_id, "method": "model/list", "params": params})
            page = read_json_rpc_response(responses, request_id, timeout=30)
            data = page.get("data")
            if not isinstance(data, list):
                raise RunnerError("Codex model/list omitted its data array", 11)
            for raw_model in data:
                if not isinstance(raw_model, dict):
                    continue
                if normalized := normalized_codex_model(raw_model):
                    records.append(normalized)
            next_cursor = page.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
            request_id += 1
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            terminate_process(process)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()

    if not records:
        detail = "".join(stderr_lines).strip()
        suffix = f": {detail}" if detail else ""
        raise RunnerError(f"Codex returned an empty model catalog{suffix}", 11)
    return records


def simple_model_records(lines: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines.splitlines():
        model_id, _, display_name = line.strip().partition("\t")
        if not model_id or re.search(r"\s", model_id) or model_id in seen:
            continue
        seen.add(model_id)
        records.append(
            {
                "id": model_id,
                "display_name": display_name.strip() or model_id,
                "description": "",
                "default": False,
                "hidden": False,
                "reasoning_efforts": [],
                "default_reasoning_effort": None,
                "service_tiers": [],
            }
        )
    return records


def discover_live_engine_catalog(
    engine: str,
    *,
    refresh: bool = False,
    executable: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    if engine not in CATALOG_ENGINES:
        raise RunnerError(f"unsupported catalog engine: {engine}", 4)
    discovered_at = utc_now()
    executable = executable or shutil.which(engine)
    if not executable:
        return {
            "engine": engine,
            "status": "not_installed",
            "source": None,
            "executable": None,
            "version": None,
            "discovered_at": discovered_at,
            "models": [],
            "error": f"{engine} is not on PATH",
        }

    environment = executor_environment(engine)
    try:
        version = version or cli_version(executable, environment)
        if engine == "claude":
            # No safe model-list command: offer the tier aliases (never invoke Claude to discover models).
            return {
                "engine": engine,
                "status": "live",
                "source": "claude aliases (Claude Code has no model-list command)",
                "executable": executable,
                "version": version,
                "discovered_at": discovered_at,
                "models": [{"id": alias, "name": f"Claude {alias.title()} (current)", "hidden": False}
                           for alias in CLAUDE_MODEL_ALIASES],
                "error": None,
            }
        if engine == "codex":
            models = codex_model_records(executable, environment)
            source = "codex app-server model/list"
        elif engine == "agy":
            output = run_checked(
                [executable, "models"], timeout=60, environment=environment
            ).stdout.decode("utf-8", errors="replace")
            models = simple_model_records(output)
            source = "agy models"
        else:
            command = [executable, "models", "--pure"]
            if refresh:
                command.append("--refresh")
            output = run_checked(
                command, timeout=120, environment=environment
            ).stdout.decode("utf-8", errors="replace")
            models = simple_model_records(output)
            source = "opencode models --pure" + (" --refresh" if refresh else "")
        if not models:
            raise RunnerError(f"{engine} returned an empty model catalog", 11)
        return {
            "engine": engine,
            "status": "live",
            "source": source,
            "executable": executable,
            "version": version,
            "discovered_at": discovered_at,
            "models": models,
            "error": None,
        }
    except (OSError, RunnerError) as exc:
        return {
            "engine": engine,
            "status": "failed",
            "source": None,
            "executable": executable,
            "version": locals().get("version"),
            "discovered_at": discovered_at,
            "models": [],
            "error": str(exc),
        }


def default_agent_cache_dir() -> Path:
    home = os.environ.get("AGENT_EXECUTOR_HOME")
    if home:
        return Path(home).expanduser()
    configured_root = os.environ.get("XDG_CACHE_HOME")
    cache_root = (
        Path(configured_root).expanduser()
        if configured_root
        else Path.home() / ".cache"
    )
    return cache_root / "agent-executor"


def default_model_cache_path() -> Path:
    return default_agent_cache_dir() / "models-v1.json"


def read_model_cache(path: Path | None = None) -> dict[str, Any]:
    cache_path = path or default_model_cache_path()
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"schema": MODEL_CACHE_SCHEMA, "platforms": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != MODEL_CACHE_SCHEMA
        or not isinstance(payload.get("platforms"), dict)
    ):
        return {"schema": MODEL_CACHE_SCHEMA, "platforms": {}}
    return payload


def write_model_cache(payload: dict[str, Any], path: Path | None = None) -> None:
    cache_path = path or default_model_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_path.parent.chmod(0o700)
    temporary_path = cache_path.parent / f".{cache_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        write_private_text(
            temporary_path,
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        )
        os.replace(temporary_path, cache_path)
        cache_path.chmod(0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def cached_catalog_entry(
    cache: dict[str, Any],
    engine: str,
    *,
    executable: str,
    version: str,
    max_age_seconds: float,
) -> dict[str, Any] | None:
    entry = cache.get("platforms", {}).get(engine)
    if not isinstance(entry, dict):
        return None
    catalog = entry.get("catalog")
    cached_at_epoch = entry.get("cached_at_epoch")
    if isinstance(catalog, dict) and any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str) or re.search(r"\s", item["id"])
        for item in catalog.get("models", [])
    ):
        return None  # Refresh catalogs written by the pre-column-aware parser.
    if (
        not isinstance(catalog, dict)
        or catalog.get("status") != "live"
        or catalog.get("executable") != executable
        or catalog.get("version") != version
        or not isinstance(cached_at_epoch, (int, float))
    ):
        return None
    age_seconds = max(0.0, time.time() - float(cached_at_epoch))
    if age_seconds > max_age_seconds:
        return None
    result = dict(catalog)
    result["cache"] = {
        "status": "hit",
        "path": str(default_model_cache_path()),
        "age_seconds": round(age_seconds, 3),
        "max_age_seconds": max_age_seconds,
    }
    return result


def store_catalog_in_cache(
    engine: str,
    catalog: dict[str, Any],
    *,
    cache_path: Path | None = None,
) -> None:
    if catalog.get("status") != "live":
        return
    path = cache_path or default_model_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        lock_path.chmod(0o600)
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            cache = read_model_cache(path)
            platforms = cache.setdefault("platforms", {})
            platforms[engine] = {
                "cached_at_epoch": time.time(),
                "catalog": catalog,
            }
            cache["updated_at"] = utc_now()
            write_model_cache(cache, path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def discover_engine_catalog(
    engine: str,
    *,
    refresh: bool = False,
    max_age_seconds: float = DEFAULT_CATALOG_MAX_AGE_SECONDS,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    if engine not in CATALOG_ENGINES:
        raise RunnerError(f"unsupported catalog engine: {engine}", 4)
    executable = shutil.which(engine)
    if not executable:
        return discover_live_engine_catalog(engine, refresh=refresh)
    environment = executor_environment(engine)
    try:
        version = cli_version(executable, environment)
    except (OSError, RunnerError):
        return discover_live_engine_catalog(
            engine, refresh=refresh, executable=executable
        )

    path = cache_path or default_model_cache_path()
    if not refresh and engine != "claude":
        cached = cached_catalog_entry(
            read_model_cache(path),
            engine,
            executable=executable,
            version=version,
            max_age_seconds=max_age_seconds,
        )
        if cached is not None:
            cached["cache"]["path"] = str(path)
            return cached

    catalog = discover_live_engine_catalog(
        engine,
        refresh=refresh,
        executable=executable,
        version=version,
    )
    catalog["cache"] = {
        "status": (
            "not_applicable"
            if engine == "claude"
            else ("refreshed" if refresh else "miss")
        ),
        "path": str(path),
        "age_seconds": 0,
        "max_age_seconds": max_age_seconds,
    }
    store_catalog_in_cache(engine, catalog, cache_path=path)
    return catalog


def filtered_catalog(
    catalog: dict[str, Any], *, include_hidden: bool
) -> dict[str, Any]:
    result = dict(catalog)
    result["models"] = [
        model
        for model in catalog.get("models", [])
        if include_hidden or not model.get("hidden", False)
    ]
    return result


def matching_catalog(
    catalog: dict[str, Any], patterns: list[str]
) -> dict[str, Any]:
    if not patterns:
        return catalog
    normalized = [pattern.casefold() for pattern in patterns if pattern.strip()]
    result = dict(catalog)
    result["models"] = [
        model
        for model in catalog.get("models", [])
        if all(pattern in str(model.get("id", "")).casefold() for pattern in normalized)
    ]
    result["match"] = patterns
    if catalog.get("models") and not result["models"]:
        result["match_status"] = "no_matches"
    return result


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def print_model_table(catalogs: list[dict[str, Any]]) -> None:
    print("| Platform | Exact model ID | Default | Hidden | Options / status |")
    print("| --- | --- | --- | --- | --- |")
    for catalog in catalogs:
        models = catalog.get("models", [])
        if not models:
            detail = catalog.get("error") or catalog.get("status")
            print(
                f"| {markdown_cell(catalog['engine'])} | — | — | — | "
                f"{markdown_cell(catalog['status'])}: {markdown_cell(detail)} |"
            )
            continue
        for model in models:
            options: list[str] = []
            if model.get("reasoning_efforts"):
                options.append(
                    "effort=" + ",".join(model["reasoning_efforts"])
                )
            if model.get("service_tiers"):
                options.append("tier=" + ",".join(model["service_tiers"]))
            print(
                f"| {markdown_cell(catalog['engine'])} | "
                f"{markdown_cell(model['id'])} | "
                f"{'yes' if model.get('default') else ''} | "
                f"{'yes' if model.get('hidden') else ''} | "
                f"{markdown_cell('; '.join(options))} |"
            )


def models_main(argv: list[str] | None = None) -> int:
    args = parse_models_args(argv)
    engines = list(dict.fromkeys(args.engine or CATALOG_ENGINES))
    catalogs = [
        matching_catalog(
            filtered_catalog(
                discover_engine_catalog(engine, refresh=args.refresh),
                include_hidden=args.include_hidden,
            ),
            args.match,
        )
        for engine in engines
    ]
    payload = {
        "schema": MODEL_CATALOG_SCHEMA,
        "generated_at": utc_now(),
        "platforms": catalogs,
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=True))
    else:
        print_model_table(catalogs)
    return 0


def git_output(repo: Path, *args: str) -> bytes:
    return run_checked(["git", *args], cwd=repo).stdout


def git_root(cwd: Path) -> Path:
    try:
        return Path(
            git_output(cwd, "rev-parse", "--show-toplevel").decode().strip()
        ).resolve()
    except RunnerError as exc:
        raise RunnerError(f"target is not a Git repository: {cwd}", 4) from exc


def nul_paths(blob: bytes) -> set[bytes]:
    return {item for item in blob.split(b"\0") if item}


def hash_path(path: Path, digest: Any, *, relative: str = ".") -> None:
    """Hash content, type, mode, names, and symlink targets without following symlinks."""
    try:
        metadata = path.lstat()
        digest.update(f"{relative}\0MODE:{metadata.st_mode:o}\0".encode())
        if path.is_symlink():
            digest.update(b"SYMLINK\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            return
        if path.is_file():
            digest.update(b"FILE\0")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            return
        if path.is_dir():
            digest.update(b"DIRECTORY\0")
            for child in sorted(
                path.iterdir(), key=lambda item: os.fsencode(item.name)
            ):
                child_relative = (
                    child.name if relative == "." else f"{relative}/{child.name}"
                )
                hash_path(child, digest, relative=child_relative)
            return
        digest.update(b"OTHER\0")
    except OSError as exc:
        digest.update(f"{relative}\0UNREADABLE:{exc}".encode("utf-8", errors="replace"))


def path_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"kind": "missing", "sha256": hashlib.sha256(b"MISSING").hexdigest()}
    digest = hashlib.sha256()
    hash_path(path, digest)
    try:
        if path.is_symlink():
            kind = "symlink"
        elif path.is_file():
            kind = "file"
        elif path.is_dir():
            kind = "directory"
        else:
            kind = "other"
    except OSError:
        kind = "unreadable"
    return {"kind": kind, "sha256": digest.hexdigest()}


def normalize_repo_paths(values: list[str], *, option: str) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = Path(value)
        if (
            candidate.is_absolute()
            or value.strip() in {"", "."}
            or ".." in candidate.parts
            or (candidate.parts and candidate.parts[0] == ".git")
        ):
            raise RunnerError(
                f"{option} must be a non-root repository-relative path: {value!r}", 4
            )
        clean = candidate.as_posix().rstrip("/")
        if clean not in normalized:
            normalized.append(clean)
    return normalized


def index_fingerprints(repo: Path) -> dict[str, str]:
    entries: dict[str, list[bytes]] = {}
    for record in nul_paths(git_output(repo, "ls-files", "-s", "-z")):
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            continue
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        entries.setdefault(relative, []).append(metadata)
    return {
        relative: hashlib.sha256(b"\0".join(metadata)).hexdigest()
        for relative, metadata in entries.items()
    }


def git_path_fingerprint(
    repo: Path, relative: str, index_entries: dict[str, str]
) -> dict[str, Any]:
    state = path_fingerprint(repo / relative)
    state["index_sha256"] = index_entries.get(relative, hashlib.sha256(b"").hexdigest())
    return state


def git_state(repo: Path, tracked_paths: list[str] | None = None) -> dict[str, Any]:
    status_bytes = git_output(
        repo, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    )
    dirty_paths = set()
    dirty_paths |= nul_paths(
        git_output(repo, "diff", "--name-only", "--no-renames", "-z")
    )
    dirty_paths |= nul_paths(
        git_output(repo, "diff", "--cached", "--name-only", "--no-renames", "-z")
    )
    dirty_paths |= nul_paths(
        git_output(repo, "ls-files", "--others", "--exclude-standard", "-z")
    )

    index_entries = index_fingerprints(repo)
    path_fingerprints = {}
    for raw_path in sorted(dirty_paths):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        path_fingerprints[relative] = git_path_fingerprint(
            repo, relative, index_entries
        )

    tracked_fingerprints = {
        relative: path_fingerprint(repo / relative)
        for relative in (tracked_paths or [])
    }
    digest = hashlib.sha256()
    digest.update(status_bytes)
    digest.update(
        json.dumps(
            {"paths": path_fingerprints, "tracked": tracked_fingerprints},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8", errors="surrogateescape")
    )

    status_text = git_output(repo, "status", "--short", "--untracked-files=all").decode(
        "utf-8", errors="replace"
    )
    return {
        "fingerprint": digest.hexdigest(),
        "status": [line for line in status_text.splitlines() if line],
        "path_fingerprints": path_fingerprints,
        "tracked_path_fingerprints": tracked_fingerprints,
    }


def state_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    before_paths = before.get("path_fingerprints", {})
    after_paths = after.get("path_fingerprints", {})
    changed_paths = sorted(
        path
        for path in set(before_paths) | set(after_paths)
        if before_paths.get(path) != after_paths.get(path)
    )
    before_tracked = before.get("tracked_path_fingerprints", {})
    after_tracked = after.get("tracked_path_fingerprints", {})
    tracked_paths_changed = sorted(
        path
        for path in set(before_tracked) | set(after_tracked)
        if before_tracked.get(path) != after_tracked.get(path)
    )
    return {
        "changed_paths": sorted(set(changed_paths) | set(tracked_paths_changed)),
        "tracked_paths_changed": tracked_paths_changed,
    }


REVIEW_FLAG_PATTERN = re.compile(
    r"\b(?:TODO|FIXME|HACK)\b|@ts-ignore|eslint-disable|console\.(?:log|warn|error)\("
)


def file_line_delta(
    repo: Path,
    path: str,
    *,
    preexisting_dirty: bool,
) -> dict[str, Any]:
    if preexisting_dirty:
        return {
            "path": path,
            "added_lines": None,
            "removed_lines": None,
            "exact_for_run": False,
            "reason": "path_was_dirty_before_executor",
            "review_flags": [],
        }

    completed = run_checked(
        ["git", "diff", "--numstat", "HEAD", "--", path],
        cwd=repo,
    )
    line = completed.stdout.decode("utf-8", errors="replace").strip()
    added: int | None = None
    removed: int | None = None
    if line:
        fields = line.split("\t", 2)
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            added, removed = int(fields[0]), int(fields[1])
    else:
        target = repo / path
        if target.is_file():
            try:
                added = len(target.read_text(encoding="utf-8").splitlines())
                removed = 0
            except (OSError, UnicodeDecodeError):
                pass

    patch = run_checked(
        ["git", "diff", "--unified=0", "HEAD", "--", path],
        cwd=repo,
    ).stdout.decode("utf-8", errors="replace")
    review_flags = sorted(
        {
            match.group(0)
            for raw_line in patch.splitlines()
            if raw_line.startswith("+") and not raw_line.startswith("+++")
            for match in REVIEW_FLAG_PATTERN.finditer(raw_line[1:])
        }
    )
    return {
        "path": path,
        "added_lines": added,
        "removed_lines": removed,
        "exact_for_run": added is not None and removed is not None,
        "reason": None if added is not None and removed is not None else "binary_or_unavailable",
        "review_flags": review_flags,
    }


def compact_review_bundle(
    *,
    repo: Path,
    before: dict[str, Any],
    run_delta: dict[str, list[str]],
    verification: dict[str, Any],
    result_path: Path,
    session_id: str | None,
    scope_violations: list[str],
    history_violations: list[str],
) -> dict[str, Any]:
    dirty_before = set(before.get("path_fingerprints", {}))
    files = [
        file_line_delta(
            repo,
            path,
            preexisting_dirty=path in dirty_before,
        )
        for path in run_delta["changed_paths"]
    ]
    return {
        "result_path": str(result_path),
        "session_id": session_id,
        "changed_file_count": len(files),
        "files": files,
        "verification": {
            "status": verification.get("status"),
            "command_count": verification.get("command_count", 0),
            "completed_count": verification.get("completed_count", 0),
            "commands": [
                {
                    "index": command.get("index"),
                    "status": command.get("status"),
                    "exit_code": command.get("exit_code"),
                    "duration_seconds": command.get("duration_seconds"),
                }
                for command in verification.get("commands", [])
            ],
        },
        "scope_violations": scope_violations,
        "history_violations": history_violations,
        "review_flags": sorted(
            {
                flag
                for file in files
                for flag in file.get("review_flags", [])
            }
        ),
    }


def path_is_allowed(path: str, prefixes: list[str]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def find_scope_violations(
    delta: dict[str, list[str]], allowed_paths: list[str]
) -> list[str]:
    if not allowed_paths:
        return list(delta["changed_paths"])
    return [
        path
        for path in delta["changed_paths"]
        if not path_is_allowed(path, allowed_paths)
    ]


def read_brief(source: str) -> str:
    if source == "-":
        text = sys.stdin.read()
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise RunnerError(f"brief file not found: {path}")
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise RunnerError("brief is empty")
    size = len(text.encode("utf-8"))
    if size > 120 * 1024:
        raise RunnerError(
            f"brief is {size // 1024} KiB; keep it below 120 KiB and point the executor to workspace files for bulky context"
        )
    return text.rstrip()


def brief_renderer_module() -> Any:
    """Load the colocated renderer without depending on the caller's sys.path."""
    return bundled_module("render_brief")


def bundled_module(name: str) -> Any:
    renderer_path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(
        "agent_executor_" + name, renderer_path
    )
    if spec is None or spec.loader is None:
        raise RunnerError("could not load the bundled task-spec renderer", 4)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_task_spec(source: str, *, repo: Path) -> tuple[str, dict[str, Any]]:
    """Render and validate a task spec whose root is exactly the execution repo."""
    renderer = brief_renderer_module()
    try:
        values = renderer.validate_spec(renderer.read_json(source))
        declared_root = Path(values["repository_root"]).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise RunnerError(f"task spec is invalid: {exc}", 4) from exc
    if declared_root != repo:
        raise RunnerError(
            "--task-spec repository_root must exactly match --cwd: "
            f"{declared_root} != {repo}",
            4,
        )
    try:
        brief = renderer.render_brief(values)
    except ValueError as exc:
        raise RunnerError(f"task spec cannot render a brief: {exc}", 4) from exc
    return brief.rstrip(), values


def resolve_execution_brief(
    args: argparse.Namespace, *, repo: Path
) -> tuple[str, dict[str, Any] | None]:
    """Return the standalone brief and, for specs, the normalized authoritative values."""
    if args.task_spec:
        if args.allow_path or args.track_path:
            raise RunnerError(
                "--task-spec derives scope; do not also pass --allow-path or --track-path",
                4,
            )
        brief, task_spec = read_task_spec(args.task_spec, repo=repo)
    else:
        brief, task_spec = read_brief(args.brief), None
    validate_brief_contract(brief)
    return brief, task_spec


def brief_report_contract_violations(brief: str) -> list[str]:
    final_section = re.search(r"(?im)^#{1,6}\s+Final response\s*$", brief)
    if final_section is None:
        return []

    section = brief[final_section.end() :]
    positions = {heading: section.upper().find(heading) for heading in REPORT_HEADINGS}
    violations = [
        f"brief_missing_heading:{heading}"
        for heading, position in positions.items()
        if position < 0
    ]
    if not violations:
        ordered = [positions[heading] for heading in REPORT_HEADINGS]
        if ordered != sorted(ordered):
            violations.append("brief_heading_order")
    return violations


def validate_brief_contract(brief: str) -> None:
    violations = brief_report_contract_violations(brief)
    if violations:
        raise RunnerError(
            "brief final-response contract is malformed: " + ", ".join(violations),
            4,
        )


def prepare_out_dir(requested: Path | None, repo: Path) -> Path:
    resolved_repo = repo.resolve()
    if requested:
        out_dir = requested.expanduser().resolve()
        if out_dir == resolved_repo or resolved_repo in out_dir.parents:
            raise RunnerError("--out-dir must be outside the target repository", 4)
        out_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        out_dir.chmod(0o700)
        return out_dir
    # Under the cache (not $TMPDIR, which macOS cleans) so --resume-result paths stay valid; pruned by age.
    base = default_agent_cache_dir().resolve() / "runs-v1"
    if base == resolved_repo or resolved_repo in base.parents:
        raise RunnerError("the agent-executor cache must be outside the target repository", 4)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = base / f"{repo.name}-{stamp}-{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(mode=0o700)
    out_dir.chmod(0o700)
    return out_dir


def prepare_job_dir(requested: Path | None, repo: Path) -> Path:
    resolved_repo = repo.resolve()
    if requested:
        job_dir = requested.expanduser().resolve()
        if job_dir == resolved_repo or resolved_repo in job_dir.parents:
            raise RunnerError("--job-dir must be outside the target repository", 4)
        job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        job_dir.chmod(0o700)
        return job_dir
    base = default_agent_cache_dir().resolve() / "jobs-v1"
    if base == resolved_repo or resolved_repo in base.parents:
        raise RunnerError(
            "the agent-executor cache must be outside the target repository", 4
        )
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    job_dir = base / f"{repo.name}-{stamp}-{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(mode=0o700)
    job_dir.chmod(0o700)
    return job_dir


def command_preview(command: list[str]) -> list[str]:
    return [
        "--print=<brief omitted>" if item.startswith("--print=") else item
        for item in command
    ]


def write_private_text(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        path.chmod(0o600)


def open_private_binary(path: Path) -> Any:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    path.chmod(0o600)
    return os.fdopen(descriptor, "wb")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    # Escaping non-ASCII data also makes paths decoded with surrogateescape safe to
    # serialize, while keeping the result portable across JSON consumers.
    write_private_text(path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        write_json(temporary, payload)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def default_completion_event_dir() -> Path:
    return default_agent_cache_dir() / "completions-v1"


def prepare_completion_event_path() -> Path:
    event_dir = default_completion_event_dir().resolve()
    event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    event_dir.chmod(0o700)
    return event_dir / f"{uuid.uuid4().hex}.json"


def normalize_completion_event_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_parent = default_completion_event_dir().resolve()
    if resolved.parent != expected_parent:
        raise RunnerError(
            f"internal completion event must be inside {expected_parent}", 4
        )
    expected_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected_parent.chmod(0o700)
    return resolved


def completion_event_path_from_reference(reference: str) -> Path:
    value = reference.strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return normalize_completion_event_path(
            default_completion_event_dir() / f"{value.lower()}.json"
        )

    candidate = Path(value).expanduser()
    if candidate.suffix != ".json" or not re.fullmatch(
        r"[0-9a-fA-F]{32}", candidate.stem
    ):
        raise RunnerError(
            "completion event must be an exact 32-character event ID or "
            "AGENT_EVENT .json path",
            4,
        )
    return normalize_completion_event_path(candidate)


def read_completion_event(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunnerError(f"could not read completion event {path}: {exc}", 4) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"invalid completion event {path}: {exc}", 4) from exc
    if not isinstance(payload, dict) or payload.get("schema") != COMPLETION_EVENT_SCHEMA:
        raise RunnerError(f"unsupported completion event schema: {path}", 4)
    if payload.get("event_id") != path.stem:
        raise RunnerError(f"completion event ID does not match its path: {path}", 4)
    return {**payload, "event_path": str(path)}


def resolve_completion_hook(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RunnerError(
            f"--completion-hook must be an executable file: {resolved}", 4
        )
    return resolved


def ensure_hook_outside_workspaces(
    hook: Path | None, workspace_roots: list[Path]
) -> None:
    if hook is None:
        return
    for root in workspace_roots:
        resolved_root = root.resolve()
        if hook == resolved_root or resolved_root in hook.parents:
            raise RunnerError(
                "--completion-hook must be outside every executor workspace", 4
            )


def send_desktop_notification(event: dict[str, Any]) -> dict[str, Any]:
    status = str(event.get("status") or "finished")
    engine = str(event.get("engine") or "agent")
    model = str(event.get("model") or "").strip()
    repository = Path(str(event.get("repository") or "workspace")).name
    title = f"Agent executor: {status.replace('_', ' ')}"
    details = " · ".join(item for item in (engine, model, repository) if item)

    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        if not executable:
            return {"kind": "desktop", "status": "unsupported"}
        command = [
            executable,
            "-e",
            (
                "on run argv\n"
                "display notification (item 2 of argv) with title (item 1 of argv)\n"
                "end run"
            ),
            title,
            details,
        ]
        provider = "macos"
    elif sys.platform.startswith("linux"):
        executable = shutil.which("notify-send")
        if not executable:
            return {"kind": "desktop", "status": "unsupported"}
        command = [executable, title, details]
        provider = "freedesktop"
    else:
        return {"kind": "desktop", "status": "unsupported"}

    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "kind": "desktop",
            "status": "failed",
            "provider": provider,
            "error": str(exc),
        }
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        return {
            "kind": "desktop",
            "status": "failed",
            "provider": provider,
            "exit_code": completed.returncode,
            "error": error[-500:],
        }
    return {"kind": "desktop", "status": "sent", "provider": provider}


def run_completion_hook(
    hook: Path, event_path: Path, event: dict[str, Any]
) -> dict[str, Any]:
    environment = {
        key: value
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TMPDIR")
        if (value := os.environ.get(key)) is not None
    }
    environment.update(
        {
            "AGENT_EVENT": str(event_path),
            "AGENT_RESULT": str(event.get("result_path") or ""),
            "AGENT_STATUS": str(event.get("status") or ""),
            "AGENT_OUTCOME": str(event.get("task_outcome") or ""),
            "AGENT_VERIFICATION_STATUS": str(
                event.get("verification_status") or ""
            ),
            "AGENT_ENGINE": str(event.get("engine") or ""),
            "AGENT_MODEL": str(event.get("model") or ""),
        }
    )
    try:
        completed = subprocess.run(
            [str(hook), str(event_path)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "kind": "hook",
            "status": "failed",
            "path": str(hook),
            "error": str(exc),
        }
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        return {
            "kind": "hook",
            "status": "failed",
            "path": str(hook),
            "exit_code": completed.returncode,
            "error": error[-500:],
        }
    return {"kind": "hook", "status": "sent", "path": str(hook)}


def publish_completion_event(
    *,
    result: dict[str, Any],
    result_path: Path,
    event_path: Path,
    notification_mode: str,
    completion_hook: Path | None,
) -> dict[str, Any]:
    repository_value = result.get("repository")
    repository = (
        str(Path(str(repository_value)).expanduser().resolve())
        if repository_value
        else None
    )
    event = {
        "schema": COMPLETION_EVENT_SCHEMA,
        "event_id": event_path.stem,
        "created_at": utc_now(),
        "acknowledged_at": None,
        "status": result.get("status"),
        "task_outcome": result.get("task_outcome"),
        "reported_outcome": result.get("reported_outcome"),
        "verification_status": (result.get("verification") or {}).get("status"),
        "exit_code": result.get("exit_code"),
        "engine": result.get("engine"),
        "model": result.get("model"),
        "variant": result.get("variant"),
        "repository": repository,
        "result_path": str(result_path.expanduser().resolve()),
        "error": result.get("error"),
        "scope_violations": result.get("scope_violations", []),
        "history_violations": result.get("history_violations", []),
        "deliveries": [],
    }
    write_json_atomic(event_path, event)

    deliveries: list[dict[str, Any]] = []
    if notification_mode == "desktop":
        deliveries.append(send_desktop_notification(event))
    if completion_hook is not None:
        deliveries.append(run_completion_hook(completion_hook, event_path, event))

    # Preserve a very fast acknowledgement if a consumer races the notifier.
    try:
        current = json.loads(event_path.read_text(encoding="utf-8"))
        if isinstance(current, dict):
            event = current
    except (OSError, json.JSONDecodeError):
        pass
    event["deliveries"] = deliveries
    write_json_atomic(event_path, event)
    return event


def completion_events(
    *,
    repository: Path | None,
    include_acknowledged: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    event_dir = default_completion_event_dir()
    if not event_dir.exists():
        return [], []
    repository_text = str(repository.resolve()) if repository else None
    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(event_dir.glob("*.json")):
        try:
            payload = read_completion_event(path)
        except RunnerError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        if payload is None:
            continue
        if repository_text:
            payload_repository = payload.get("repository")
            if not payload_repository:
                continue
            if str(Path(str(payload_repository)).expanduser().resolve()) != repository_text:
                continue
        if not include_acknowledged and payload.get("acknowledged_at"):
            continue
        events.append(payload)
    events.sort(key=lambda item: str(item.get("created_at") or ""))
    return events, errors


def completion_signal(event: dict[str, Any]) -> dict[str, Any]:
    error = str(event.get("error") or "")
    if len(error) > MAX_COMPLETION_SIGNAL_ERROR_CHARS:
        error = error[:MAX_COMPLETION_SIGNAL_ERROR_CHARS] + "... [truncated]"
    return {
        "schema": COMPLETION_SIGNAL_SCHEMA,
        "event_id": event.get("event_id"),
        "created_at": event.get("created_at"),
        "acknowledged_at": event.get("acknowledged_at"),
        "status": event.get("status"),
        "task_outcome": event.get("task_outcome"),
        "reported_outcome": event.get("reported_outcome"),
        "verification_status": event.get("verification_status"),
        "exit_code": event.get("exit_code"),
        "engine": event.get("engine"),
        "model": event.get("model"),
        "variant": event.get("variant"),
        "repository": event.get("repository"),
        "result_path": event.get("result_path"),
        "event_path": event.get("event_path"),
        "error": error or None,
        "scope_violation_count": len(event.get("scope_violations") or []),
        "history_violation_count": len(event.get("history_violations") or []),
    }


def print_completion_signal(event: dict[str, Any], *, output_format: str) -> None:
    signal_payload = completion_signal(event)
    if output_format == "json":
        print(
            json.dumps(
                signal_payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return

    print(f"AGENT_EVENT={signal_payload['event_path']}", flush=True)
    print(f"AGENT_EVENT_ID={signal_payload['event_id']}", flush=True)
    print(f"AGENT_STATUS={signal_payload['status'] or ''}", flush=True)
    print(f"AGENT_OUTCOME={signal_payload['task_outcome'] or ''}", flush=True)
    print(
        f"AGENT_VERIFICATION_STATUS="
        f"{signal_payload['verification_status'] or ''}",
        flush=True,
    )
    print(f"AGENT_EXIT_CODE={signal_payload['exit_code']}", flush=True)
    print(f"AGENT_ENGINE={signal_payload['engine'] or ''}", flush=True)
    print(f"AGENT_MODEL={signal_payload['model'] or ''}", flush=True)
    print(f"AGENT_RESULT={signal_payload['result_path'] or ''}", flush=True)


def wait_for_completion_events(
    event_paths: list[Path],
    *,
    repository: Path | None,
    timeout_seconds: float,
    poll_seconds: float,
) -> Iterator[dict[str, Any]]:
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise RunnerError("--poll-seconds must be a finite number greater than 0", 4)

    pending = dict.fromkeys(event_paths)
    deadline = time.monotonic() + timeout_seconds
    repository_text = str(repository.resolve()) if repository else None
    while pending:
        for path in list(pending):
            event = read_completion_event(path)
            if event is None:
                continue
            if repository_text:
                event_repository = event.get("repository")
                normalized_repository = (
                    str(Path(str(event_repository)).expanduser().resolve())
                    if event_repository
                    else None
                )
                if normalized_repository != repository_text:
                    raise RunnerError(
                        f"completion event {event['event_id']} does not belong to "
                        f"{repository_text}",
                        4,
                    )
            del pending[path]
            yield event

        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pending_ids = ", ".join(path.stem for path in pending)
            raise RunnerError(
                f"timed out waiting for completion event(s): {pending_ids}",
                12,
            )
        time.sleep(min(poll_seconds, remaining))


def acknowledge_completion_event(event: dict[str, Any]) -> None:
    event_path = Path(str(event["event_path"]))
    payload = {key: value for key, value in event.items() if key != "event_path"}
    payload["acknowledged_at"] = utc_now()
    write_json_atomic(event_path, payload)


def events_main(argv: list[str] | None = None) -> int:
    args = parse_events_args(argv)
    repository: Path | None = None
    if args.cwd:
        cwd = args.cwd.expanduser().resolve()
        if not cwd.is_dir():
            raise RunnerError(f"target directory does not exist: {cwd}", 4)
        repository = git_root(cwd)
        if cwd != repository:
            raise RunnerError(f"--cwd must be the Git repository root: {repository}", 4)

    if args.action in ("wait", "follow"):
        references = args.event if isinstance(args.event, list) else [args.event]
        event_paths = list(
            dict.fromkeys(
                completion_event_path_from_reference(reference)
                for reference in references
            )
        )
        timeout_seconds = parse_duration(args.timeout)
        for event in wait_for_completion_events(
            event_paths,
            repository=repository,
            timeout_seconds=timeout_seconds,
            poll_seconds=args.poll_seconds,
        ):
            print_completion_signal(event, output_format=args.format)
        return 0

    events, errors = completion_events(
        repository=repository,
        include_acknowledged=args.all or bool(args.ack),
    )
    if args.ack:
        matches = [event for event in events if event.get("event_id") == args.ack]
        if not matches:
            raise RunnerError(f"completion event not found: {args.ack}", 4)
        acknowledge_completion_event(matches[0])
        print(f"AGENT_EVENT_ACKNOWLEDGED={args.ack}")
        return 0
    if args.ack_all:
        for event in events:
            acknowledge_completion_event(event)
        print(f"AGENT_EVENTS_ACKNOWLEDGED={len(events)}")
        return 0

    if args.format == "json":
        print(
            json.dumps(
                {
                    "schema": COMPLETION_LIST_SCHEMA,
                    "events": events,
                    "errors": errors,
                },
                indent=2,
                ensure_ascii=True,
            )
        )
        return 0

    print(f"AGENT_EVENT_COUNT={len(events)}")
    for event in events:
        print(f"AGENT_EVENT={event['event_path']}")
        print(f"AGENT_EVENT_ID={event['event_id']}")
        print(f"AGENT_STATUS={event.get('status') or ''}")
        print(f"AGENT_OUTCOME={event.get('task_outcome') or ''}")
        print(
            f"AGENT_VERIFICATION_STATUS="
            f"{event.get('verification_status') or ''}"
        )
        print(f"AGENT_ENGINE={event.get('engine') or ''}")
        print(f"AGENT_MODEL={event.get('model') or ''}")
        print(f"AGENT_RESULT={event.get('result_path') or ''}")
    if errors:
        print(f"AGENT_EVENT_ERRORS={json.dumps(errors, ensure_ascii=True)}")
    return 0


def bound_final_message(text: str) -> str:
    if len(text) <= MAX_FINAL_MESSAGE_CHARS:
        return text
    marker = "\n... [truncated; complete report is in the raw artifacts] ...\n"
    available = MAX_FINAL_MESSAGE_CHARS - len(marker)
    head_size = available // 2
    tail_size = available - head_size
    return f"{text[:head_size]}{marker}{text[-tail_size:]}"


def extract_final_report(stdout: str) -> tuple[str, bool]:
    report, extracted = extract_raw_final_report(stdout)
    return bound_final_message(report), extracted


def extract_raw_final_report(stdout: str) -> tuple[str, bool]:
    matches = list(
        re.finditer(r"^(?:#{1,6}\s+)?(?:\*\*|__)?STATUS(?:\*\*|__)?:?\s*$", stdout, re.MULTILINE | re.IGNORECASE)
    )
    if not matches:
        return bound_final_message(stdout.strip()[-MAX_FINAL_MESSAGE_CHARS:]), False
    report = stdout[matches[-1].start() :].strip()
    return report, True


def parse_reported_outcome(report: str, report_extracted: bool) -> str | None:
    if not report_extracted:
        return None
    lines = report.splitlines()
    status_heading = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(
                r"(?:#{1,6}\s+)?(?:\*\*|__)?STATUS(?:\*\*|__)?:?\s*",
                line,
                re.IGNORECASE,
            )
        ),
        None,
    )
    if status_heading is None:
        return "unknown"
    status_line = next(
        (
            line.strip()
            for line in lines[status_heading + 1 :]
            if line.strip()
        ),
        "",
    )
    # Models often format the token: `COMPLETE`, **COMPLETE**, COMPLETE. all mean COMPLETE.
    status_line = re.sub(r"^[-*]\s+", "", status_line)
    status_line = re.sub(r"^[`*_]+([A-Za-z]+)[`*_]+", r"\1", status_line)
    status_line = re.sub(r"^([A-Za-z]+)\.$", r"\1", status_line.strip())
    match = re.fullmatch(
        (
            r"(?:[-*]\s*)?"
            r"(COMPLETE|COMPLETED|SUCCESS|SUCCEEDED|PASSED|"
            r"BLOCKED|BLOCKER|FAILED|FAILURE)"
            r"(\s*(?:[-:—]|\().*)?"
        ),
        status_line,
        re.IGNORECASE,
    )
    if not match:
        return "unknown"
    token = match.group(1).lower()
    if token in {"complete", "completed", "success", "succeeded", "passed"}:
        return "unknown" if match.group(2) else "complete"
    if token in {"blocked", "blocker"}:
        return "blocked"
    return "failed"


def report_contract_violations(
    report: str,
    report_extracted: bool,
) -> list[str]:
    if not report_extracted:
        return ["missing_heading:STATUS"]

    heading_pattern = re.compile(
        (
            r"(?:#{1,6}\s+)?(?:\*\*|__)?"
            r"(STATUS|FILES CHANGED|COMMANDS RUN|VERIFICATION|RISKS OR BLOCKERS)"
            r"(?:\*\*|__)?:?\s*"
        ),
        re.IGNORECASE,
    )
    positions: dict[str, list[int]] = {heading: [] for heading in REPORT_HEADINGS}
    for index, line in enumerate(report.splitlines()):
        if match := heading_pattern.fullmatch(line):
            positions[match.group(1).upper()].append(index)

    violations = [
        f"missing_heading:{heading}"
        for heading in REPORT_HEADINGS
        if not positions[heading]
    ]
    violations.extend(
        f"duplicate_heading:{heading}"
        for heading in REPORT_HEADINGS
        if len(positions[heading]) > 1
    )
    if not violations:
        ordered_positions = [positions[heading][0] for heading in REPORT_HEADINGS]
        if ordered_positions != sorted(ordered_positions):
            violations.append("heading_order")
    return violations


def task_outcome_for_status(status: str) -> str:
    if status == "completed":
        return "complete"
    if status == "blocked":
        return "blocked"
    if status == "dry_run":
        return "not_run"
    return "failed"


def print_summary(result: dict[str, Any], result_path: Path) -> None:
    print(f"AGENT_STATUS={result['status']}")
    print(f"AGENT_OUTCOME={result.get('task_outcome', '')}")
    print(
        f"AGENT_VERIFICATION_STATUS="
        f"{(result.get('verification') or {}).get('status', '')}"
    )
    print(f"AGENT_ENGINE={result.get('engine', '')}")
    print(f"AGENT_MODEL={result.get('model', '')}")
    print(
        f"AGENT_FULL_PERMISSIONS={str(result.get('full_permissions', False)).lower()}"
    )
    if result.get("session_id"):
        print(f"AGENT_SESSION={result['session_id']}")
    if result.get("scope_violations"):
        print(f"AGENT_SCOPE_VIOLATIONS={json.dumps(result['scope_violations'])}")
    if result.get("history_violations"):
        print(f"AGENT_HISTORY_VIOLATIONS={json.dumps(result['history_violations'])}")
    if result.get("event_errors"):
        print(f"AGENT_EVENT_ERRORS={json.dumps(result['event_errors'])}")
    review = result.get("review") or {}
    if review:
        print(
            "AGENT_REVIEW="
            + json.dumps(
                {
                    "changed_file_count": review.get("changed_file_count", 0),
                    "files": review.get("files", []),
                    "verification": review.get("verification", {}),
                    "review_flags": review.get("review_flags", []),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    print(f"AGENT_RESULT={result_path}")
    final_message = result.get("final_message", "").strip()
    if final_message:
        print("--- AGENT FINAL REPORT ---")
        print(final_message)
        print("--- END AGENT REPORT ---")


def emit_lifecycle(stage: str, **fields: Any) -> None:
    details = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=True)}"
        for key, value in fields.items()
        if value is not None
    )
    print(
        f"AGENT_LIFECYCLE={stage}" + (f" {details}" if details else ""),
        flush=True,
    )


def parse_agy_session_id(log_path: Path) -> str | None:
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for pattern in (
        r"Print mode: conversation=([0-9a-f-]+)",
        r"Created conversation ([0-9a-f-]+)",
    ):
        if match := re.search(pattern, text, re.IGNORECASE):
            return match.group(1)
    return None


def parse_jsonl_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def parse_agy_output(text: str) -> tuple[str, str | None, str | None]:
    events = bundled_module("usage").agy_events(text)
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        # Retain compatibility with older plain-output adapters. Malformed JSON
        # without a terminal report still fails the ordinary report contract.
        return text, None, None
    final = results[-1]
    response = final.get("response")
    status = final.get("status")
    return response if isinstance(response, str) else "", final.get("conversation_id"), status.strip().lower() if isinstance(status, str) and status.strip() else "unknown"


def agy_terminal_error(text: str) -> str | None:
    results = [event for event in bundled_module("usage").agy_events(text) if event.get("type") == "result"]
    error = results[-1].get("error") if results else None
    return error.strip() if isinstance(error, str) and error.strip() else None


def parse_claude_output(text: str) -> tuple[str, str | None, str, str | None]:
    """Claude Code `-p --output-format json`: (report, session id, "success"|"error", error text)."""
    final: dict[str, Any] | None = None
    for event in parse_jsonl_events(text):
        if isinstance(event, dict) and event.get("type") == "result":
            final = event
    if final is None:
        return "", None, "unknown", None
    result = final.get("result") if isinstance(final.get("result"), str) else ""
    if final.get("is_error"):
        return "", final.get("session_id"), "error", result or final.get("subtype") or "error"
    return result, final.get("session_id"), "success", None


def provider_quota_exhausted(*evidence: str | None) -> bool:
    """True when a provider-side failure names quota, credits or rate limits (e.g. agy 429 RESOURCE_EXHAUSTED)."""
    text = "\n".join(part for part in evidence if part).lower()
    return any(marker in text for marker in PROVIDER_QUOTA_MARKERS)


def parse_codex_session_id(stdout: str) -> str | None:
    for event in parse_jsonl_events(stdout):
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            return event["thread_id"]
    return None


def parse_event_errors(stdout: str) -> list[str]:
    messages: list[str] = []
    for event in parse_jsonl_events(stdout):
        if event.get("type") != "error":
            continue
        candidates: list[Any] = [event]
        error = event.get("error")
        if isinstance(error, dict):
            candidates.append(error)
            if isinstance(error.get("data"), dict):
                candidates.append(error["data"])
        for candidate in reversed(candidates):
            message = candidate.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
                break
    return messages


def parse_opencode_output(stdout: str) -> tuple[str, str | None]:
    text_parts: list[str] = []
    session_id: str | None = None
    for event in parse_jsonl_events(stdout):
        if session_id is None and isinstance(event.get("sessionID"), str):
            session_id = event["sessionID"]
        part = event.get("part")
        if (
            event.get("type") == "text"
            and isinstance(part, dict)
            and isinstance(part.get("text"), str)
        ):
            text_parts.append(part["text"])
    return "\n".join(text_parts), session_id


def git_optional_output(repo: Path, *args: str) -> str:
    try:
        return git_output(repo, *args).decode("utf-8", errors="replace").strip()
    except RunnerError:
        return ""


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def nonempty_line_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line)


def git_identity(repo: Path) -> dict[str, Any]:
    """What this run must not change: this worktree's HEAD, branch, HEAD reflog and the stash.

    Other worktrees and other branches move on their own when work runs in parallel; comparing them made
    any commit or `worktree add` elsewhere fail the job. They are tracked by git_activity() instead and
    reported as concurrent activity, not as a history violation.
    """
    common_dir = git_optional_output(repo, "rev-parse", "--git-common-dir")
    if common_dir:
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = (repo / common_path).resolve()
        common_dir = str(common_path)
    branch = (
        git_optional_output(repo, "symbolic-ref", "--quiet", "--short", "HEAD") or None
    )
    head_reflog = git_optional_output(
        repo, "reflog", "show", "-1", "--format=%H%x00%gs", "HEAD"
    )
    branch_ref = (
        git_optional_output(repo, "rev-parse", "--verify", f"refs/heads/{branch}") if branch else ""
    )
    stash = git_optional_output(repo, "stash", "list", "--format=%H%x00%gd%x00%gs")
    return {
        "head": git_optional_output(repo, "rev-parse", "--verify", "HEAD") or None,
        "branch": branch,
        "branch_ref": branch_ref or None,
        "head_reflog_sha256": text_fingerprint(head_reflog),
        "stash_sha256": text_fingerprint(stash),
        "stash_count": nonempty_line_count(stash),
        "common_dir": common_dir or None,
    }


def git_activity(repo: Path) -> dict[str, Any]:
    """Repository state shared with other worktrees: branch names and worktree paths (not their HEADs)."""
    ref_names = git_optional_output(repo, "for-each-ref", "--format=%(refname)", "refs/heads")
    worktrees = git_optional_output(repo, "worktree", "list", "--porcelain")
    paths = sorted(line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree "))
    return {
        "local_ref_names_sha256": text_fingerprint(ref_names),
        "local_ref_count": nonempty_line_count(ref_names),
        "worktree_paths_sha256": text_fingerprint("\n".join(paths)),
        "worktree_count": len(paths),
    }


def find_history_violations(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def wait_with_heartbeats(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    heartbeat_seconds: float,
    monotonic_start: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_process(process)
            return True
        wait_for = (
            remaining if heartbeat_seconds <= 0 else min(remaining, heartbeat_seconds)
        )
        try:
            process.wait(timeout=wait_for)
            return False
        except subprocess.TimeoutExpired:
            if heartbeat_seconds > 0:
                elapsed = round(time.monotonic() - monotonic_start, 1)
                print(f"AGENT_PROGRESS elapsed_seconds={elapsed}", flush=True)


def verification_summary(
    commands: list[str],
    *,
    status: str,
    skipped_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "command_count": len(commands),
        "completed_count": 0,
        "skipped_reason": skipped_reason,
        "commands": [],
    }


def nonempty_tail(text: str, limit: int = 20) -> list[str]:
    return [
        line[-MAX_VERIFICATION_TAIL_LINE_CHARS:]
        for line in text.splitlines()
        if line
    ][-limit:]


def read_text_tail(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        start = max(0, size - MAX_VERIFICATION_TAIL_BYTES)
        handle.seek(start)
        data = handle.read()
    if start and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    return data.decode("utf-8", errors="replace"), size


def run_verification_commands(
    commands: list[str],
    *,
    repo: Path,
    out_dir: Path,
    timeout_seconds: float,
    deadline: str | None = None,
    lease: int | None = None,
) -> dict[str, Any]:
    if not commands:
        return verification_summary(commands, status="not_requested")

    command_results: list[dict[str, Any]] = []
    environment = dict(os.environ)
    environment["PWD"] = str(repo)
    # Python checks (including their subprocesses) must not mutate the worktree
    # merely by importing project modules. Explicit writers are still audited.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    overall_status = "passed"

    for index, command in enumerate(commands, start=1):
        remaining = deadline_remaining(deadline)
        if remaining is not None and remaining <= 0:
            overall_status = "timed_out"
            break
        stdout_path = out_dir / f"verification-{index:02d}.stdout.txt"
        stderr_path = out_dir / f"verification-{index:02d}.stderr.txt"
        started = time.monotonic()
        with (
            open_private_binary(stdout_path) as stdout_handle,
            open_private_binary(stderr_path) as stderr_handle,
        ):
            process = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=repo,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
                pass_fds=(lease,) if lease is not None else (),
            )
            timed_out = wait_with_heartbeats(
                process,
                timeout_seconds=min(timeout_seconds, remaining) if remaining is not None else timeout_seconds,
                heartbeat_seconds=0,
                monotonic_start=started,
            )

        stdout_text, stdout_size = read_text_tail(stdout_path)
        stderr_text, stderr_size = read_text_tail(stderr_path)
        command_status = (
            "timed_out"
            if timed_out
            else ("passed" if process.returncode == 0 else "failed")
        )
        command_results.append(
            {
                "index": index,
                "command": command,
                "status": command_status,
                "exit_code": process.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "stdout_size_bytes": stdout_size,
                "stderr_size_bytes": stderr_size,
                "stdout_tail": nonempty_tail(stdout_text),
                "stderr_tail": nonempty_tail(stderr_text),
            }
        )
        if command_status != "passed":
            overall_status = command_status
            break

    return {
        "status": overall_status,
        "command_count": len(commands),
        "completed_count": len(command_results),
        "skipped_reason": None,
        "commands": command_results,
    }


def classify_result(
    *,
    history_violations: list[str],
    scope_violations: list[str],
    timed_out: bool,
    executor_exit_code: int | None,
    stdout_present: bool,
    report_extracted: bool,
    expect_changes: bool,
    workspace_changed: bool,
    reported_outcome: str | None = "complete",
    report_contract_valid: bool = True,
) -> tuple[str, int]:
    if history_violations:
        return "history_violation", 21
    if scope_violations:
        return "scope_violation", 22
    if timed_out:
        return "timed_out", 12
    if executor_exit_code != 0:
        exit_code = (
            executor_exit_code if executor_exit_code and executor_exit_code > 0 else 1
        )
        return "failed", exit_code
    if not stdout_present:
        return "empty_output", 3
    if not report_extracted:
        return "malformed_report", 23
    if not report_contract_valid:
        return "malformed_report", 23
    if reported_outcome in (None, "unknown"):
        return "malformed_report", 23
    if reported_outcome == "blocked":
        return "blocked", 24
    if reported_outcome == "failed":
        return "failed", 1
    if expect_changes and not workspace_changed:
        return "no_changes", 20
    return "completed", 0


def resolve_additional_directories(values: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for value in values:
        directory = value.expanduser().resolve()
        if not directory.is_dir():
            raise RunnerError(
                f"additional workspace directory does not exist: {directory}", 4
            )
        if directory not in resolved:
            resolved.append(directory)
    return resolved


def executor_environment(
    engine: str, repo: Path | None = None, claude_config_dir: Path | None = None
) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not HOST_SESSION_ENV.match(key)}
    if engine == "claude" and claude_config_dir is not None:
        # Chooses the Claude account. Unset means Claude Code's default profile (~/.claude).
        environment["CLAUDE_CONFIG_DIR"] = str(claude_config_dir.expanduser())
    if repo is not None:
        environment["PWD"] = str(repo)
    if engine == "agy":
        # Antigravity's terminal-title updates are not task progress and can bypass
        # ordinary stdout/stderr capture when the caller owns a PTY.
        environment["TERM"] = "dumb"
        environment["NO_COLOR"] = "1"
    if engine == "opencode":
        environment["OPENCODE_PERMISSION"] = json.dumps("allow")
        environment["OPENCODE_AUTO_SHARE"] = "false"
        environment["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(OPENCODE_RUNTIME_CONFIG)
    return environment


_NATIVE_FAMILIES = (
    ("claude", {"anthropic"}, {"claude", "opus", "sonnet", "haiku", "fable"}),
    ("codex", {"openai"}, {"gpt", "chatgpt"}),
    ("agy", {"google", "google-ai-studio", "google-vertex", "vertex"}, {"gemini"}),
)


def native_engine_for(model: str) -> str | None:
    """The engine whose first-party harness owns this model family (Claude, GPT, Gemini), or None.

    Those families always run in their own harness: Claude Code, Codex, Antigravity. OpenCode/OpenRouter
    is for families without one here: GLM, DeepSeek, Qwen, Kimi and the like. Open-weight GPT-OSS and
    Gemma are not the hosted families and stay allowed.
    """
    segments = model.lower().split("/")
    tokens = re.split(r"[-_.:~]", segments[-1])
    if "gemma" in tokens or any(a == "gpt" and b == "oss" for a, b in zip(tokens, tokens[1:])):
        return None
    for engine, providers, names in _NATIVE_FAMILIES:
        if any(segment in providers for segment in segments[:-1]):
            return engine
        for index, token in enumerate(tokens):
            if token in names and not (token == "gpt" and index + 1 < len(tokens) and tokens[index + 1] == "oss"):
                return engine
    return None


def enforce_native_route(engine: str, model: str) -> None:
    native = native_engine_for(model) if engine == "opencode" else None
    if native:
        raise RunnerError(
            f"{model} belongs to a family with its own harness; run it with --engine {native}, not through "
            "OpenCode/OpenRouter. OpenCode is only for families without a native harness (GLM, DeepSeek, Qwen, "
            "Kimi, ...).",
            EXIT_ROUTE_NOT_ALLOWED,
        )


def select_live_model(
    engine: str,
    requested_model: str | None,
    catalog: dict[str, Any],
) -> tuple[str, str]:
    if catalog.get("status") != "live":
        detail = catalog.get("error") or catalog.get("status")
        exit_code = 13 if catalog.get("status") == "not_installed" else 11
        raise RunnerError(f"cannot retrieve live {engine} models: {detail}", exit_code)
    available_models = [
        model["id"]
        for model in catalog.get("models", [])
        if not model.get("hidden", False)
    ]
    model = requested_model or preferred_model(engine)
    if model in available_models:
        reason = "exact_user_request" if requested_model else "validated_preference"
        return model, reason
    if engine == "claude" and CLAUDE_MODEL_ID.match(model):
        return model, "unverified_claude_id"

    suggestions = difflib.get_close_matches(model, available_models, n=3, cutoff=0.35)
    suggestion_text = (
        f" Closest live IDs: {', '.join(suggestions)}." if suggestions else ""
    )
    preference_text = (
        "The policy preference is no longer available."
        if requested_model is None
        else "The requested ID is not available."
    )
    raise RunnerError(
        f"{preference_text} {engine} model: {model}.{suggestion_text} "
        f"Run `{Path(__file__).name} models --engine {engine}` and select an exact ID.",
        14,
    )


def executor_preflight(
    engine: str,
    requested_model: str | None,
    *,
    refresh: bool = False,
) -> tuple[str, str, list[str], str, str, dict[str, Any]]:
    if requested_model:
        enforce_native_route(engine, requested_model)
    catalog = discover_engine_catalog(engine, refresh=refresh)
    try:
        model, selection_reason = select_live_model(
            engine, requested_model, catalog
        )
    except RunnerError:
        if refresh or catalog.get("cache", {}).get("status") != "hit":
            raise
        catalog = discover_engine_catalog(engine, refresh=True)
        model, selection_reason = select_live_model(
            engine, requested_model, catalog
        )
    executable = catalog.get("executable")
    if not isinstance(executable, str):
        raise RunnerError(f"{engine} is not on PATH", 13)
    available_models = [
        item["id"]
        for item in catalog.get("models", [])
        if not item.get("hidden", False)
    ]
    return (
        executable,
        str(catalog.get("version") or "unknown"),
        available_models,
        model,
        selection_reason,
        catalog,
    )


def build_executor_command(
    *,
    executable: str,
    engine: str,
    model: str,
    variant: str | None,
    repo: Path,
    add_dirs: list[Path],
    session_id: str | None,
    continue_last: bool,
    timeout: str,
    submitted_brief_path: Path,
    final_output_path: Path,
    log_path: Path,
    effort: str | None = None,
) -> list[str]:
    if engine == "agy":
        command = [executable]
        if session_id:
            command += ["--conversation", session_id]
        elif continue_last:
            command += ["--continue"]
        else:
            command += ["--new-project", "--add-dir", str(repo)]
            for directory in add_dirs:
                command += ["--add-dir", str(directory)]
        command += [
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--print-timeout",
            timeout,
            "--log-file",
            str(log_path),
            f"--print={submitted_brief_path.read_text(encoding='utf-8')}",
        ]
        return command

    if engine == "claude":
        command = [executable, "-p", "--output-format", "json", "--model", model,
                   "--permission-mode", "bypassPermissions", "--add-dir", str(repo)]
        for directory in add_dirs:
            command += ["--add-dir", str(directory)]
        if effort is not None:
            command += ["--effort", effort]
        if session_id:
            command += ["--resume", session_id]
        elif continue_last:
            command += ["--continue"]
        return command

    if engine == "codex":
        if session_id or continue_last:
            command = [executable, "exec", "resume"]
            command += [session_id] if session_id else ["--last"]
        else:
            command = [executable, "exec", "-C", str(repo)]
            for directory in add_dirs:
                command += ["--add-dir", str(directory)]
        if effort is not None:
            command += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        command += [
            "--model",
            model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--output-last-message",
            str(final_output_path),
            "-",
        ]
        return command

    command = [executable, "run"]
    if session_id:
        command += ["--session", session_id]
    elif continue_last:
        command += ["--continue"]
    command += [
        "--model",
        model,
        "--variant",
        variant or DEFAULT_OPENCODE_VARIANT,
        "--pure",
        "--agent",
        "build",
        "--format",
        "json",
        "--auto",
        "--dir",
        str(repo),
        "Execute the attached execution brief exactly and return its required report.",
        "--file",
        str(submitted_brief_path),
    ]
    return command


def validated_run_options(
    args: argparse.Namespace, *, task_spec: dict[str, Any] | None = None
) -> tuple[
    str | None,
    list[Path],
    list[str],
    list[str],
    list[str],
    float,
    float,
]:
    variant = args.variant
    if args.resume_result and not args.session:
        raise RunnerError("--resume-result requires an exact --session", 4)
    if args.engine == "opencode":
        variant = variant or DEFAULT_OPENCODE_VARIANT
    elif variant:
        raise RunnerError("--variant is only valid with --engine opencode", 4)
    if args.heartbeat_seconds < 0:
        raise RunnerError("--heartbeat-seconds must be 0 or greater", 4)
    if args.add_dir and (args.session or args.continue_last):
        raise RunnerError(
            "--add-dir is only valid when creating a fresh executor session", 4
        )
    if args.engine == "opencode" and args.add_dir:
        raise RunnerError(
            "--engine opencode cannot faithfully establish additional workspace roots; use codex or agy",
            4,
        )
    add_dirs = resolve_additional_directories(args.add_dir)
    if task_spec is not None:
        if args.allow_path or args.track_path:
            raise RunnerError(
                "--task-spec derives scope; do not also pass --allow-path or --track-path",
                4,
            )
        allowed_paths = normalize_repo_paths(
            list(task_spec["allow_paths"]), option="task-spec allow_paths"
        )
        tracked_paths = normalize_repo_paths(
            list(task_spec["track_paths"]), option="task-spec track_paths"
        )
    else:
        allowed_paths = normalize_repo_paths(args.allow_path, option="--allow-path")
        tracked_paths = normalize_repo_paths(args.track_path, option="--track-path")
    enforced_paths = list(dict.fromkeys([*allowed_paths, *tracked_paths]))
    if args.expect_changes and not enforced_paths:
        raise RunnerError(
            "--expect-changes requires at least one --allow-path or --track-path for scope enforcement",
            4,
        )
    if args.retry_read_only < 0 or args.retry_read_only > 3:
        raise RunnerError("--retry-read-only must be between 0 and 3", 4)
    if args.retry_read_only and (args.expect_changes or enforced_paths):
        raise RunnerError(
            "--retry-read-only requires a read-only run with no allowed or tracked paths",
            4,
        )
    if any(not command.strip() for command in args.verify_command):
        raise RunnerError("--verify-command cannot be empty", 4)
    timeout_seconds = parse_duration(args.timeout)
    deadline_remaining(args.deadline)
    verify_timeout_seconds = parse_duration(
        args.verify_timeout,
        option="--verify-timeout",
    )
    return (
        variant,
        add_dirs,
        allowed_paths,
        tracked_paths,
        enforced_paths,
        timeout_seconds,
        verify_timeout_seconds,
    )


def transient_provider_failure(stdout_text: str, stderr_text: str) -> bool:
    """Conservatively identify provider/network failures that are safe to retry read-only."""
    evidence = f"{stdout_text}\n{stderr_text}".lower()
    markers = (
        "connection reset",
        "connection refused",
        "connection timed out",
        "network error",
        "network is unreachable",
        "temporary failure",
        "temporarily unavailable",
        "service unavailable",
        "gateway timeout",
        "econnreset",
        "econnrefused",
        "eai_again",
        "http 429",
        "http 502",
        "http 503",
        "http 504",
        "rate limit",
    )
    return any(marker in evidence for marker in markers)


def archive_executor_attempt_artifacts(
    *,
    out_dir: Path,
    attempt: int,
    stdout_path: Path,
    stderr_path: Path,
    log_path: Path,
    final_output_path: Path,
) -> dict[str, str]:
    """Keep a failed retry attempt private before its paths are reused."""
    archived: dict[str, str] = {}
    for label, source in (
        ("stdout_path", stdout_path),
        ("stderr_path", stderr_path),
        ("log_path", log_path),
        ("final_output_path", final_output_path),
    ):
        if not source.exists():
            continue
        destination = out_dir / f"attempt-{attempt}-{source.name}"
        os.replace(source, destination)
        destination.chmod(0o600)
        archived[label] = str(destination)
    return archived


def detached_runner_command(
    args: argparse.Namespace,
    *,
    repo: Path,
    brief_path: Path | None,
    task_spec_path: Path | None,
    run_dir: Path,
    event_path: Path,
    notification_mode: str,
    completion_hook: Path | None,
) -> list[str]:
    if (brief_path is None) == (task_spec_path is None):
        raise RunnerError("detached launch requires exactly one task input", 4)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--cwd",
        str(repo),
        "--engine",
        args.engine,
        "--timeout",
        args.timeout,
        "--out-dir",
        str(run_dir),
        "--heartbeat-seconds",
        str(args.heartbeat_seconds),
        "--completion-event",
        str(event_path),
        "--notify",
        notification_mode,
    ]
    if task_spec_path is not None:
        command.extend(["--task-spec", str(task_spec_path)])
    else:
        command.extend(["--brief", str(brief_path)])
    if completion_hook is not None:
        command.extend(["--completion-hook", str(completion_hook)])
    if args.model:
        command.extend(["--model", args.model])
    if args.variant:
        command.extend(["--variant", args.variant])
    if args.effort:
        command.extend(["--effort", args.effort])
    command.extend(["--billing-mode", args.billing_mode])
    if args.deadline:
        command.extend(["--deadline", args.deadline])
    if args.resume_result:
        command.extend(["--resume-result", str(args.resume_result.expanduser().resolve())])
    if args.refresh_models:
        command.append("--refresh-models")
    if args.ignore_quota:
        command.append("--ignore-quota")
    if args.claude_config_dir:
        command.extend(["--claude-config-dir", str(args.claude_config_dir.expanduser())])
    for extra in args.add_dir:
        command.extend(["--add-dir", str(extra)])
    if task_spec_path is None:
        for allowed in args.allow_path:
            command.extend(["--allow-path", allowed])
        for tracked in args.track_path:
            command.extend(["--track-path", tracked])
    for verification_command in args.verify_command:
        command.extend(["--verify-command", verification_command])
    command.extend(["--verify-timeout", args.verify_timeout])
    if args.expect_changes:
        command.append("--expect-changes")
    if args.retry_read_only:
        command.extend(["--retry-read-only", str(args.retry_read_only)])
    if args.session:
        command.extend(["--session", args.session])
    elif args.continue_last:
        command.append("--continue")
    return command


def launch_detached(args: argparse.Namespace) -> int:
    if args.brief == "-" or args.task_spec == "-":
        raise RunnerError("--detach requires a task input file instead of stdin", 4)
    if args.out_dir:
        raise RunnerError("--out-dir cannot be combined with --detach; use --job-dir", 4)
    if args.dry_run:
        raise RunnerError("--dry-run cannot be combined with --detach", 4)
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise RunnerError(f"target directory does not exist: {cwd}", 4)
    repo = git_root(cwd)
    if cwd != repo:
        raise RunnerError(f"--cwd must be the Git repository root: {repo}", 4)

    brief, task_spec = resolve_execution_brief(args, repo=repo)
    _, add_dirs, _allowed_paths, tracked_paths, enforced_paths, _, _ = validated_run_options(
        args, task_spec=task_spec
    )
    # Check before detaching so the caller hears about an exhausted pool now, not in a completion event.
    # Fail before reporting "started": model and effort against the live catalog, the native-route rule,
    # quota, and whether another executor already holds this worktree.
    _executable, _version, _models, model, _reason, catalog = executor_preflight(
        args.engine, args.model, refresh=args.refresh_models
    )
    support = bundled_module("execution_support")
    effort = args.effort
    try:
        support.bind_effort(args.engine, model, effort, catalog)
    except support.ContractError as error:
        raise RunnerError(str(error), 4) from error
    quota_gate(args.engine, model, ignore=args.ignore_quota)
    try:
        with support.workspace_lease(repo, default_agent_cache_dir()):
            pass
    except support.ContractError as error:
        raise RunnerError(str(error), 27) from error
    event_dir = default_completion_event_dir().resolve()
    if event_dir == repo or repo in event_dir.parents:
        raise RunnerError(
            "the agent-executor cache must be outside the target repository", 4
        )
    job_dir = prepare_job_dir(args.job_dir, repo)
    event_path = prepare_completion_event_path()
    notification_mode = args.notify or "desktop"
    completion_hook = resolve_completion_hook(args.completion_hook)
    ensure_hook_outside_workspaces(completion_hook, [repo, *add_dirs])
    copied_brief_path: Path | None = None
    copied_task_spec_path: Path | None = None
    stdout_path = job_dir / "runner.stdout"
    stderr_path = job_dir / "runner.stderr"
    run_dir = job_dir / "run"
    result_path = run_dir / "result.json"
    if task_spec is not None:
        copied_task_spec_path = job_dir / "task-spec.json"
        write_json(copied_task_spec_path, task_spec)
    else:
        copied_brief_path = job_dir / "brief.txt"
        write_private_text(copied_brief_path, brief + "\n")
    command = detached_runner_command(
        args,
        repo=repo,
        brief_path=copied_brief_path,
        task_spec_path=copied_task_spec_path,
        run_dir=run_dir,
        event_path=event_path,
        notification_mode=notification_mode,
        completion_hook=completion_hook,
    )
    environment = dict(os.environ)
    environment["PWD"] = str(repo)
    with (
        open_private_binary(stdout_path) as stdout_handle,
        open_private_binary(stderr_path) as stderr_handle,
    ):
        process = subprocess.Popen(
            command,
            cwd=repo,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    threading.Thread(target=process.wait, daemon=True).start()

    job = {
        "schema": "agent-executor.job.v1",
        "runner_version": RUNNER_VERSION,
        "status": "started",
        "pid": process.pid,
        "started_at": utc_now(),
        "repository": str(repo),
        "engine": args.engine,
        "requested_model": args.model,
        "task_input": "task_spec" if task_spec is not None else "brief",
        "allowed_paths": enforced_paths,
        "tracked_paths": tracked_paths,
        "job_dir": str(job_dir),
        "run_dir": str(run_dir),
        "result_path": str(result_path),
        "completion_event_path": str(event_path),
        "completion_event_id": event_path.stem,
        "notification_mode": notification_mode,
        "completion_hook": str(completion_hook) if completion_hook else None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "command": command_preview(command),
    }
    job_path = job_dir / "job.json"
    write_json(job_path, job)
    print("AGENT_JOB_STATUS=started")
    print(f"AGENT_JOB_PID={process.pid}")
    print(f"AGENT_JOB={job_path}")
    print(f"AGENT_RESULT={result_path}")
    print(f"AGENT_EVENT={event_path}")
    print(f"AGENT_EVENT_ID={event_path.stem}")
    return 0


def finish_run(
    *,
    result: dict[str, Any],
    result_path: Path,
    completion_event_path: Path | None,
    notification_mode: str,
    completion_hook: Path | None,
) -> int:
    usage_module = bundled_module("usage")
    result.setdefault("usage", usage_module.empty())
    registry_path = Path(__file__).parents[1] / "references" / "model-registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        result["tariff_estimate"] = usage_module.estimate_tariff(
            result["usage"], result.get("model", ""), registry,
            billing_mode=result.get("billing_mode", "unknown"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        result["tariff_estimate"] = {"estimated_usd": None, "gaps": ["tariff_registry_unavailable_or_invalid"]}
    write_json_atomic(result_path, result)
    if completion_event_path is not None:
        publish_completion_event(
            result=result,
            result_path=result_path,
            event_path=completion_event_path,
            notification_mode=notification_mode,
            completion_hook=completion_hook,
        )
    print_summary(result, result_path)
    return int(result["exit_code"])


def publish_uncaught_completion_failure(error: Exception, exit_code: int) -> None:
    if "--completion-event" not in sys.argv[1:]:
        return
    try:
        args = parse_args(sys.argv[1:])
    except SystemExit:
        return
    try:
        args = resolve_route(args)
    except RunnerError:
        args.engine = args.engine or DEFAULT_ENGINE
    if not args.completion_event or not args.out_dir:
        return
    try:
        event_path = normalize_completion_event_path(args.completion_event)
        if event_path.exists():
            return
        result_path = args.out_dir.expanduser().resolve() / "result.json"
        repository = args.cwd.expanduser().resolve()
        if result_path == repository or repository in result_path.parents:
            return
        result = {
            "schema": RESULT_SCHEMA,
            "runner_version": RUNNER_VERSION,
            "status": "runner_failed",
            "task_outcome": "failed",
            "reported_outcome": None,
            "exit_code": exit_code,
            "error": str(error),
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "engine": args.engine,
            "model": args.model or preferred_model(args.engine),
            "variant": args.variant,
            "full_permissions": True,
            "repository": str(repository),
            "scope_violations": [],
            "history_violations": [],
            "verification": verification_summary(
                args.verify_command,
                status="skipped" if args.verify_command else "not_requested",
                skipped_reason=(
                    "runner_failed" if args.verify_command else None
                ),
            ),
        }
        write_json_atomic(result_path, result)
        completion_hook: Path | None
        try:
            completion_hook = resolve_completion_hook(args.completion_hook)
        except RunnerError:
            completion_hook = None
        publish_completion_event(
            result=result,
            result_path=result_path,
            event_path=event_path,
            notification_mode=args.notify or "none",
            completion_hook=completion_hook,
        )
    except Exception as notification_error:
        print(
            f"agent-executor: could not publish failure event: {notification_error}",
            file=sys.stderr,
        )


AUTO_PRUNE_SECONDS = 30 * 24 * 60 * 60


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0


def prune_state(older_than_seconds: float, *, dry_run: bool = False) -> dict[str, Any]:
    """Remove runner state older than the cutoff. Unacknowledged completion events, and the job
    directories they point to, are always kept: they are results nobody has reviewed yet."""
    cache = default_agent_cache_dir()
    cutoff = time.time() - older_than_seconds
    removed = {"events": 0, "jobs": 0, "runs": 0, "bytes": 0}
    protected: set[Path] = set()
    for event_path in sorted((cache / "completions-v1").glob("*.json")):
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result_path = Path(str(event.get("result_path") or ""))
        job_dir = next((parent for parent in result_path.parents if parent.parent == cache / "jobs-v1"), None)
        if not event.get("acknowledged_at") or event_path.stat().st_mtime > cutoff:
            if job_dir is not None:
                protected.add(job_dir)
            continue
        removed["events"] += 1
        removed["bytes"] += event_path.stat().st_size
        if not dry_run:
            event_path.unlink(missing_ok=True)
    for kind, directory in (("jobs", cache / "jobs-v1"), ("runs", cache / "runs-v1")):
        for entry in sorted(directory.glob("*")) if directory.is_dir() else []:
            if not entry.is_dir() or entry in protected or entry.stat().st_mtime > cutoff:
                continue
            removed[kind] += 1
            removed["bytes"] += _tree_size(entry)
            if not dry_run:
                shutil.rmtree(entry, ignore_errors=True)
    return {"dry_run": dry_run, "older_than_seconds": older_than_seconds, "cache": str(cache), **removed}


def auto_prune() -> None:
    """At most once a day, drop state older than 30 days. Best effort; never fails a run."""
    marker = default_agent_cache_dir() / "last-prune"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < 24 * 60 * 60:
            return
        prune_state(AUTO_PRUNE_SECONDS)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass


def prune_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(__file__).name} prune",
                                     description="Remove acknowledged events and job/run directories older than a cutoff.")
    parser.add_argument("--older-than", default="14d", help="Age cutoff such as 14d, 12h (default 14d)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be removed")
    args = parser.parse_args(argv)
    print(json.dumps(prune_state(parse_duration(args.older_than, option="--older-than"), dry_run=args.dry_run), indent=2))
    return 0


def routes_module() -> Any:
    return bundled_module("routes")


def preferred_model(engine: str) -> str:
    """The model an engine runs when none is given: its default route, else the built-in fallback."""
    try:
        route = routes_module().engine_default(engine)
    except (OSError, ValueError):
        route = None
    return (route or {}).get("model") or PREFERRED_MODELS[engine]


def resolve_route(args: argparse.Namespace) -> argparse.Namespace:
    """Fill engine, model, effort and variant from --route, or from the engine's default route."""
    module = routes_module()
    try:
        routes = module.load_routes()
    except (OSError, ValueError) as error:
        raise RunnerError(f"cannot load routes: {error}", 4) from error
    route = None
    if args.route:
        if args.route not in routes:
            raise RunnerError(f"unknown --route {args.route!r}; configured: {', '.join(sorted(routes))}", 4)
        route = routes[args.route]
        if args.engine and args.engine != route["engine"]:
            route = None  # an explicit engine overrides the route entirely
    args.engine = args.engine or (route or {}).get("engine") or DEFAULT_ENGINE
    route = route or module.engine_default(args.engine, routes)
    if route and route.get("engine") == args.engine:
        if args.model is None and args.route:
            args.model = route.get("model")
        model = args.model or route.get("model")
        if args.effort is None and route.get("effort") and model == route.get("model"):
            args.effort = route["effort"]
        if getattr(args, "variant", None) is None and route.get("variant") and args.engine == "opencode":
            args.variant = route["variant"]
    args.resolved_route = args.route
    return args


def routes_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(__file__).name} routes",
                                     description="Show the role routes in effect (defaults plus your config).")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    module = routes_module()
    routes = module.load_routes()
    if args.format == "json":
        print(json.dumps({"config": str(module.config_path()), "routes": routes}, indent=2))
        return 0
    print(f"config: {module.config_path()}{'' if module.config_path().exists() else ' (not present: defaults only)'}")
    for role, route in routes.items():
        extra = " ".join(f"--{key} {value}" for key, value in route.items() if key in ("effort", "variant"))
        print(f"  {role:<15} --engine {route['engine']} --model {route.get('model', '(engine default)')} {extra}".rstrip())
    return 0


def init_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(__file__).name} init", description="Write a starter route config.")
    parser.add_argument("--force", action="store_true", help="Replace an existing config")
    args = parser.parse_args(argv)
    module = routes_module()
    path = module.config_path()
    if path.exists() and not args.force:
        print(f"{path} exists; pass --force to replace it")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(module.starter_config(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}; edit the routes there")
    return 0


HOST_MARKERS = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "codex": ("CODEX_SANDBOX", "CODEX_THREAD_ID", "CODEX_MANAGED_BY_NPM"),
    "opencode": ("OPENCODE", "OPENCODE_SESSION_ID"),
    "agy": ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_AGENT"),
}


def doctor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{Path(__file__).name} doctor",
                                     description="Report host, installed CLIs, routes, quota source, paths and skill installs.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    hosts = [host for host, keys in HOST_MARKERS.items() if any(os.environ.get(key) for key in keys)]
    clis = {}
    for engine in EXECUTION_ENGINES:
        executable = shutil.which(engine)
        version = None
        if executable:
            try:
                version = cli_version(executable, executor_environment(engine))
            except (OSError, RunnerError) as error:
                version = f"error: {error}"
        clis[engine] = {"executable": executable, "version": version}
    skill_dir = Path(__file__).resolve().parent.parent
    installs = {}
    for location in ("~/.agents/skills", "~/.claude/skills", "~/.claude-personal/skills", "~/.codex/skills",
                     "~/.config/opencode/skills", "~/.gemini/config/skills"):
        entry = Path(location).expanduser() / "agent-executor"
        if entry.exists() or entry.is_symlink():
            resolved = entry.resolve()
            installs[location] = {"resolves_to": str(resolved), "same_as_this": resolved == skill_dir,
                                  "kind": "link" if entry.is_symlink() else "copy"}
    quota = bundled_module("quota")
    report = {
        "runner_version": RUNNER_VERSION,
        "skill_dir": str(skill_dir),
        "host": hosts or ["unknown"],
        "clis": clis,
        "routes_config": str(routes_module().config_path()),
        "routes": routes_module().load_routes(),
        "ai_usage": quota.ai_usage_command(),
        "cache_dir": str(default_agent_cache_dir()),
        "skill_installs": installs,
        "stale_copies": [loc for loc, info in installs.items() if not info["same_as_this"]],
    }
    if args.format == "json":
        print(json.dumps(report, indent=2))
        return 0
    print(f"agent-executor {RUNNER_VERSION} at {skill_dir}")
    print(f"host: {', '.join(report['host'])}")
    for engine, info in clis.items():
        print(f"  {engine:<9} {info['executable'] or 'not installed'}{'  ' + info['version'] if info['version'] else ''}")
    print(f"routes: {report['routes_config']} ({len(report['routes'])} roles; run `routes` for detail)")
    print(f"quota source: {report['ai_usage'] or 'ai-usage not installed (quota unknown, runs not blocked)'}")
    print(f"cache: {report['cache_dir']}")
    for location, info in installs.items():
        flag = "" if info["same_as_this"] else "  <- different copy: update or relink it"
        print(f"  skill {location}: {info['kind']} -> {info['resolves_to']}{flag}")
    return 0


def quota_gate(engine: str, model: str, *, ignore: bool) -> None:
    """Refuse to launch into an exhausted subscription pool and warn when little is usable now.

    Quota is read live from ai-usage and never stored. Missing ai-usage means unknown quota, which
    does not block a run.
    """
    if ignore:
        return
    quota = bundled_module("quota")
    view = quota.snapshot()
    result = quota.check(engine, model, view)
    if result["status"] == "blocked":
        raise RunnerError(quota.blocked_message(engine, model, result, view), quota.EXIT_QUOTA_EXHAUSTED)
    if result["status"] == "low":
        emit_lifecycle(
            "quota_low", pool=result.get("label"), usable_now_pct=round(result.get("availableNowPct") or 0),
            advice=result.get("advice"),
        )


def quota_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{Path(__file__).name} quota",
        description="Live subscription quota for each executor route, read from ai-usage. Nothing is stored.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    quota = bundled_module("quota")
    view = quota.snapshot()
    rows = quota.route_table(view)
    if args.format == "json":
        print(json.dumps({"source": "ai-usage" if view else None,
                          "generated_at": view.get("generatedAt") if view else None, "routes": rows}, indent=2))
        return 0
    if view is None:
        print("ai-usage is not installed or did not answer: quota unknown, runs are not blocked.")
    for row in rows:
        head = f"{row['engine']:<21} {row['models']:<21}"
        if row["status"] in ("not_applicable", "unknown"):
            print(f"{head} {row['status']}: {row.get('detail', '')}")
            continue
        points = row.get("surplusPts")
        pace = f"{points:+d} pts" if isinstance(points, int) and row["status"] != "blocked" else ""
        billing = " [personal]" if row.get("billing") == "personal" else ""
        print(f"{head} {row['status']:<9} {round(row['availableNowPct']):>3}% usable {pace:>8}  "
              f"{row['label']}{billing}: {row['advice']}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "models":
        return models_main(argv[1:])
    if argv and argv[0] == "events":
        return events_main(argv[1:])
    if argv and argv[0] == "quota":
        return quota_main(argv[1:])
    if argv and argv[0] == "prune":
        return prune_main(argv[1:])
    if argv and argv[0] == "routes":
        return routes_main(argv[1:])
    if argv and argv[0] == "init":
        return init_main(argv[1:])
    if argv and argv[0] == "doctor":
        return doctor_main(argv[1:])
    auto_prune()
    args = resolve_route(parse_args(argv))
    if args.detach:
        return execute(args)
    repo = git_root(args.cwd.expanduser().resolve())
    support = bundled_module("execution_support")
    try:
        with support.workspace_lease(repo, default_agent_cache_dir()) as lease:
            return execute(args, lease=lease)
    except support.ContractError as exc:
        raise RunnerError(str(exc), 27) from exc


def execute(args: argparse.Namespace, *, lease: int | None = None) -> int:
    if args.job_dir and not args.detach:
        raise RunnerError("--job-dir requires --detach", 4)
    if (args.notify or args.completion_hook) and not (
        args.detach or args.completion_event
    ):
        raise RunnerError(
            "--notify and --completion-hook are only valid with --detach", 4
        )
    if args.detach:
        return launch_detached(args)
    completion_event_path = (
        normalize_completion_event_path(args.completion_event)
        if args.completion_event
        else None
    )
    notification_mode = args.notify or "none"
    completion_hook = resolve_completion_hook(args.completion_hook)
    started_at = utc_now()
    monotonic_start = time.monotonic()
    model = args.model or preferred_model(args.engine)
    # resolve_route() already applied the route's effort when the model is the route's model.
    selected_effort = args.effort
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise RunnerError(f"target directory does not exist: {cwd}", 4)
    repo = git_root(cwd)
    if cwd != repo:
        raise RunnerError(f"--cwd must be the Git repository root: {repo}", 4)
    brief, task_spec = resolve_execution_brief(args, repo=repo)
    (
        variant,
        add_dirs,
        allowed_paths,
        tracked_paths,
        enforced_paths,
        timeout_seconds,
        verify_timeout_seconds,
    ) = validated_run_options(args, task_spec=task_spec)
    ensure_hook_outside_workspaces(completion_hook, [repo, *add_dirs])
    out_dir = prepare_out_dir(args.out_dir, repo)
    result_path = out_dir / "result.json"
    original_brief_path = out_dir / "brief.txt"
    submitted_brief_path = out_dir / "submitted-brief.txt"
    stdout_path = out_dir / "stdout.txt"
    stderr_path = out_dir / "stderr.txt"
    log_path = out_dir / "executor.log"
    final_output_path = out_dir / "final-output.txt"
    write_private_text(original_brief_path, brief + "\n")
    submitted_brief = f"{brief}\n\n{REPORT_CONTRACT}\n"
    write_private_text(submitted_brief_path, submitted_brief)
    write_private_text(log_path, "")
    write_private_text(final_output_path, "")

    try:
        (
            executable,
            version,
            available_models,
            model,
            model_selection,
            model_catalog,
        ) = executor_preflight(
            args.engine,
            args.model,
            refresh=args.refresh_models,
        )
        quota_gate(args.engine, model, ignore=args.ignore_quota or args.dry_run)
        support = bundled_module("execution_support")
        try:
            effort = support.bind_effort(args.engine, model, selected_effort, model_catalog)
            previous_result = None
            if args.resume_result:
                previous_result = json.loads(args.resume_result.expanduser().read_text(encoding="utf-8"))
                support.validate_resume(previous_result, {
                    "repository": str(repo), "session_id": args.session,
                    "engine": args.engine, "model": model, "variant": variant,
                    "executor_version": version,
                    "effort": effort["bound"], "allowed_paths": enforced_paths,
                    "tracked_paths": tracked_paths, "verification_commands": args.verify_command,
                    "billing_mode": args.billing_mode,
                })
        except support.ContractError as error:
            raise RunnerError(str(error), 4) from error
        except (OSError, ValueError, TypeError) as error:
            raise RunnerError(f"invalid --resume-result: {error}", 4) from error
    except RunnerError as exc:
        result = {
            "schema": RESULT_SCHEMA,
            "runner_version": RUNNER_VERSION,
            "status": "preflight_failed",
            "task_outcome": "failed",
            "reported_outcome": None,
            "exit_code": exc.exit_code,
            "error": str(exc),
            "started_at": started_at,
            "finished_at": utc_now(),
            "engine": args.engine,
            "model": model,
            "requested_model": args.model,
            "requested_effort": selected_effort,
            "variant": variant,
            "full_permissions": True,
            "repository": str(repo),
            "verification": verification_summary(
                args.verify_command,
                status="skipped" if args.verify_command else "not_requested",
                skipped_reason=(
                    "preflight_failed" if args.verify_command else None
                ),
            ),
        }
        return finish_run(
            result=result,
            result_path=result_path,
            completion_event_path=completion_event_path,
            notification_mode=notification_mode,
            completion_hook=completion_hook,
        )

    before = git_state(repo, tracked_paths)
    git_identity_before = git_identity(repo)
    git_activity_before = git_activity(repo)
    if previous_result is not None and (
        previous_result.get("git_after", {}).get("fingerprint") != before["fingerprint"]
        or previous_result.get("git_identity_after") != git_identity_before
    ):
        raise RunnerError("workspace or history changed since the resume result; investigate before correction", 4)
    command = build_executor_command(
        executable=executable,
        engine=args.engine,
        model=model,
        variant=variant,
        repo=repo,
        add_dirs=add_dirs,
        session_id=args.session,
        continue_last=args.continue_last,
        timeout=args.timeout,
        submitted_brief_path=submitted_brief_path,
        final_output_path=final_output_path,
        log_path=log_path,
        effort=effort["bound"] if args.engine in ("codex", "claude") else None,
    )

    base_result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "runner_version": RUNNER_VERSION,
        "engine": args.engine,
        "executor_version": version,
        "model": model,
        "requested_model": args.model,
        "resolved_model": None,
        "effort": effort,
        "billing_mode": args.billing_mode,
        "resumed_from": str(args.resume_result.expanduser().resolve()) if args.resume_result else None,
        "variant": variant,
        "model_preflight": "live_exact_match",
        "model_selection": model_selection,
        "model_catalog": {
            "source": model_catalog.get("source"),
            "discovered_at": model_catalog.get("discovered_at"),
            "cache": model_catalog.get("cache"),
            "available_model_count": len(available_models),
        },
        "full_permissions": True,
        "sandbox": False,
        "repository": str(repo),
        "task_input": "task_spec" if task_spec is not None else "brief",
        "started_at": started_at,
        "command": command_preview(command),
        "brief_path": str(original_brief_path),
        "submitted_brief_path": str(submitted_brief_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "log_path": str(log_path),
        "final_output_path": str(final_output_path),
        "git_before": before,
        "git_identity_before": git_identity_before,
        "allowed_paths": enforced_paths,
        "tracked_paths": tracked_paths,
        "expected_changes": args.expect_changes,
        "retry_read_only": args.retry_read_only,
        "heartbeat_seconds": args.heartbeat_seconds,
        "verification_commands": args.verify_command,
        "verification_timeout": args.verify_timeout,
        "deadline": args.deadline,
    }

    if args.dry_run:
        result = {
            **base_result,
            "status": "dry_run",
            "task_outcome": "not_run",
            "reported_outcome": None,
            "exit_code": 0,
            "finished_at": utc_now(),
            "duration_seconds": round(time.monotonic() - monotonic_start, 3),
            "git_after": before,
            "git_identity_after": git_identity_before,
            "run_delta": {"changed_paths": [], "tracked_paths_changed": []},
            "scope_violations": [],
            "history_violations": [],
            "workspace_changed": False,
            "workspace_changed_after_executor": False,
            "verification_changed_workspace": False,
            "verification_delta": {
                "changed_paths": [],
                "tracked_paths_changed": [],
            },
            "verification": verification_summary(
                args.verify_command,
                status="dry_run",
            ),
            "report_extracted": False,
            "report_contract_valid": False,
            "report_contract_violations": ["missing_heading:STATUS"],
            "final_message": "",
        }
        return finish_run(
            result=result,
            result_path=result_path,
            completion_event_path=completion_event_path,
            notification_mode=notification_mode,
            completion_hook=completion_hook,
        )

    environment = executor_environment(args.engine, repo, args.claude_config_dir)
    remaining = deadline_remaining(args.deadline)
    if remaining is not None and remaining <= 0:
        raise RunnerError("task deadline expired before executor dispatch", 28)
    usage_module = bundled_module("usage")
    usage_baseline = (previous_result or {}).get("usage", {}).get("cumulative")
    attempt_usage: list[dict[str, Any]] = []
    executor_attempts: list[dict[str, Any]] = []
    attempt_number = 0
    while True:
        attempt_number += 1
        remaining = deadline_remaining(args.deadline)
        stdin_handle: Any = (
            submitted_brief_path.open("rb")
            if args.engine in ("codex", "claude")
            else subprocess.DEVNULL
        )
        try:
            emit_lifecycle(
                "executor_started",
                engine=args.engine,
                model=model,
                attempt=attempt_number,
            )
            with (
                open_private_binary(stdout_path) as stdout_handle,
                open_private_binary(stderr_path) as stderr_handle,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=repo,
                    env=environment,
                    stdin=stdin_handle,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                    pass_fds=(lease,) if lease is not None else (),
                )
                timed_out = wait_with_heartbeats(
                    process,
                    timeout_seconds=min(timeout_seconds + 60, max(0, remaining)) if remaining is not None else timeout_seconds + 60,
                    heartbeat_seconds=args.heartbeat_seconds,
                    monotonic_start=monotonic_start,
                )
        finally:
            if args.engine == "codex":
                stdin_handle.close()

        for artifact in (stdout_path, stderr_path, log_path, final_output_path):
            if artifact.exists():
                artifact.chmod(0o600)
        stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
        stderr_bytes = stderr_path.read_bytes() if stderr_path.exists() else b""
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        usage = usage_module.normalize_usage(
            args.engine, stdout_text, resumed=bool(args.session or args.continue_last), baseline=usage_baseline,
        )
        attempt_usage.append(usage)
        if args.session or args.continue_last:
            usage_baseline = usage.get("cumulative") or usage_baseline
        after_attempt = git_state(repo, tracked_paths)
        git_identity_after_attempt = git_identity(repo)
        attempt_delta = state_delta(before, after_attempt)
        attempt_history_violations = find_history_violations(
            git_identity_before, git_identity_after_attempt
        )
        attempt_workspace_changed = before["fingerprint"] != after_attempt["fingerprint"]
        # agy reports provider errors (e.g. 429) as a terminal status with exit 0; count those as well.
        terminal_error = (
            agy_terminal_error(stdout_text) if args.engine == "agy"
            else parse_claude_output(stdout_text)[3] if args.engine == "claude" else None
        )
        transient_failure = (
            not timed_out
            and (process.returncode not in (None, 0) or terminal_error is not None)
            and (transient_provider_failure(stdout_text, stderr_text) or provider_quota_exhausted(terminal_error))
        )
        retry_eligible = (
            args.retry_read_only > len(executor_attempts)
            and transient_failure
            and not attempt_workspace_changed
            and not attempt_history_violations
            and (args.deadline is None or deadline_remaining(args.deadline) > 0)
        )
        attempt_record: dict[str, Any] = {
            "attempt": attempt_number,
            "executor_exit_code": process.returncode,
            "timed_out": timed_out,
            "transient_provider_failure": transient_failure,
            "workspace_changed": attempt_workspace_changed,
            "history_violations": attempt_history_violations,
            "run_delta": attempt_delta,
            "usage": usage,
        }
        emit_lifecycle(
            "executor_finished",
            exit_code=process.returncode,
            changed_files=len(attempt_delta["changed_paths"]),
            attempt=attempt_number,
        )
        if retry_eligible:
            attempt_record["retrying"] = True
            attempt_record.update(
                archive_executor_attempt_artifacts(
                    out_dir=out_dir,
                    attempt=attempt_number,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    log_path=log_path,
                    final_output_path=final_output_path,
                )
            )
            executor_attempts.append(attempt_record)
            time.sleep(min(float(attempt_number), 3.0))
            continue

        attempt_record["retrying"] = False
        attempt_record.update(
            {
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "log_path": str(log_path),
                "final_output_path": str(final_output_path),
            }
        )
        executor_attempts.append(attempt_record)
        after_executor = after_attempt
        git_identity_after_executor = git_identity_after_attempt
        run_delta_after_executor = attempt_delta
        history_violations_after_executor = attempt_history_violations
        workspace_changed_after_executor = attempt_workspace_changed
        break

    session_id: str | None
    agy_status = None
    if args.engine == "codex":
        candidate_message = (
            final_output_path.read_text(encoding="utf-8", errors="replace")
            if final_output_path.exists()
            else ""
        )
        session_id = parse_codex_session_id(stdout_text)
    elif args.engine == "opencode":
        candidate_message, session_id = parse_opencode_output(stdout_text)
    elif args.engine == "claude":
        candidate_message, session_id, agy_status, _claude_error = parse_claude_output(stdout_text)
        final_output_path.write_text(candidate_message, encoding="utf-8")
        final_output_path.chmod(0o600)
    else:
        candidate_message, session_id, agy_status = parse_agy_output(stdout_text)
        session_id = session_id or parse_agy_session_id(log_path)
        final_output_path.write_text(candidate_message, encoding="utf-8")
        final_output_path.chmod(0o600)
    session_id = session_id or args.session
    raw_report, report_extracted = extract_raw_final_report(candidate_message)
    final_message = bound_final_message(raw_report)
    reported_outcome = parse_reported_outcome(
        raw_report,
        report_extracted,
    )
    if agy_status is not None and agy_status != "success":
        reported_outcome = "blocked" if agy_status == "waiting_for_input" else "failed"
    report_violations = report_contract_violations(
        raw_report,
        report_extracted,
    )
    report_contract_valid = not report_violations
    consultation = support.consultation_from_report(raw_report, reported_outcome)
    if consultation["status"] == "invalid":
        report_violations.append("invalid_consultation_request")
        report_contract_valid = False
    scope_violations_after_executor = find_scope_violations(
        run_delta_after_executor,
        enforced_paths,
    )

    preliminary_status, preliminary_exit_code = classify_result(
        history_violations=history_violations_after_executor,
        scope_violations=scope_violations_after_executor,
        timed_out=timed_out,
        executor_exit_code=process.returncode,
        stdout_present=bool(candidate_message.strip()),
        report_extracted=report_extracted,
        expect_changes=args.expect_changes,
        workspace_changed=workspace_changed_after_executor,
        reported_outcome=reported_outcome,
        report_contract_valid=report_contract_valid,
    )

    if preliminary_status == "completed":
        emit_lifecycle(
            "verification_started",
            commands=len(args.verify_command),
        )
        verification = run_verification_commands(
            args.verify_command,
            repo=repo,
            out_dir=out_dir,
            timeout_seconds=verify_timeout_seconds,
            deadline=args.deadline,
            lease=lease,
        )
        emit_lifecycle(
            "verification_finished",
            status=verification["status"],
            completed=verification["completed_count"],
        )
    else:
        verification = verification_summary(
            args.verify_command,
            status="skipped" if args.verify_command else "not_requested",
            skipped_reason=(
                f"runner_status:{preliminary_status}"
                if args.verify_command
                else None
            ),
        )

    after = git_state(repo, tracked_paths)
    git_identity_after = git_identity(repo)
    git_activity_after = git_activity(repo)
    run_delta = state_delta(before, after)
    verification_delta = state_delta(after_executor, after)
    history_violations = find_history_violations(
        git_identity_before,
        git_identity_after,
    )
    scope_violations = find_scope_violations(run_delta, enforced_paths)
    workspace_changed = before["fingerprint"] != after["fingerprint"]
    verification_changed_workspace = (
        verification["completed_count"] > 0
        and after_executor["fingerprint"] != after["fingerprint"]
    )

    status, exit_code = preliminary_status, preliminary_exit_code
    if history_violations:
        status, exit_code = "history_violation", 21
    elif scope_violations:
        status, exit_code = "scope_violation", 22
    elif verification_changed_workspace:
        status, exit_code = "verification_mutation", 26
    elif verification["status"] in {"failed", "timed_out"}:
        status, exit_code = "verification_failed", 25
    provider_error = (
        agy_terminal_error(stdout_text) if args.engine == "agy"
        else parse_claude_output(stdout_text)[3] if args.engine == "claude" else None
    )
    provider_failed = provider_error is not None or (agy_status not in (None, "success", "waiting_for_input"))
    if (
        status in {"failed", "malformed_report", "empty_output"}
        and (provider_failed or process.returncode not in (None, 0))
        and provider_quota_exhausted(provider_error, stderr_text, stdout_text if provider_failed else None)
    ):
        # The account ran out of quota or credits mid-run: not a task failure; retry elsewhere or later.
        status, exit_code = "provider_quota_exhausted", EXIT_PROVIDER_QUOTA

    post_failure_catalog: dict[str, Any] | None = None
    if process.returncode not in (None, 0):
        refreshed_catalog = discover_engine_catalog(args.engine, refresh=True)
        refreshed_ids = [
            item["id"]
            for item in refreshed_catalog.get("models", [])
            if not item.get("hidden", False)
        ]
        post_failure_catalog = {
            "status": refreshed_catalog.get("status"),
            "source": refreshed_catalog.get("source"),
            "discovered_at": refreshed_catalog.get("discovered_at"),
            "cache": refreshed_catalog.get("cache"),
            "model_still_available": model in refreshed_ids,
            "available_model_count": len(refreshed_ids),
            "closest_models": difflib.get_close_matches(
                model, refreshed_ids, n=5, cutoff=0.25
            ),
            "error": refreshed_catalog.get("error"),
        }

    review = compact_review_bundle(
        repo=repo,
        before=before,
        run_delta=run_delta,
        verification=verification,
        result_path=result_path,
        session_id=session_id,
        scope_violations=scope_violations,
        history_violations=history_violations,
    )
    result = {
        **base_result,
        "status": status,
        "task_outcome": task_outcome_for_status(status),
        "reported_outcome": reported_outcome,
        "provider_terminal_status": agy_status,
        "provider_error": provider_error,
        "exit_code": exit_code,
        "executor_exit_code": process.returncode,
        "executor_attempts": executor_attempts,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - monotonic_start, 3),
        "session_id": session_id,
        "git_after_executor": after_executor,
        "git_identity_after_executor": git_identity_after_executor,
        "git_after": after,
        "git_identity_after": git_identity_after,
        "run_delta_after_executor": run_delta_after_executor,
        "run_delta": run_delta,
        "scope_violations_after_executor": scope_violations_after_executor,
        "history_violations_after_executor": history_violations_after_executor,
        "scope_violations": scope_violations,
        "history_violations": history_violations,
        # Other worktrees or branches changed during the run: informational, never a failure.
        "concurrent_activity": find_history_violations(git_activity_before, git_activity_after),
        "workspace_changed": workspace_changed,
        "workspace_changed_after_executor": workspace_changed_after_executor,
        "verification_changed_workspace": verification_changed_workspace,
        "verification_delta": verification_delta,
        "verification": verification,
        "report_extracted": report_extracted,
        "report_contract_valid": report_contract_valid,
        "report_contract_violations": report_violations,
        "stdout_size_bytes": len(stdout_bytes),
        "final_message": final_message,
        "stderr_tail": [line for line in stderr_text.splitlines() if line][-20:],
        "event_errors": parse_event_errors(stdout_text)[-20:],
        "post_failure_model_catalog": post_failure_catalog,
        "review": review,
        "usage": usage_module.combine_attempts(attempt_usage),
        "consultation": consultation,
    }
    return finish_run(
        result=result,
        result_path=result_path,
        completion_event_path=completion_event_path,
        notification_mode=notification_mode,
        completion_hook=completion_hook,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        publish_uncaught_completion_failure(exc, exc.exit_code)
        print(f"agent-executor: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)
    except Exception as exc:
        publish_uncaught_completion_failure(exc, 1)
        raise
