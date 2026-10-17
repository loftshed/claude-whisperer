"""Structured output parsers and provider error classification for agent-executor."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Provider error messages that mean the account is out of quota or credits, not that the task failed.
# They are matched only against provider error channels (stderr, structured CLI error fields), never
# against the model's content or tool output, and they are anchored: a bare "quota" or "rate limit"
# is ordinary vocabulary in code and docs.
PROVIDER_QUOTA_PATTERNS = re.compile(
    r"\bresource_exhausted\b"
    r"|\b(?:http|status|code|error|api error)\s*[:=(]?\s*429\b"
    r"|\b429\s+too many requests\b"
    r"|\b(?:insufficient_quota|rate_limit_error|rate_limit_exceeded|usage_limit_reached|quota_exceeded)\b"
    r"|\bquota (?:exceeded|exhausted)\b|\bexceeded your current quota\b"
    r"|\brate limit (?:reached|exceeded|hit)\b"
    r"|\busage limit (?:reached|exceeded)\b|\bhit your (?:usage )?limit\b|\breached your usage limit\b"
    r"|\binsufficient credits\b|\bcredit balance is too low\b|\bout of credits\b",
    re.IGNORECASE,
)
# Claude Code's structured assistant error kinds that mean quota or billing, not a task failure.
CLAUDE_QUOTA_ERROR_KINDS = frozenset({"rate_limit", "billing_error"})

TRANSIENT_FAILURE_PATTERNS = (
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
    "http 502",
    "http 503",
    "http 504",
    "operation timed out",
    "stream reading error",
    "network issue connecting",
)

MODEL_AVAILABILITY_FAILURE_PATTERNS = re.compile(
    r"\bmodel[_\s-]*(?:not[_\s-]*found|unavailable|does[_\s-]*not[_\s-]*exist|deprecated|retired|unknown|invalid|unsupported)\b"
    r"|\b(?:unknown|invalid|unsupported|unrecognized|unavailable|deprecated|retired)\s+model\b"
    r"|\bno\s+such\s+model\b"
    r"|\bmodel\s+.*\s+(?:not\s+found|is\s+not\s+available|does\s+not\s+exist|is\s+not\s+supported)\b"
    r"|\bcatalog\b",
    re.IGNORECASE,
)


def _bundled_module(name: str) -> Any:
    runner = globals().get("_runner_module")
    if runner is None or not hasattr(runner, "bundled_module"):
        for key in ("run_agent", "agent_executor_run_agent", "__main__"):
            candidate = sys.modules.get(key)
            if candidate is not None and hasattr(candidate, "bundled_module"):
                runner = candidate
                break
    if runner is not None and hasattr(runner, "bundled_module"):
        return runner.bundled_module(name)
    import importlib.util

    module_path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("agent_executor_" + name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    events = _bundled_module("usage").agy_events(text)
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        # Retain compatibility with older plain-output adapters. Malformed JSON
        # without a terminal report still fails the ordinary report contract.
        return text, None, None
    final = results[-1]
    response = final.get("response")
    status = final.get("status")
    return (
        response if isinstance(response, str) else "",
        final.get("conversation_id"),
        status.strip().lower() if isinstance(status, str) and status.strip() else "unknown",
    )


def agy_terminal_error(text: str) -> str | None:
    results = [event for event in _bundled_module("usage").agy_events(text) if event.get("type") == "result"]
    error = results[-1].get("error") if results else None
    return error.strip() if isinstance(error, str) and error.strip() else None


def claude_result_text(value: Any) -> str:
    """The text of a Claude Code result: a plain string, or a wrapped {"text": ...} / content-block list."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "result", "content"):
            if key in value:
                return claude_result_text(value[key])
        return ""
    if isinstance(value, list):
        return "\n".join(part for part in (claude_result_text(item) for item in value) if part)
    return ""


def parse_claude_output(text: str) -> tuple[str, str | None, str, str | None]:
    """Claude Code `-p` output: (report, session id, "success"|"error"|"unknown", error text).

    Reads the last `result` message whether stdout is one JSON object, a JSON array of every message
    (`--output-format json` with verbose output), or JSON lines / stream-json. A run with several
    results (a background task notification starts another turn) ends with the last one.
    """
    events = _bundled_module("usage").claude_events(text)
    results = [event for event in events if event.get("type") == "result"]
    session_id = next(
        (event["session_id"] for event in reversed(events) if isinstance(event.get("session_id"), str)), None
    )
    if not results:
        return "", session_id, "unknown", None
    final = results[-1]
    if isinstance(final.get("session_id"), str):
        session_id = final["session_id"]
    result = claude_result_text(final.get("result"))
    if final.get("is_error"):
        details = [result] if result.strip() else []
        if isinstance(final.get("error"), str) and final["error"].strip():
            details.append(final["error"].strip())
        errors = final.get("errors")
        if isinstance(errors, list):
            error_items = [
                str(item.get("message") if isinstance(item, dict) and item.get("message") else item)
                for item in errors
                if item
            ]
            if error_items:
                details.append("; ".join(error_items))
        if final.get("api_error_status") is not None:
            details.append(f"api error status {final['api_error_status']}")
        return "", session_id, "error", "\n".join(details) or final.get("subtype") or "error"
    extract_raw_final_report = _bundled_module("report_contract").extract_raw_final_report
    if not extract_raw_final_report(result)[1]:
        # A post-report Stop-hook reply or follow-up turn can overwrite the final result event's text.
        # Scan earlier result and assistant messages in reverse order to preserve a valid report.
        for event in reversed(events):
            kind = event.get("type")
            if kind == "result" and not event.get("is_error"):
                candidate = claude_result_text(event.get("result"))
            elif kind == "assistant":
                candidate = claude_result_text(event.get("message") or event)
            else:
                continue
            if candidate and extract_raw_final_report(candidate)[1]:
                result = candidate
                break
    return result, session_id, "success", None


def codex_terminal_error(text: str) -> str | None:
    """The fatal error Codex reports in its JSONL stream (`error` or `turn.failed`); item-level errors are warnings."""
    message = None
    for event in parse_jsonl_events(text):
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            message = event["message"]
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            elif isinstance(error, str):
                message = error
    return message.strip() if message and message.strip() else None


def opencode_terminal_error(text: str) -> str | None:
    """The error OpenCode reports as a `type: error` event ({"error": {"name", "data": {"message", "statusCode"}}})."""
    message = None
    for event in parse_jsonl_events(text):
        if event.get("type") != "error":
            continue
        error = event.get("error")
        if isinstance(error, str):
            message = error
        elif isinstance(error, dict):
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            parts = [error.get("name"), data.get("message") or error.get("message")]
            if data.get("statusCode") is not None:
                parts.append(f"status {data['statusCode']}")
            message = ": ".join(str(part) for part in parts if part) or json.dumps(error)
    return message.strip() if message and message.strip() else None


def provider_terminal_error(engine: str, text: str) -> str | None:
    """The provider error the CLI reports in its own structured fields, or None. Never the model's content."""
    if engine == "agy":
        return agy_terminal_error(text)
    if engine == "claude":
        return parse_claude_output(text)[3]
    if engine == "codex":
        return codex_terminal_error(text)
    if engine == "opencode":
        return opencode_terminal_error(text)
    return None


def provider_quota_exhausted(*evidence: str | None) -> bool:
    """True when a provider error message names quota, credits or rate limits (e.g. agy 429 RESOURCE_EXHAUSTED).

    Pass only provider error channels: stderr and the CLI's structured error fields. Stdout carries the
    model's content and tool output, where "quota" and "rate limit" are ordinary words.
    """
    return any(PROVIDER_QUOTA_PATTERNS.search(part) for part in evidence if part)


def structured_quota_signal(engine: str, text: str) -> bool:
    """Quota or rate-limit rejection recorded in the CLI's structured fields rather than in message text."""
    if engine == "claude":
        for event in _bundled_module("usage").claude_events(text):
            kind = event.get("type")
            if kind == "rate_limit_event":
                info = event.get("rate_limit_info")
                # `status` is the request's own limit; `overageStatus: rejected` only means no overage billing.
                if isinstance(info, dict) and info.get("status") == "rejected":
                    return True
            elif (
                kind == "assistant"
                and event.get("error") in CLAUDE_QUOTA_ERROR_KINDS
                or kind == "result"
                and event.get("is_error")
                and event.get("api_error_status") == 429
            ):
                return True
    elif engine == "opencode":
        for event in parse_jsonl_events(text):
            error = event.get("error") if event.get("type") == "error" else None
            data = error.get("data") if isinstance(error, dict) else None
            if isinstance(data, dict) and data.get("statusCode") == 429:
                return True
    return False


def parse_codex_session_id(stdout: str) -> str | None:
    for event in parse_jsonl_events(stdout):
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
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
        if event.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
    return "\n".join(text_parts), session_id


def transient_provider_failure(stderr_text: str, provider_error: str | None) -> bool:
    """Conservatively identify provider/network failures that are safe to retry read-only.

    Reads stderr and the CLI's structured error only; stdout holds model content and tool output.
    """
    if provider_quota_exhausted(stderr_text, provider_error):
        return False
    evidence = f"{stderr_text}\n{provider_error or ''}".lower()
    return any(marker in evidence for marker in TRANSIENT_FAILURE_PATTERNS)


def classify_model_availability_failure(stderr_text: str, provider_error: str | None) -> str | None:
    """Identify whether a failed run's stderr or provider_error indicates model/catalog unavailability."""
    evidence = f"{stderr_text}\n{provider_error or ''}"
    match = MODEL_AVAILABILITY_FAILURE_PATTERNS.search(evidence)
    return match.group(0) if match else None
