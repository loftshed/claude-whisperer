"""Small execution contracts shared by the runner and its conductor."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:
    fcntl = None


class ContractError(ValueError):
    pass


@contextlib.contextmanager
def workspace_lease(repository: Path, cache: Path) -> Iterator[int]:
    """Serialize cooperating runners; a child inherits the lease across parent death.

    Keep the lock file in place. Unlinking a flock file allows another process to
    acquire a different inode while an existing holder still owns the old one.
    This does not stop editors or other tools from changing the workspace.
    """
    if fcntl is None:
        raise ContractError("workspace leases require POSIX flock on this platform")
    directory = (cache / "worktree-locks-v1").resolve()
    repository = repository.resolve()
    if directory == repository or repository in directory.parents:
        raise ContractError("the executor cache must be outside the repository")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(os.fsencode(str(repository))).hexdigest()
    descriptor = os.open(directory / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError(f"another executor owns the worktree: {repository}") from exc
        # Closing, rather than explicitly unlocking, keeps inherited child locks
        # valid if the runner disappears before its executor has exited.
        yield descriptor
    finally:
        os.close(descriptor)


def bind_effort(
    engine: str, model: str, requested: str | None, catalog: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "requested": requested,
        "bound": None,
        "binding": "cli_default_or_config",
        "observed": None,
    }
    if engine == "codex" and requested is not None:
        record = next((item for item in catalog.get("models", []) if item.get("id") == model), {})
        supported = record.get("reasoning_efforts", [])
        if requested not in supported:
            raise ContractError(
                f"effort {requested!r} is not advertised for {model}; supported: {supported}"
            )
        result.update(bound=requested, binding="model_reasoning_effort")
    elif engine == "agy":
        # Exact catalog-validated slugs work on old and new AGY versions. Never
        # silently change the user's selected model to satisfy an effort request.
        match = re.search(r"-(low|medium|high)$", model)
        bound = match.group(1) if match else None
        if requested is not None and requested != bound:
            raise ContractError(
                "AGY effort must match an exact catalog model ending in -low, -medium, or -high"
            )
        if bound:
            result.update(bound=bound, binding="catalog_model_slug")
    elif requested is not None:
        raise ContractError("--effort is supported for Codex and AGY; use --variant for OpenCode")
    return result


CONSULTATION_FIELDS = ("question", "decision_needed", "observations", "attempts", "versions", "hypotheses")


def validate_consultation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("consultation request must be an object")
    unknown = set(value) - set(CONSULTATION_FIELDS)
    if unknown:
        raise ContractError("unknown consultation fields: " + ", ".join(sorted(unknown)))
    for field in CONSULTATION_FIELDS:
        entry = value.get(field)
        if field in ("question", "decision_needed"):
            if not isinstance(entry, str) or not entry.strip():
                raise ContractError(f"consultation {field} must be a nonempty string")
        elif not isinstance(entry, list) or any(not isinstance(item, str) or not item.strip() for item in entry):
            raise ContractError(f"consultation {field} must be an array of nonempty strings")
    if not value["observations"] or not value["hypotheses"]:
        raise ContractError("consultation needs observations and at least one hypothesis")
    if len(json.dumps(value).encode()) > 16 * 1024:
        raise ContractError("consultation request exceeds 16 KiB")
    return {field: value[field] for field in CONSULTATION_FIELDS}


def consultation_from_report(report: str, outcome: str | None) -> dict[str, Any]:
    """A worker may request advice. It cannot approve it or mark evidence verified."""
    blocks = list(re.finditer(r"(?ms)^```consultation-request\s*\n(.*?)^```\s*$", report))
    if not blocks:
        return {"status": "not_requested", "request": None, "error": None}
    try:
        if len(blocks) != 1:
            raise ContractError("expected one consultation-request block")
        risk = list(re.finditer(r"(?im)^(?:#{1,6}\s+)?RISKS OR BLOCKERS\s*$", report))
        if not risk or blocks[0].start() < risk[-1].end():
            raise ContractError("consultation request must be under RISKS OR BLOCKERS")
        if outcome not in {"blocked", "failed"}:
            raise ContractError("consultation requests require a BLOCKED or FAILED outcome")
        request = validate_consultation(json.loads(blocks[0].group(1)))
        return {"status": "requested", "request": request, "error": None}
    except (ValueError, TypeError) as exc:
        return {"status": "invalid", "request": None, "error": str(exc)}


def validate_resume(previous: dict[str, Any], expected: dict[str, Any]) -> None:
    if not isinstance(previous, dict):
        raise ContractError("resume baseline must be a JSON object")
    if previous.get("schema") != "agent-executor.result.v2":
        raise ContractError("resume baseline is not an agent-executor result")
    if previous.get("status") not in {"completed", "blocked", "failed", "no_changes", "verification_failed", "malformed_report"}:
        raise ContractError("this runner outcome requires conductor investigation before resumption")
    if previous.get("scope_violations") or previous.get("history_violations"):
        raise ContractError("cannot resume a result with scope or history violations")
    for field, value in expected.items():
        actual = previous.get(field)
        if field == "effort":
            actual = (actual or {}).get("bound")
        if actual != value:
            raise ContractError(f"resume result does not match {field}")
