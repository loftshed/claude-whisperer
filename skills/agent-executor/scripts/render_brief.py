#!/usr/bin/env python3
"""Render a runner-compatible execution brief from a concise JSON task spec."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPORT_HEADINGS = (
    "STATUS",
    "FILES CHANGED",
    "COMMANDS RUN",
    "VERIFICATION",
    "RISKS OR BLOCKERS",
)
REQUIRED_FIELDS = (
    "objective",
    "repository_root",
    "current_state",
    "pre_existing_changes",
    "plan",
    "allow_paths",
    "track_paths",
    "development_checks",
    "acceptance_criteria",
)
COMMON_CONSTRAINTS = (
    "Follow all applicable AGENTS.md and repository instructions.",
    "Preserve unrelated and pre-existing user changes.",
    "Do not change public behavior outside the objective.",
    "Do not add dependencies unless the plan explicitly requires them.",
    "Do not edit checklists, completion plans, or status claims unless an exact file and evidence requirement appear in the approved scope.",
    "Do not commit, push, merge, reset, clean, stash, checkout, switch branches, create worktrees, or share/publish the session.",
    "Stop and report instead of expanding beyond the allowed scope.",
)


class BriefSpecError(ValueError):
    """Raised when a task spec cannot produce a safe standalone brief."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="JSON spec file, or '-' for stdin")
    parser.add_argument(
        "--output",
        default="-",
        help="Rendered Markdown destination, or '-' for stdout (default: '-')",
    )
    return parser.parse_args(argv)


def read_json(source: str) -> dict[str, Any]:
    if source == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(source).expanduser().read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BriefSpecError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise BriefSpecError("spec must be a JSON object")
    return value


def require_text(spec: dict[str, Any], field: str) -> str:
    value = spec.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BriefSpecError(f"{field} must be a non-empty string")
    return value.strip()


def require_text_list(spec: dict[str, Any], field: str) -> list[str]:
    value = spec.get(field)
    if not isinstance(value, list):
        raise BriefSpecError(f"{field} must be a JSON array")
    rendered: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise BriefSpecError(f"{field}[{index}] must be a non-empty string")
        rendered.append(item.strip())
    return rendered


def normalize_repo_paths(values: list[str], field: str) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = Path(value)
        if (
            candidate.is_absolute()
            or value in {"", "."}
            or ".." in candidate.parts
            or (candidate.parts and candidate.parts[0] == ".git")
        ):
            raise BriefSpecError(
                f"{field} must contain non-root repository-relative paths: {value!r}"
            )
        clean = candidate.as_posix().rstrip("/")
        if clean not in normalized:
            normalized.append(clean)
    return normalized


def markdown_list(values: list[str], *, empty: str = "none") -> str:
    if not values:
        return f"- {empty}"
    return "\n".join(f"- {value}" for value in values)


def numbered_list(values: list[str]) -> str:
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values, 1))


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_FIELDS if field not in spec]
    if missing:
        raise BriefSpecError("missing required fields: " + ", ".join(missing))

    repository_root = Path(require_text(spec, "repository_root")).expanduser()
    if not repository_root.is_absolute():
        raise BriefSpecError("repository_root must be an absolute path")

    plan = require_text_list(spec, "plan")
    criteria = require_text_list(spec, "acceptance_criteria")
    if not plan:
        raise BriefSpecError("plan must contain at least one step")
    if not criteria:
        raise BriefSpecError("acceptance_criteria must contain at least one item")

    allow_paths = normalize_repo_paths(require_text_list(spec, "allow_paths"), "allow_paths")
    track_paths = normalize_repo_paths(require_text_list(spec, "track_paths"), "track_paths")
    effective_scope = list(dict.fromkeys([*allow_paths, *track_paths]))
    packet = spec.get("context_packet", {})
    if not isinstance(packet, dict):
        raise BriefSpecError("context_packet must be a JSON object")
    packet_fields = ("decisions", "failed_attempts", "evidence", "versions")
    if set(packet) - set(packet_fields):
        raise BriefSpecError("unknown context_packet fields")
    packet = {field: require_text_list(packet, field) for field in packet_fields if field in packet}
    return {
        "objective": require_text(spec, "objective"),
        "repository_root": str(repository_root),
        "current_state": require_text(spec, "current_state"),
        "pre_existing_changes": require_text_list(spec, "pre_existing_changes"),
        "plan": plan,
        "allow_paths": allow_paths,
        "track_paths": track_paths,
        "effective_scope": effective_scope,
        "development_checks": require_text_list(spec, "development_checks"),
        "acceptance_criteria": criteria,
        "context_packet": packet,
    }


def render_brief(spec: dict[str, Any]) -> str:
    """Validate *spec* and return a complete, runner-compatible Markdown brief."""
    values = validate_spec(spec)
    packet = ""
    if values["context_packet"]:
        packet = "\n# Context to preserve\n\n" + "\n\n".join(
            field.replace("_", " ").capitalize() + ":\n" + markdown_list(entries)
            for field, entries in values["context_packet"].items()
        ) + "\n"
    return f"""# Objective

{values['objective']}

# Repository and current state

- Repository root: {values['repository_root']}
- Relevant implementation already present: {values['current_state']}
- Pre-existing working-tree changes to preserve:
{markdown_list(values['pre_existing_changes'])}
{packet}

# Approved plan

{numbered_list(values['plan'])}

# Allowed scope

- May modify or create only these effective paths (tracked deliverables included):
{markdown_list(values['effective_scope'], empty='none; this is a read-only brief')}
- Runner `--allow-path` prefixes:
{markdown_list(values['allow_paths'])}
- Runner `--track-path` deliverables:
{markdown_list(values['track_paths'])}
- May run the focused development checks below and ordinary inspection or tooling needed for the approved plan.

# Mutation budget

- Git history, branch, stash, and worktrees: no changes.
- External systems and publication: no writes unless the user explicitly authorized an exact destination and payload.
- Destructive actions: none unless the approved objective explicitly requires and identifies them.
- Secrets and unrelated private files: no access.

# Constraints

{markdown_list(list(COMMON_CONSTRAINTS))}

# Development checks

Use these focused commands while implementing and address failures caused by this task:

{numbered_list(values['development_checks']) if values['development_checks'] else 'No development checks were supplied; explain why in the final report.'}

The conductor runs authoritative final-state gates separately with runner `--verify-command` values after this executor exits. Do not claim that those runner-owned commands passed.

# Acceptance criteria

{markdown_list(values['acceptance_criteria'])}

# Final response

Return the compact execution report using exactly these five headings, exactly once and in this order:

### STATUS

Put exactly one of `COMPLETE`, `BLOCKED`, or `FAILED` on the first non-empty line.

### FILES CHANGED

### COMMANDS RUN

### VERIFICATION

### RISKS OR BLOCKERS

Do not add alternative headings such as `SUMMARY`, `CHANGES`, or `RISKS`. Do not paste complete files or raw logs.
""".rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rendered = render_brief(read_json(args.spec))
    except (BriefSpecError, OSError) as exc:
        print(f"brief renderer error: {exc}", file=sys.stderr)
        return 2
    if args.output == "-":
        sys.stdout.write(rendered)
    else:
        destination = Path(args.output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
