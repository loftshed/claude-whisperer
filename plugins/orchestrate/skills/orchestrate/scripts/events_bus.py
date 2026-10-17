"""Durable completion events, notifications, and event-bus CLI for agent-executor."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

DEFAULT_EVENT_WAIT_TIMEOUT = "30m"
DEFAULT_EVENT_POLL_SECONDS = 0.5
MAX_COMPLETION_SIGNAL_ERROR_CHARS = 500
COMPLETION_EVENT_SCHEMA = "agent-executor.completion.v1"
COMPLETION_LIST_SCHEMA = "agent-executor.completion-list.v1"
COMPLETION_SIGNAL_SCHEMA = "agent-executor.completion-signal.v1"
NOTIFICATION_MODES = ("none", "desktop")
# How long a finished run's result may stand without its completion event before waiting reports it.
MISSING_EVENT_GRACE_SECONDS = 5.0


def _runner() -> Any:
    mod = globals().get("_runner_module")
    if mod is not None and hasattr(mod, "RunnerError"):
        return mod
    for key in ("run_agent", "agent_executor_run_agent", "__main__"):
        candidate = sys.modules.get(key)
        if candidate is not None and hasattr(candidate, "RunnerError"):
            return candidate
    return None


def _runner_error(message: str, exit_code: int = 2) -> Exception:
    runner = _runner()
    error_cls = getattr(runner, "RunnerError", ValueError)
    try:
        return error_cls(message, exit_code)
    except TypeError:
        return error_cls(message)


def _utc_now() -> str:
    runner = _runner()
    if runner is not None and hasattr(runner, "utc_now"):
        return runner.utc_now()
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


def _default_agent_cache_dir() -> Path:
    runner = _runner()
    if runner is not None and hasattr(runner, "default_agent_cache_dir"):
        return runner.default_agent_cache_dir()
    home = os.environ.get("AGENT_EXECUTOR_HOME")
    if home:
        return Path(home).expanduser()
    configured_root = os.environ.get("XDG_CACHE_HOME")
    cache_root = Path(configured_root).expanduser() if configured_root else Path.home() / ".cache"
    return cache_root / "agent-executor"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    runner = _runner()
    if runner is not None and hasattr(runner, "write_json_atomic"):
        runner.write_json_atomic(path, payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def parse_events_args(argv: list[str] | None = None) -> argparse.Namespace:
    arguments = list(argv) if argv is not None else None
    action = arguments.pop(0) if arguments and arguments[0] in ("wait", "follow") else "list"
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
            help=("Exact event ID or AGENT_EVENT path" + ("; repeatable" if action == "follow" else "")),
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
                + ("; one JSON object is emitted per completed event)" if action == "follow" else ")")
            ),
        )
        parser.add_argument(
            "--messages",
            action="store_true",
            help="Return when a worker message needs attention, as well as on completion",
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


def default_completion_event_dir() -> Path:
    return _default_agent_cache_dir() / "completions-v1"


def prepare_completion_event_path() -> Path:
    event_dir = default_completion_event_dir().resolve()
    event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    event_dir.chmod(0o700)
    return event_dir / f"{uuid.uuid4().hex}.json"


def normalize_completion_event_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_parent = default_completion_event_dir().resolve()
    if resolved.parent != expected_parent:
        raise _runner_error(f"internal completion event must be inside {expected_parent}", 4)
    expected_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected_parent.chmod(0o700)
    return resolved


def completion_event_path_from_reference(reference: str) -> Path:
    value = reference.strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return normalize_completion_event_path(default_completion_event_dir() / f"{value.lower()}.json")

    candidate = Path(value).expanduser()
    if candidate.suffix != ".json" or not re.fullmatch(r"[0-9a-fA-F]{32}", candidate.stem):
        raise _runner_error(
            "completion event must be an exact 32-character event ID or AGENT_EVENT .json path",
            4,
        )
    return normalize_completion_event_path(candidate)


def read_completion_event(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _runner_error(f"could not read completion event {path}: {exc}", 4) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _runner_error(f"invalid completion event {path}: {exc}", 4) from exc
    if not isinstance(payload, dict) or payload.get("schema") != COMPLETION_EVENT_SCHEMA:
        raise _runner_error(f"unsupported completion event schema: {path}", 4)
    if payload.get("event_id") != path.stem:
        raise _runner_error(f"completion event ID does not match its path: {path}", 4)
    return {**payload, "event_path": str(path)}


def resolve_completion_hook(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise _runner_error(f"--completion-hook must be an executable file: {resolved}", 4)
    return resolved


def ensure_hook_outside_workspaces(hook: Path | None, workspace_roots: list[Path]) -> None:
    if hook is None:
        return
    for root in workspace_roots:
        resolved_root = root.resolve()
        if hook == resolved_root or resolved_root in hook.parents:
            raise _runner_error("--completion-hook must be outside every executor workspace", 4)


def send_desktop_notification(event: dict[str, Any]) -> dict[str, Any]:
    runner = _runner()
    sys_mod = getattr(runner, "sys", sys)
    shutil_mod = getattr(runner, "shutil", shutil)
    subprocess_mod = getattr(runner, "subprocess", subprocess)

    status = str(event.get("status") or "finished")
    engine = str(event.get("engine") or "agent")
    model = str(event.get("model") or "").strip()
    repository = Path(str(event.get("repository") or "workspace")).name
    title = f"Agent executor: {status.replace('_', ' ')}"
    details = " · ".join(item for item in (engine, model, repository) if item)

    if sys_mod.platform == "darwin":
        executable = shutil_mod.which("osascript")
        if not executable:
            return {"kind": "desktop", "status": "unsupported"}
        command = [
            executable,
            "-e",
            ("on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"),
            title,
            details,
        ]
        provider = "macos"
    elif sys_mod.platform.startswith("linux"):
        executable = shutil_mod.which("notify-send")
        if not executable:
            return {"kind": "desktop", "status": "unsupported"}
        command = [executable, title, details]
        provider = "freedesktop"
    else:
        return {"kind": "desktop", "status": "unsupported"}

    try:
        completed = subprocess_mod.run(
            command,
            check=False,
            stdin=subprocess_mod.DEVNULL,
            capture_output=True,
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


def run_completion_hook(hook: Path, event_path: Path, event: dict[str, Any]) -> dict[str, Any]:
    runner = _runner()
    subprocess_mod = getattr(runner, "subprocess", subprocess)
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
            "AGENT_VERIFICATION_STATUS": str(event.get("verification_status") or ""),
            "AGENT_ENGINE": str(event.get("engine") or ""),
            "AGENT_MODEL": str(event.get("model") or ""),
        }
    )
    try:
        completed = subprocess_mod.run(
            [str(hook), str(event_path)],
            check=False,
            stdin=subprocess_mod.DEVNULL,
            capture_output=True,
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
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repository_value = result.get("repository")
    repository = str(Path(str(repository_value)).expanduser().resolve()) if repository_value else None
    mailbox = result.get("mailbox") or {}
    event = {
        "schema": COMPLETION_EVENT_SCHEMA,
        "event_id": event_path.stem,
        "created_at": _utc_now(),
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
        "run_id": mailbox.get("run"),
        "mailbox": mailbox.get("conductor"),
        "error": result.get("error") or result.get("provider_error"),
        "scope_violations": result.get("scope_violations", []),
        "history_violations": result.get("history_violations", []),
        "deliveries": [],
        **(extra or {}),
    }
    _write_json_atomic(event_path, event)

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
    _write_json_atomic(event_path, event)
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
    runner = _runner()
    runner_error_cls = getattr(runner, "RunnerError", ValueError)
    for path in sorted(event_dir.glob("*.json")):
        try:
            payload = read_completion_event(path)
        except runner_error_cls as exc:
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
        "model": result_model if (result_model := event.get("model")) else None,
        "variant": event.get("variant"),
        "repository": event.get("repository"),
        "result_path": event.get("result_path"),
        "event_path": event.get("event_path"),
        "error": error or None,
        "scope_violation_count": len(event.get("scope_violations") or []),
        "history_violation_count": len(event.get("history_violations") or []),
        "steered_from": event.get("steered_from") or [],
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
        f"AGENT_VERIFICATION_STATUS={signal_payload['verification_status'] or ''}",
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
    messages: bool = False,
) -> Iterator[dict[str, Any]]:
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise _runner_error("--poll-seconds must be a finite number greater than 0", 4)

    runner = _runner()
    pending: dict[Path, str | None] = dict.fromkeys(event_paths)
    terminal_since: dict[Path, float] = {}
    mailbox = runner.bundled_module("peer_mailbox") if (messages and runner is not None) else None
    deadline = time.monotonic() + timeout_seconds
    repository_text = str(repository.resolve()) if repository else None
    while pending:
        grace_seconds = getattr(runner, "MISSING_EVENT_GRACE_SECONDS", MISSING_EVENT_GRACE_SECONDS)
        for path in list(pending):
            event = read_completion_event(path)
            if messages and mailbox is not None:
                run_id = (
                    (event or {}).get("run_id")
                    or pending[path]
                    or mailbox.aliased_run(mailbox.root(), path.stem)
                    or path.stem
                )
                info = mailbox.run_info(mailbox.root(), run_id)
                if info is not None:
                    run_peer = mailbox.run_peers(mailbox.root(), run_id)["conductor"]
                    if repository_text and run_peer["repository"] != repository_text:
                        raise _runner_error("mailbox run does not belong to this repository", 4)
                    incoming = mailbox.run_inbox(mailbox.root(), run_id, "conductor")
                    status = mailbox.run_status(mailbox.root(), run_id)
                    if event is None and status:
                        terminal_since.setdefault(path, time.monotonic())
                    # A run past its deadline publishes no event, and one whose result was written but whose
                    # event still is not there a few seconds later lost it; say so instead of waiting out.
                    stalled = event is None and (
                        status == "deadline_expired"
                        or (status and time.monotonic() - terminal_since[path] >= grace_seconds)
                    )
                    if incoming or stalled:
                        yield {
                            "schema": "agent-executor.message-signal.v1",
                            "event_id": path.stem,
                            "mailbox": info["conductor"],
                            "messages": incoming,
                            "run_status": status,
                        }
                        return
            if event is None:
                continue
            superseded_by = event.get("superseded_by")
            if isinstance(superseded_by, str) and re.fullmatch(r"[0-9a-f]{32}", superseded_by):
                # A steered job continues under a new event; keep waiting for the job, not the segment.
                run_id = event.get("run_id") or pending[path] or path.stem
                del pending[path]
                pending.setdefault(normalize_completion_event_path(path.parent / f"{superseded_by}.json"), run_id)
                continue
            if repository_text:
                event_repository = event.get("repository")
                normalized_repository = (
                    str(Path(str(event_repository)).expanduser().resolve()) if event_repository else None
                )
                if normalized_repository != repository_text:
                    raise _runner_error(
                        f"completion event {event['event_id']} does not belong to {repository_text}",
                        4,
                    )
            del pending[path]
            yield event

        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pending_ids = ", ".join(path.stem for path in pending)
            raise _runner_error(
                f"timed out waiting for completion event(s): {pending_ids}",
                12,
            )
        time.sleep(min(poll_seconds, remaining))


def acknowledge_completion_event(event: dict[str, Any]) -> None:
    event_path = Path(str(event["event_path"]))
    payload = {key: value for key, value in event.items() if key != "event_path"}
    payload["acknowledged_at"] = _utc_now()
    _write_json_atomic(event_path, payload)


def events_main(argv: list[str] | None = None) -> int:
    runner = _runner()
    args = parse_events_args(argv)
    repository: Path | None = None
    if args.cwd:
        cwd = args.cwd.expanduser().resolve()
        if not cwd.is_dir():
            raise _runner_error(f"target directory does not exist: {cwd}", 4)
        repository = runner.git_root(cwd)
        if cwd != repository:
            raise _runner_error(f"--cwd must be the Git repository root: {repository}", 4)

    if args.action in ("wait", "follow"):
        references = args.event if isinstance(args.event, list) else [args.event]
        event_paths = list(dict.fromkeys(completion_event_path_from_reference(reference) for reference in references))
        timeout_seconds = runner.parse_duration(args.timeout)
        for event in wait_for_completion_events(
            event_paths,
            repository=repository,
            timeout_seconds=timeout_seconds,
            poll_seconds=args.poll_seconds,
            messages=args.messages,
        ):
            if event.get("schema") == "agent-executor.message-signal.v1":
                print(json.dumps(event, ensure_ascii=True), flush=True)
            else:
                print_completion_signal(event, output_format=args.format)
        return 0

    events, errors = completion_events(
        repository=repository,
        include_acknowledged=args.all or bool(args.ack),
    )
    if args.ack:
        matches = [event for event in events if event.get("event_id") == args.ack]
        if not matches:
            raise _runner_error(f"completion event not found: {args.ack}", 4)
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
        print(f"AGENT_VERIFICATION_STATUS={event.get('verification_status') or ''}")
        print(f"AGENT_ENGINE={event.get('engine') or ''}")
        print(f"AGENT_MODEL={event.get('model') or ''}")
        print(f"AGENT_RESULT={event.get('result_path') or ''}")
    if errors:
        print(f"AGENT_EVENT_ERRORS={json.dumps(errors, ensure_ascii=True)}")
    return 0
