"""Report contract validation, brief loading, and outcome classification for agent-executor."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

MAX_FINAL_MESSAGE_CHARS = 4000
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
AGY_ONESHOT_GUARDRAIL = (
    "\n- In one-shot `--print` mode, never stop calling tools to wait for a background task or timer "
    "(`WaitMsBeforeAsync`), because ending a turn terminates the session immediately; run commands "
    "synchronously to completion."
)

_STATUS_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*|__)?STATUS(?:\*\*|__)?(?:\s*:\s*(.*)|\s*)$",
    re.MULTILINE | re.IGNORECASE,
)


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


def read_brief(source: str) -> str:
    if source == "-":
        text = sys.stdin.read()
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise _runner_error(f"brief file not found: {path}")
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise _runner_error("brief is empty")
    size = len(text.encode("utf-8"))
    if size > 120 * 1024:
        raise _runner_error(
            f"brief is {size // 1024} KiB; keep it below 120 KiB and point the executor to workspace files for bulky context"
        )
    return text.rstrip()


def brief_renderer_module() -> Any:
    """Load the colocated renderer without depending on the caller's sys.path."""
    runner = _runner()
    if runner is not None and hasattr(runner, "bundled_module"):
        return runner.bundled_module("render_brief")
    import importlib.util

    renderer_path = Path(__file__).with_name("render_brief.py")
    spec = importlib.util.spec_from_file_location("agent_executor_render_brief", renderer_path)
    if spec is None or spec.loader is None:
        raise _runner_error("could not load the bundled task-spec renderer", 4)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_task_spec(source: str, *, repo: Path) -> tuple[str, dict[str, Any]]:
    """Render and validate a task spec whose root is exactly the execution repo."""
    renderer = brief_renderer_module()
    try:
        values = renderer.validate_spec(renderer.read_json(source))
        declared_root = Path(values["repository_root"]).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise _runner_error(f"task spec is invalid: {exc}", 4) from exc
    if declared_root != repo:
        raise _runner_error(
            f"--task-spec repository_root must exactly match --cwd: {declared_root} != {repo}",
            4,
        )
    try:
        brief = renderer.render_brief(values)
    except ValueError as exc:
        raise _runner_error(f"task spec cannot render a brief: {exc}", 4) from exc
    return brief.rstrip(), values


def resolve_execution_brief(args: argparse.Namespace, *, repo: Path) -> tuple[str, dict[str, Any] | None]:
    """Return the standalone brief and, for specs, the normalized authoritative values."""
    if args.task_spec:
        if args.allow_path or args.track_path:
            raise _runner_error(
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
    violations = [f"brief_missing_heading:{heading}" for heading, position in positions.items() if position < 0]
    if not violations:
        ordered = [positions[heading] for heading in REPORT_HEADINGS]
        if ordered != sorted(ordered):
            violations.append("brief_heading_order")
    return violations


def validate_brief_contract(brief: str) -> None:
    violations = brief_report_contract_violations(brief)
    if violations:
        raise _runner_error(
            "brief final-response contract is malformed: " + ", ".join(violations),
            4,
        )


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
    matches = list(_STATUS_HEADING_RE.finditer(stdout))
    if not matches:
        return bound_final_message(stdout.strip()[-MAX_FINAL_MESSAGE_CHARS:]), False
    report = stdout[matches[-1].start() :].strip()
    return report, True


def parse_reported_outcome(report: str, report_extracted: bool) -> str | None:
    if not report_extracted:
        return None
    lines = report.splitlines()
    status_heading: int | None = None
    inline_status = ""
    for index, line in enumerate(lines):
        if match := _STATUS_HEADING_RE.fullmatch(line):
            status_heading = index
            inline_status = (match.group(1) or "").strip()
            break
    if status_heading is None:
        return "unknown"
    status_line = inline_status or next(
        (line.strip() for line in lines[status_heading + 1 :] if line.strip()),
        "",
    )
    # Models often format the token: `COMPLETE`, **COMPLETE**, COMPLETE. all mean COMPLETE.
    status_line = re.sub(r"^[-*]\s+", "", status_line)
    status_line = re.sub(r"^[`*_]+([A-Za-z]+)[`*_]+", r"\1", status_line)
    status_line = re.sub(r"^([A-Za-z]+)\.$", r"\1", status_line.strip())
    match = re.match(
        r"^(?:[-*]\s*)?(COMPLETE|COMPLETED|SUCCESS|SUCCEEDED|PASSED|PARTIAL|BLOCKED|BLOCKER|FAILED|FAILURE)\b(.*)$",
        status_line,
        re.IGNORECASE,
    )
    if not match:
        return "unknown"
    token = match.group(1).lower()
    rest = (match.group(2) or "").strip()
    if token in {"complete", "completed", "success", "succeeded", "passed"}:
        if rest and re.search(r"\b(?:except|but|however|unresolved|incomplete)\b", rest, re.IGNORECASE):
            return "unknown"
        return "complete"
    if token == "partial":
        return "partial"
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
            r"(?:\*\*|__)?(?:\s*:.*|\s*)"
        ),
        re.IGNORECASE,
    )
    positions: dict[str, list[int]] = {heading: [] for heading in REPORT_HEADINGS}
    for index, line in enumerate(report.splitlines()):
        if match := heading_pattern.fullmatch(line):
            heading_name = match.group(1).upper()
            # Only STATUS allows inline text after the colon; other headings allow at most trailing whitespace.
            if (
                heading_name != "STATUS"
                and ":" in line
                and not re.fullmatch(
                    r"(?:#{1,6}\s+)?(?:\*\*|__)?(?:FILES CHANGED|COMMANDS RUN|VERIFICATION|RISKS OR BLOCKERS)(?:\*\*|__)?:?\s*",
                    line,
                    re.IGNORECASE,
                )
            ):
                continue
            positions[heading_name].append(index)

    violations = [f"missing_heading:{heading}" for heading in REPORT_HEADINGS if not positions[heading]]
    violations.extend(f"duplicate_heading:{heading}" for heading in REPORT_HEADINGS if len(positions[heading]) > 1)
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
        executor_failed = executor_exit_code not in (None, 0)
        if not (
            not executor_failed
            and report_extracted
            and report_contract_valid
            and reported_outcome in {"complete", "blocked"}
        ):
            return "timed_out", 12
    elif executor_exit_code != 0:
        return "failed", 1
    if reported_outcome == "failed":
        return "failed", 1
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
    if expect_changes and not workspace_changed:
        return "no_changes", 20
    return "completed", 0
