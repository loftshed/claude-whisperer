#!/usr/bin/env python3
"""Ask a more capable model one focused question: `run_agent.py consult`.

A consultation is not a delegation. The consultant receives the question, the context and any
attached files inline, answers once, and holds no permission to edit files, publish, or take
actions. A plain question runs in an empty private workspace; an investigation or review may read
one repository with `--read`. Either way every engine runs in its no-write mode, and only Claude
Code and OpenCode can be held to no tools at all. Implementation delegation keeps the runner's own
workflow, with scope, audit and verification.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

RESULT_SCHEMA = "agent-executor.consultation.v1"
REQUEST_SCHEMA = "agent-executor.consultation-request.v1"
DEFAULT_ROUTE = "consultation"
INTENTS = ("question", "investigate", "review")
# A question is one model turn; reading a repository takes several.
DEFAULT_TIMEOUTS = {"question": "10m", "investigate": "20m", "review": "20m"}
MAX_PROMPT_BYTES = 200 * 1024
MAX_ATTACHMENT_BYTES = 128 * 1024
MAX_DIFF_BYTES = 128 * 1024
TRANSPORTS = ("auto", "native", "executor")
HOSTS = ("claude", "codex", "agy", "opencode")
EXIT_BAD_ARGUMENTS = 4
EXIT_FAILED = 1
EXIT_EMPTY_OUTPUT = 3
EXIT_TIMED_OUT = 12
EXIT_LEASE_HELD = 27
# File names and directories that hold credentials far more often than context. The caller pastes
# redacted text through --context instead; the consultant's provider must never receive a secret.
SECRET_NAME_PATTERNS = (
    r"^\.env(\..*)?$",
    r"\.(pem|key|p12|pfx|jks|keystore)$",
    r"^id_(rsa|dsa|ecdsa|ed25519)(\..*)?$",
    r"credential",
    r"secret",
    r"^\.(netrc|npmrc|pypirc)$",
)
SECRET_DIRECTORY_NAMES = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})
OPENCODE_MESSAGE = "Answer the attached consultation and end with its required headings."
# OpenCode tools by name. Everything that acts is disabled for every consultation; the read tools stay
# only with --read. Verified against OpenCode 2.0.30 on a private server: the consultant then lists
# glob, grep and read and nothing else.
OPENCODE_READ_TOOLS = ("read", "glob", "grep", "list")
OPENCODE_ACTING_TOOLS = (
    "bash",
    "shell",
    "write",
    "edit",
    "patch",
    "multiedit",
    "execute",
    "webfetch",
    "websearch",
    "task",
    "subagent",
    "todowrite",
    "todoread",
    "skill",
    "question",
)
# What each engine can do while answering. None of these grants a write, an approval or a publish;
# only Claude Code can be held to no tools at all, the others keep their read tools.
TOOL_POLICIES = {
    "question": {
        "codex": "codex exec in an empty workspace with sandbox_mode=read-only, approval_policy=never and MCP/plugins disabled: "
        "no writes; read tools and read-only shell remain",
        "claude": "claude -p with --tools '' (no built-in tools) and --strict-mcp-config (no MCP servers)",
        "agy": "agy --mode plan in an empty workspace without --dangerously-skip-permissions: no writes; read tools remain",
        "opencode": "opencode run on a private server with every tool and MCP server disabled: no tools at all",
    },
    "read": {
        "codex": "codex exec in the repository with sandbox_mode=read-only, approval_policy=never and MCP/plugins disabled: "
        "read tools and read-only shell, no writes",
        "claude": "claude -p in the repository with --tools Read,Grep,Glob and --strict-mcp-config: read tools only",
        "agy": "agy --mode plan in the repository without --dangerously-skip-permissions: read tools, no writes",
        "opencode": "opencode run in the repository on a private server with only glob, grep and read enabled and no MCP servers",
    },
}
NATIVE_HOWTO = {
    "claude": (
        "Claude Code: spawn one native subagent with the Agent tool (model {model}, a read-only prompt or "
        "a read-only agent type) carrying the prompt file's contents, then read its sections. "
        "A follow-up goes to the same agent with SendMessage."
    ),
    "codex": "Codex: start one native sub-agent thread on {model} with the prompt file's contents, read-only.",
    "agy": "Antigravity: invoke_subagent on {model} with the prompt file's contents, read-only.",
    "opencode": "OpenCode: a native sub-agent using the read-only plan agent on {model} with the prompt file's contents.",
}
# The headings each intent's reply ends with, in order. The first is the main section.
# Which engines can run a read-only command while reading a repository. Claude Code gets file tools
# only; Antigravity's headless plan mode denies the command and ends the turn empty; OpenCode's shell
# is disabled by configuration.
COMMANDS_AVAILABLE = {"codex": True, "claude": False, "agy": False, "opencode": False}
INTENT_HEADINGS = {
    "question": ("ANSWER", "UNCERTAINTY"),
    "investigate": ("FINDINGS", "EVIDENCE", "UNCERTAINTY"),
    "review": ("FINDINGS", "EVIDENCE", "UNCERTAINTY"),
}
_HEADING = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*|__)?(ANSWER|FINDINGS|EVIDENCE|UNCERTAINTY)(?:\*\*|__)?\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_FENCE = re.compile(r"(?ms)^([`~]{3,})[^\n]*\n.*?^\1[ \t]*$")


def _runner() -> Any:
    module = globals().get("_runner_module")
    if module is not None and hasattr(module, "RunnerError"):
        return module
    for key in ("run_agent", "agent_executor_run_agent", "__main__"):
        candidate = sys.modules.get(key)
        if candidate is not None and hasattr(candidate, "RunnerError"):
            return candidate
    spec = importlib.util.spec_from_file_location("agent_executor_run_agent", Path(__file__).with_name("run_agent.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    globals()["_runner_module"] = module
    return module


def _error(message: str, exit_code: int = EXIT_BAD_ARGUMENTS) -> Exception:
    return _runner().RunnerError(message, exit_code)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} consult",
        description=(
            "Ask a more capable model one focused question and get one answer, the model that answered, "
            "and its material uncertainty. The consultant can change nothing."
        ),
    )
    parser.add_argument("--question", help="The question: text, @FILE, or '-' for stdin")
    parser.add_argument(
        "--intent",
        choices=INTENTS,
        help="question (default): answer from what is sent. investigate / review: read one repository (--read) "
        "and return findings with evidence",
    )
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        help="Context the answer needs: text or @FILE; repeatable, joined in order",
    )
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        type=Path,
        help=f"Text file to include verbatim (at most {MAX_ATTACHMENT_BYTES // 1024} KiB each); repeatable",
    )
    parser.add_argument(
        "--read",
        type=Path,
        metavar="REPO",
        help="Git repository the consultant may read (never write); required for investigate and review",
    )
    parser.add_argument(
        "--diff",
        nargs="?",
        const="HEAD",
        metavar="REF",
        help="Capture `git status --short` and `git diff REF` (default HEAD) from --read into the prompt",
    )
    parser.add_argument("--model", help="Model preference: exact ID, family selector (gpt:sol, claude:best) or alias")
    parser.add_argument("--engine", choices=HOSTS, help="Executor CLI; inferred from the model when omitted")
    parser.add_argument("--effort", help="Reasoning effort where the engine takes one")
    parser.add_argument("--variant", help="OpenCode model variant")
    parser.add_argument(
        "--route",
        help=f"Role from the route config that fills in engine, model and effort (default: {DEFAULT_ROUTE})",
    )
    parser.add_argument(
        "--claude-config-dir",
        type=Path,
        help="Claude account profile for --engine claude; choosing a profile chooses its billing",
    )
    parser.add_argument("--billing-mode", choices=("unknown", "subscription", "api"), default="unknown")
    parser.add_argument("--ignore-quota", action="store_true", help="Ask even when monitor reports the pool exhausted")
    parser.add_argument("--refresh-models", action="store_true", help="Bypass the model catalog cache")
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default="auto",
        help="auto hands a model native to the calling harness back to its native tools; executor always runs the CLI",
    )
    parser.add_argument("--host", choices=HOSTS, help="The calling harness, when the environment does not show it")
    parser.add_argument("--follow-up", type=Path, help="A previous consultation result.json to continue in its session")
    parser.add_argument(
        "--timeout",
        help=f"Executor duration such as 10m (default {DEFAULT_TIMEOUTS['question']} for a question, "
        f"{DEFAULT_TIMEOUTS['investigate']} with --read)",
    )
    parser.add_argument("--out-dir", type=Path, help="Artifact directory (default: under the agent-executor cache)")
    parser.add_argument("--detach", action="store_true", help="Ask in the background and return the completion event")
    parser.add_argument(
        "--notify", choices=("none", "desktop"), help="Detached completion notification (default desktop)"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Summary output format")
    parser.add_argument(
        "--dry-run", action="store_true", help="Resolve everything and print the command; no model call"
    )
    parser.add_argument("--request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--completion-event", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def read_text_argument(value: str, *, option: str) -> str:
    if value == "-":
        text = sys.stdin.read()
    elif value.startswith("@"):
        path = Path(value[1:]).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _error(f"{option} file cannot be read: {exc}") from exc
    else:
        text = value
    if not text.strip():
        raise _error(f"{option} is empty")
    return text.strip()


def is_secret_path(path: Path) -> bool:
    """A credential-shaped name or directory, on the path as given and on what it resolves to."""
    candidates = [path]
    with contextlib.suppress(OSError):
        candidates.append(path.resolve())
    for candidate in candidates:
        if any(re.search(pattern, candidate.name.lower()) for pattern in SECRET_NAME_PATTERNS):
            return True
        if any(part in SECRET_DIRECTORY_NAMES for part in candidate.parts[:-1]):
            return True
    return False


def read_attachment(path: Path, *, repository: Path | None) -> dict[str, Any]:
    given = path.expanduser()
    if is_secret_path(given):
        raise _error(
            f"--attach refuses {given}: its name or directory says it holds credentials. "
            "Pass redacted text through --context instead; the prompt goes to the model provider."
        )
    try:
        data = given.read_bytes()
    except OSError as exc:
        raise _error(f"--attach file cannot be read: {exc}") from exc
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise _error(
            f"--attach {given} is {len(data) // 1024} KiB; keep each attachment under "
            f"{MAX_ATTACHMENT_BYTES // 1024} KiB and quote the relevant part through --context"
        )
    if b"\x00" in data[:8192]:
        raise _error(f"--attach {given} looks binary; attachments are inlined as text")
    resolved = given.resolve()
    label = given.name
    if repository is not None and (resolved == repository or repository in resolved.parents):
        label = resolved.relative_to(repository).as_posix()
    return {"name": label, "path": str(resolved), "bytes": len(data), "text": data.decode("utf-8", "replace")}


def fence_for(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def capture_diff(repository: Path, ref: str) -> dict[str, Any]:
    runner = _runner()
    status = runner.git_output(repository, "status", "--short", "--untracked-files=all").decode("utf-8", "replace")
    diff = runner.git_output(repository, "diff", "--no-color", ref, "--").decode("utf-8", "replace")
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        raise _error(
            f"the diff against {ref} is {len(diff.encode('utf-8')) // 1024} KiB; keep it under {MAX_DIFF_BYTES // 1024} KiB "
            "with a narrower ref, or attach the relevant hunks"
        )
    return {"ref": ref, "status": status.strip(), "diff": diff.rstrip("\n")}


def _intent_rules(request: dict[str, Any]) -> list[str]:
    intent = request["intent"]
    repository = request.get("read")
    if intent == "question":
        return [
            "You are a trusted specialist consultant brought in for your sharp judgment on one focused question for a "
            "colleague agent. Take a clear, rigorous stance—your candid analysis and honest flagging of uncertainty "
            "are equally valued. You have no permission to edit files, run commands with side effects, publish "
            "anything, or take actions: answer from the question, context and attachments below. Do not ask the "
            "caller to run anything for you.",
        ]
    # Only Codex gets a read-only shell. Claude Code has file tools only, and Antigravity's headless plan
    # mode denies a command and then ends the turn with no answer, so the prompt must not invite one.
    access = (
        "You may read its files and run read-only commands there."
        if COMMANDS_AVAILABLE.get(request["engine"])
        else "You may read its files. You cannot run commands: do not try, answer from what you read."
    )
    if intent == "investigate":
        return [
            f"You are a trusted root-cause specialist investigating why something happens in the repository at {repository}. {access} "
            "You cannot change anything and must not try. Trust your diagnostic instincts, follow the evidence wherever "
            "it leads, revise hypotheses as you go, and distinguish what you observed from what you infer.",
        ]
    return [
        f"You are a trusted senior reviewer examining a change in the repository at {repository}. {access} "
        "You cannot change anything and must not try: report findings, do not fix them. "
        "Bring a constructive, rigorous eye—judge correctness first, then safety and clarity; cite the file and line for every finding.",
    ]


def _intent_contract(intent: str) -> list[str]:
    if intent == "question":
        return [
            "- Lead with the answer; make it concrete and specific to the material given.",
            "- Say what you assumed and what you could not verify.",
            "- If the material is insufficient, say exactly what is missing instead of guessing.",
            "- End with exactly these two headings, in this order:",
            "",
            "### ANSWER",
            "",
            "The answer.",
            "",
            "### UNCERTAINTY",
            "",
            "Material uncertainty: assumptions, unverified claims, and what evidence would change the answer; or `none`.",
        ]
    findings = (
        "What happens and why, most likely cause first; for each cause say how confident you are."
        if intent == "investigate"
        else "One finding per item, most severe first: severity (high, medium, low), file and line, what is wrong, "
        "and a concrete fix. Say `none` when the change is clean."
    )
    return [
        "- Report only what the evidence supports; mark inference as inference.",
        "- Quote evidence briefly, with file paths and line numbers, or the exact command and its output.",
        "- End with exactly these three headings, in this order:",
        "",
        "### FINDINGS",
        "",
        findings,
        "",
        "### EVIDENCE",
        "",
        "The observations the findings rest on: file:line references and command output, quoted briefly.",
        "",
        "### UNCERTAINTY",
        "",
        "What you could not confirm, what you did not check, and what would settle it; or `none`.",
    ]


def render_prompt(request: dict[str, Any]) -> str:
    intent = request["intent"]
    follow_up = request.get("follow_up_of") is not None
    title = {"question": "Consultation", "investigate": "Investigation", "review": "Review"}[intent]
    parts = [f"# Follow-up to the {title.lower()}" if follow_up else f"# {title}", ""]
    if follow_up:
        parts.append("This continues the conversation above with one more question. The same rules apply.")
    else:
        parts.extend(_intent_rules(request))
    parts += ["", "## Question", "", request["question"]]
    if request.get("context"):
        parts += ["", "## Context", "", request["context"]]
    change = request.get("change")
    if change:
        parts += ["", f"## Change under review (git diff {change['ref']})", ""]
        parts += ["Working tree status:", "", "```", change["status"] or "(clean)", "```", ""]
        fence = fence_for(change["diff"])
        parts += [fence, change["diff"] or "(no diff)", fence]
    if request.get("attachments"):
        parts += ["", "## Attachments"]
        for attachment in request["attachments"]:
            fence = fence_for(attachment["text"])
            parts += ["", f"### {attachment['name']}", "", fence, attachment["text"].rstrip("\n"), fence]
    parts += ["", "## Response contract", "", *_intent_contract(intent)]
    return "\n".join(parts) + "\n"


def detect_host() -> str | None:
    hosts = [host for host, keys in _runner().HOST_MARKERS.items() if any(os.environ.get(key) for key in keys)]
    return hosts[0] if hosts else None


def parse_sections(text: str, intent: str) -> tuple[dict[str, str], str]:
    """The reply's sections by heading, and the shape: structured, partial, or unstructured.

    Headings inside fenced code blocks are ignored, so a reply quoting the contract is parsed by its real
    headings. The main section's last occurrence wins; a missing main heading leaves the whole reply there.
    """
    expected = INTENT_HEADINGS[intent]
    masked = _FENCE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)
    headings = [(m.group(1).upper(), m.start(), m.end()) for m in _HEADING.finditer(masked)]
    main_index = next((i for i in range(len(headings) - 1, -1, -1) if headings[i][0] == expected[0]), None)
    if main_index is None:
        return {expected[0]: text.strip()}, "unstructured"
    sections: dict[str, str] = {}
    relevant = headings[main_index:]
    for position, (name, _start, end) in enumerate(relevant):
        if name in sections:
            continue
        next_start = relevant[position + 1][1] if position + 1 < len(relevant) else len(text)
        sections[name] = text[end:next_start].strip()
    shape = "structured" if all(name in sections for name in expected) else "partial"
    return {name: sections[name] for name in expected if name in sections}, shape


def load_follow_up(path: Path) -> dict[str, Any]:
    try:
        previous = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _error(f"--follow-up result cannot be read: {exc}") from exc
    if not isinstance(previous, dict) or previous.get("schema") != RESULT_SCHEMA:
        raise _error("--follow-up must name a consultation result.json")
    # An empty turn (Antigravity after a denied action) still has a session worth nudging.
    if previous.get("status") not in ("answered", "empty_output"):
        raise _error(f"--follow-up result was not answered (status {previous.get('status')}); ask a fresh question")
    if previous.get("transport") != "executor" or not previous.get("session_id"):
        raise _error("--follow-up needs an executor consultation with a session; a native one continues natively")
    return previous


def resolve_route(args: argparse.Namespace, previous: dict[str, Any] | None) -> dict[str, Any]:
    """Engine, model, effort and variant: pinned by a follow-up, else explicit, else inferred, else the route."""
    runner = _runner()
    if previous is not None:
        if args.engine and args.engine != previous["engine"]:
            raise _error(f"a follow-up stays on {previous['engine']}; ask a fresh question to change the engine")
        if args.model and args.model not in (previous["model"], previous.get("requested_model")):
            raise _error(
                f"a follow-up stays on {previous['model']}, the model that answered; ask a fresh question to change it"
            )
        effort = previous.get("effort") or {}
        if args.effort and args.effort not in (effort.get("requested"), effort.get("bound")):
            raise _error(
                f"a follow-up stays on effort {effort.get('bound') or effort.get('requested')}; ask a fresh question to change it"
            )
        if args.variant and args.variant != previous.get("variant"):
            raise _error(f"a follow-up stays on variant {previous.get('variant')}; ask a fresh question to change it")
        config_dir = previous.get("claude_config_dir")
        if args.claude_config_dir and str(args.claude_config_dir.expanduser()) != (config_dir or ""):
            raise _error("a follow-up stays on the profile that answered; it cannot change the billing account")
        return {
            "engine": previous["engine"],
            "model": previous["model"],
            "effort": effort.get("requested"),
            "variant": previous.get("variant"),
            "route": None,
            "claude_config_dir": config_dir,
        }
    routes = runner.routes_module().load_routes()
    route_name = args.route
    if route_name is None and DEFAULT_ROUTE in routes:
        route_name = DEFAULT_ROUTE
    if route_name is not None and route_name not in routes:
        raise _error(f"unknown --route {route_name!r}; configured: {', '.join(sorted(routes))}")
    engine = args.engine
    if args.model and not engine:
        families = runner.load_model_families()
        parsed = runner.families_module().parse_selector(args.model, families)
        engine = families[parsed[0]]["engine"] if parsed else runner.native_engine_for(args.model)
        if engine is None:
            raise _error(f"cannot tell which engine runs {args.model!r}; pass --engine")
    namespace = argparse.Namespace(
        route=route_name, engine=engine, model=args.model, effort=args.effort, variant=args.variant
    )
    if engine and route_name:
        # An explicit model or engine that the role does not cover still gets that engine's own defaults.
        namespace.route = route_name if routes[route_name]["engine"] == engine else None
    resolved = runner.resolve_route(namespace)
    if resolved.engine != "opencode" and resolved.variant:
        raise _error("--variant is only valid with --engine opencode")
    return {
        "engine": resolved.engine,
        "model": resolved.model or runner.preferred_model(resolved.engine),
        "effort": resolved.effort,
        "variant": resolved.variant,
        "route": resolved.resolved_route,
        "claude_config_dir": str(args.claude_config_dir.expanduser()) if args.claude_config_dir else None,
    }


def decide_transport(args: argparse.Namespace, engine: str, host: str | None, previous: dict[str, Any] | None) -> str:
    if previous is not None:
        if args.transport == "native":
            raise _error("a follow-up continues the executor session that answered; it cannot move to native tools")
        return "executor"
    if args.transport == "native":
        if host != engine:
            raise _error(
                f"--transport native needs the calling harness to run {engine} models itself (host: {host or 'unknown'})"
            )
        return "native"
    if args.transport == "executor":
        return "executor"
    # auto: a model the caller's own harness runs goes to its native tools, unless the caller chose
    # another billing boundary (a Claude profile) that native tools cannot switch to.
    if host == engine and not (engine == "claude" and args.claude_config_dir):
        return "native"
    return "executor"


def resolve_reading(args: argparse.Namespace, previous: dict[str, Any] | None) -> tuple[str, str | None]:
    """(intent, repository root or None), inherited and enforced across a follow-up."""
    runner = _runner()
    repository: str | None = None
    if args.read is not None:
        cwd = args.read.expanduser().resolve()
        if not cwd.is_dir():
            raise _error(f"--read directory does not exist: {cwd}")
        repository = str(runner.git_root(cwd))
    intent = args.intent
    if previous is not None:
        if intent and intent != previous.get("intent"):
            raise _error(f"a follow-up keeps the {previous.get('intent')} intent; ask a fresh question to change it")
        if repository and repository != previous.get("read"):
            raise _error("a follow-up keeps the repository it read; ask a fresh question for another one")
        return previous.get("intent") or "question", previous.get("read")
    intent = intent or ("question" if repository is None else "investigate")
    if intent != "question" and repository is None:
        raise _error(f"--intent {intent} needs --read <repo>: the consultant must be able to look at it")
    if args.diff is not None and repository is None:
        raise _error("--diff needs --read <repo>")
    return intent, repository


def build_request(args: argparse.Namespace) -> dict[str, Any]:
    runner = _runner()
    if args.request is not None:
        try:
            request = json.loads(args.request.expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _error(f"--request cannot be read: {exc}") from exc
        if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA:
            raise _error("--request is not a consultation request")
        return request
    if not args.question:
        raise _error("--question is required")
    previous = load_follow_up(args.follow_up) if args.follow_up else None
    intent, repository = resolve_reading(args, previous)
    question = read_text_argument(args.question, option="--question")
    context = "\n\n".join(read_text_argument(value, option="--context") for value in args.context)
    repository_path = Path(repository) if repository else None
    attachments = [read_attachment(path, repository=repository_path) for path in args.attach]
    change = capture_diff(repository_path, args.diff) if args.diff is not None and repository_path else None
    route = resolve_route(args, previous)
    host = args.host or detect_host()
    transport = decide_transport(args, route["engine"], host, previous)
    if transport == "native" and args.detach:
        raise _error("--detach runs the executor; a native consultation is asynchronous through the host's own tools")
    timeout = args.timeout or DEFAULT_TIMEOUTS[intent]
    runner.parse_duration(timeout)
    request = {
        "schema": REQUEST_SCHEMA,
        "created_at": runner.utc_now(),
        "intent": intent,
        "read": repository,
        "question": question,
        "context": context,
        "change": change,
        "attachments": attachments,
        **route,
        "requested_model": args.model,
        "billing_mode": args.billing_mode,
        "ignore_quota": args.ignore_quota,
        "refresh_models": args.refresh_models,
        "transport": transport,
        "host": host,
        "timeout": timeout,
        "follow_up_of": str(args.follow_up.expanduser().resolve()) if args.follow_up else None,
        "previous": (
            {
                "session_id": previous["session_id"],
                "workspace": previous.get("workspace"),
                "usage_cumulative": (previous.get("usage") or {}).get("cumulative"),
            }
            if previous
            else None
        ),
    }
    prompt_size = len(render_prompt(request).encode("utf-8"))
    if prompt_size > MAX_PROMPT_BYTES:
        raise _error(
            f"the consultation prompt is {prompt_size // 1024} KiB; keep it under {MAX_PROMPT_BYTES // 1024} KiB "
            "by quoting only the relevant parts"
        )
    return request


def prepare_consult_dir(requested: Path | None, repository: str | None = None) -> Path:
    """The artifact directory, never inside the repository being read: artifacts are not evidence."""
    runner = _runner()
    read_root = Path(repository).resolve() if repository else None

    def outside_repository(candidate: Path, option: str) -> None:
        if read_root is not None and (candidate == read_root or read_root in candidate.parents):
            raise _error(f"{option} must be outside the --read repository: {candidate}")

    if requested is not None:
        directory = requested.expanduser().resolve()
        outside_repository(directory, "--out-dir")
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        directory.chmod(0o700)
        return directory
    base = runner.default_agent_cache_dir().resolve() / "consults-v1"
    outside_repository(base, "the agent-executor cache")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = base / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def codex_mcp_server_names(workspace: Path) -> list[str]:
    """Configured Codex MCP server names across user and repository config files."""
    import tomllib

    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    paths = [home / "config.toml"]
    with contextlib.suppress(OSError):
        resolved = workspace.resolve()
        paths.extend(directory / ".codex/config.toml" for directory in (resolved, *resolved.parents))
    names: set[str] = set()
    for path in paths:
        with contextlib.suppress(OSError, ValueError):
            if not path.is_file():
                continue
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcp_servers")
            if isinstance(servers, dict):
                names.update(key for key in servers if isinstance(key, str))
            profiles = data.get("profiles")
            if isinstance(profiles, dict):
                for profile in profiles.values():
                    if isinstance(profile, dict) and isinstance(profile.get("mcp_servers"), dict):
                        names.update(key for key in profile["mcp_servers"] if isinstance(key, str))
    return sorted(names)


def consult_command(
    *,
    executable: str,
    engine: str,
    model: str,
    variant: str | None,
    workspace: Path,
    reading: bool,
    session_id: str | None,
    timeout: str,
    prompt_path: Path,
    final_output_path: Path,
    log_path: Path,
    effort: str | None,
    agy_stdin: bool,
    new_session_id: str | None,
    executor_version: str | None,
) -> list[str]:
    runner = _runner()
    if engine == "codex":
        command = [executable, "exec"]
        command += ["resume", session_id] if session_id else ["-C", str(workspace)]
        command += [
            "--skip-git-repo-check",
            "-c",
            'sandbox_mode="read-only"',
            "-c",
            'approval_policy="never"',
            "-c",
            "features.plugins=false",
            "-c",
            "features.apps=false",
        ]
        mcp_names = codex_mcp_server_names(workspace)
        if mcp_names:
            entries = ",".join(f"{json.dumps(name)}={{enabled=false}}" for name in mcp_names)
            command += ["-c", f"mcp_servers={{{entries}}}"]
        if effort is not None:
            command += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        command += ["--model", model, "--json", "--output-last-message", str(final_output_path), "-"]
        return command
    if engine == "claude":
        tools = "Read,Grep,Glob" if reading else ""
        command = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            tools,
            "--strict-mcp-config",
        ]
        if effort is not None:
            command += ["--effort", effort]
        if session_id:
            command += ["--resume", session_id]
        elif new_session_id:
            command += ["--session-id", new_session_id]
        return command
    if engine == "agy":
        command = [executable]
        if session_id:
            command += ["--conversation", session_id]
        else:
            command += ["--new-project"]
            if reading:
                command += ["--add-dir", str(workspace)]
        command += [
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--mode",
            "plan",
            "--print-timeout",
            runner.format_duration(runner.parse_duration(timeout) + 90),
            "--log-file",
            str(log_path),
        ]
        if agy_stdin:
            command += ["--input-format", "stream-json", "--print="]
        else:
            command.append(f"--print={prompt_path.read_text(encoding='utf-8')}")
        return command
    version = executor_version or runner.get_cached_cli_version(executable, runner.executor_environment("opencode"))
    command = [executable, "run"]
    if session_id:
        command += ["--session", session_id]
    if runner.opencode_major_version(version) >= 2:
        # A private server honors this run's configuration; the shared service would ignore it.
        command += ["--standalone", "--model", f"{model}#{variant}" if variant else model]
    else:
        command += [
            "--model",
            model,
            "--variant",
            variant or runner.DEFAULT_OPENCODE_VARIANT,
            "--pure",
            "--dir",
            str(workspace),
        ]
    command += ["--agent", "plan", "--format", "json", OPENCODE_MESSAGE, "--file", str(prompt_path)]
    return command


def consult_environment(
    engine: str, workspace: Path, claude_config_dir: str | None, session_id: str | None, *, reading: bool
) -> dict[str, str]:
    environment = _runner().executor_environment(engine, workspace, claude_config_dir, session_id=session_id)
    if engine == "opencode":
        # This configuration only reaches a private server (`run --standalone` on v2, `--pure` on v1); the
        # shared background service keeps the config it started with. Reading keeps glob, grep and read;
        # a question has no tools. No MCP servers, and the repository's own config is not loaded.
        tools = dict.fromkeys(OPENCODE_ACTING_TOOLS, False)
        if not reading:
            tools.update(dict.fromkeys(OPENCODE_READ_TOOLS, False))
        permission = {"edit": "deny", "bash": "deny", "webfetch": "deny", "external_directory": "deny"}
        environment["OPENCODE_PERMISSION"] = json.dumps(permission)
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {"tools": tools, "permission": permission, "mcp": {}, "share": "disabled", "autoupdate": False}
        )
        environment["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    return environment


def agy_denied_actions(stdout_text: str) -> list[str]:
    """Actions Antigravity's headless mode auto-denied, from its result event (`denied_actions`)."""
    denied: list[str] = []
    for event in _runner().bundled_module("usage").agy_events(stdout_text):
        if event.get("type") != "result":
            continue
        for item in event.get("denied_actions") or []:
            name = item.get("display_name") or item.get("action") if isinstance(item, dict) else item
            if isinstance(name, str) and name:
                denied.append(name)
    return list(dict.fromkeys(denied))


def observed_models(engine: str, stdout_text: str, usage: dict[str, Any]) -> list[str]:
    """Model IDs the provider itself reported, when it reported any; never the dispatched request."""
    observed = [
        item["model"] for item in usage.get("model_observations", []) if isinstance(item, dict) and item.get("model")
    ]
    if engine == "claude":
        for event in reversed(_runner().bundled_module("usage").claude_events(stdout_text)):
            if event.get("type") == "result" and isinstance(event.get("modelUsage"), dict):
                observed.extend(key for key in event["modelUsage"] if isinstance(key, str))
                break
    return list(dict.fromkeys(observed))


def print_summary(result: dict[str, Any], *, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return
    print(f"AGENT_CONSULT_STATUS={result['status']}")
    print(f"AGENT_INTENT={result['intent']}")
    print(f"AGENT_TRANSPORT={result['transport']}")
    print(f"AGENT_ENGINE={result['engine']}")
    print(f"AGENT_MODEL={result['model']}")
    if result.get("model_observed"):
        print(f"AGENT_MODEL_OBSERVED={','.join(result['model_observed'])}")
    if result.get("session_id"):
        print(f"AGENT_SESSION={result['session_id']}")
    if result.get("prompt_path"):
        print(f"AGENT_PROMPT={result['prompt_path']}")
    if (result.get("repository_delta") or {}).get("changed_paths"):
        print(
            f"AGENT_NOTE=repository_changed_during_consultation paths={json.dumps(result['repository_delta']['changed_paths'])}"
        )
    print(f"AGENT_RESULT={result['result_path']}")
    if result["status"] == "native_handoff":
        print(result["native_instructions"])
        return
    if result.get("error"):
        print(f"AGENT_ERROR={result['error']}")
    if result["status"] != "answered":
        return
    for heading in INTENT_HEADINGS[result["intent"]]:
        print(f"--- {heading} ---")
        value = (result.get("sections") or {}).get(heading)
        print(value if value is not None else "(the consultant gave no such section)")
    print("--- END ---")


def finish(
    result: dict[str, Any],
    *,
    result_path: Path,
    completion_event: Path | None,
    notification_mode: str,
    output_format: str,
) -> int:
    runner = _runner()
    result["result_path"] = str(result_path)
    result.setdefault("usage", runner.bundled_module("usage").empty())
    runner.attach_tariff_estimate(result)
    runner.write_json_atomic(result_path, result)
    if completion_event is not None:
        runner.publish_completion_event(
            result=result,
            result_path=result_path,
            event_path=completion_event,
            notification_mode=notification_mode,
            completion_hook=None,
        )
    print_summary(result, output_format=output_format)
    return int(result["exit_code"])


def base_result(request: dict[str, Any], *, started_at: str) -> dict[str, Any]:
    runner = _runner()
    change = request.get("change")
    return {
        "schema": RESULT_SCHEMA,
        "runner_version": runner.RUNNER_VERSION,
        "kind": "consultation",
        "intent": request["intent"],
        "read": request.get("read"),
        "diff_ref": change["ref"] if change else None,
        "transport": request["transport"],
        "host": request["host"],
        "engine": request["engine"],
        "model": request["model"],
        "requested_model": request.get("requested_model"),
        "route": request.get("route"),
        "effort": {"requested": request.get("effort"), "bound": None, "binding": None, "observed": None},
        "variant": request.get("variant"),
        "billing_mode": request["billing_mode"],
        "claude_config_dir": request.get("claude_config_dir"),
        "timeout": request["timeout"],
        "question": request["question"],
        "context_chars": len(request.get("context") or ""),
        "attachments": [{k: a[k] for k in ("name", "path", "bytes")} for a in request.get("attachments", [])],
        "follow_up_of": request.get("follow_up_of"),
        "writes_allowed": False,
        # The completion event carries this, so `events --cwd <repo>` finds a detached reading consultation.
        "repository": request.get("read"),
        "started_at": started_at,
    }


def native_handoff(request: dict[str, Any], *, out_dir: Path | None, output_format: str) -> int:
    runner = _runner()
    started_at = runner.utc_now()
    directory = prepare_consult_dir(out_dir, request.get("read"))
    prompt_path = directory / "prompt.md"
    runner.write_private_text(prompt_path, render_prompt(request))
    host = request["host"]
    headings = " and ".join(INTENT_HEADINGS[request["intent"]])
    result = {
        **base_result(request, started_at=started_at),
        "status": "native_handoff",
        "task_outcome": "not_run",
        "exit_code": 0,
        "prompt_path": str(prompt_path),
        "tool_policy": "the host's native read-only tooling; no write permission is granted by this handoff",
        "native_instructions": (
            f"Ask natively: this {host} session runs {request['engine']} models itself, so use its own agent tools "
            f"instead of a CLI copy.\n{NATIVE_HOWTO[host].format(model=request['model'])}\n"
            f"Report its {headings}, and the model the native tool used. "
            "To run the CLI anyway (another profile or billing boundary), pass --transport executor."
        ),
        "sections": None,
        "answer": None,
        "uncertainty": None,
        "answer_format": None,
        "session_id": None,
        "finished_at": runner.utc_now(),
    }
    return finish(
        result,
        result_path=directory / "result.json",
        completion_event=None,
        notification_mode="none",
        output_format=output_format,
    )


def preflight(request: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any], dict[str, Any], str | None]:
    """(executable, version, model, selection, catalog, effort, variant); the quota gate is separate."""
    runner = _runner()
    support = runner.bundled_module("execution_support")
    engine = request["engine"]
    executable, version, _available, model, selection, catalog = runner.executor_preflight(
        engine, request["model"], refresh=request.get("refresh_models", False)
    )
    try:
        effort = support.bind_effort(engine, model, request.get("effort"), catalog)
    except support.ContractError as exc:
        raise _error(str(exc)) from exc
    variant = request.get("variant")
    if engine == "opencode":
        variant = variant or runner.DEFAULT_OPENCODE_VARIANT
    elif variant:
        raise _error("--variant is only valid with --engine opencode")
    return executable, version, model, selection, catalog, effort, variant


def perform(
    request: dict[str, Any],
    *,
    out_dir: Path | None,
    completion_event: Path | None,
    notification_mode: str,
    output_format: str,
    dry_run: bool,
) -> int:
    runner = _runner()
    started_at = runner.utc_now()
    monotonic_start = time.monotonic()
    directory = prepare_consult_dir(out_dir, request.get("read"))
    result_path = directory / "result.json"
    try:
        return _perform(
            request,
            directory=directory,
            result_path=result_path,
            started_at=started_at,
            monotonic_start=monotonic_start,
            completion_event=completion_event,
            notification_mode=notification_mode,
            output_format=output_format,
            dry_run=dry_run,
        )
    except Exception as exc:
        if completion_event is None or result_path.exists():
            raise
        # A detached consultation must still publish its event, or the waiting caller times out.
        exit_code = getattr(exc, "exit_code", EXIT_FAILED)
        result = {
            **base_result(request, started_at=started_at),
            "status": "failed",
            "task_outcome": "failed",
            "exit_code": exit_code,
            "error": str(exc),
            "sections": None,
            "answer": None,
            "uncertainty": None,
            "answer_format": None,
            "session_id": None,
            "finished_at": runner.utc_now(),
        }
        finish(
            result,
            result_path=result_path,
            completion_event=completion_event,
            notification_mode=notification_mode,
            output_format=output_format,
        )
        raise


def _perform(
    request: dict[str, Any],
    *,
    directory: Path,
    result_path: Path,
    started_at: str,
    monotonic_start: float,
    completion_event: Path | None,
    notification_mode: str,
    output_format: str,
    dry_run: bool,
) -> int:
    runner = _runner()
    support = runner.bundled_module("execution_support")
    usage_module = runner.bundled_module("usage")
    engine = request["engine"]
    intent = request["intent"]
    executable, version, model, selection, catalog, effort, variant = preflight(request)
    if not dry_run:
        runner.quota_gate(
            engine, model, ignore=request.get("ignore_quota", False), claude_config_dir=request.get("claude_config_dir")
        )
    previous = request.get("previous") or {}
    session_id = previous.get("session_id")
    reading = request.get("read") is not None
    if reading:
        workspace = Path(request["read"])
        if not workspace.is_dir():
            raise _error(f"--read repository is gone: {workspace}")
    elif previous.get("workspace"):
        # A resumed session (Antigravity project, OpenCode session) keeps the directory it started in.
        workspace = Path(previous["workspace"])
        if not workspace.is_dir():
            raise _error(f"the workspace the session started in no longer exists: {workspace}; ask a fresh question")
    else:
        workspace = directory / "workspace"
        workspace.mkdir(mode=0o700)
    prompt_path = directory / "prompt.md"
    stdout_path = directory / "stdout.txt"
    stderr_path = directory / "stderr.txt"
    log_path = directory / "executor.log"
    final_output_path = directory / "final-output.txt"
    runner.write_private_text(prompt_path, render_prompt(request))
    runner.write_private_text(log_path, "")
    runner.write_private_text(final_output_path, "")
    new_session_id = str(uuid.uuid4()) if engine == "claude" and not session_id else None
    agy_stdin = engine == "agy" and runner.version_at_least(version, runner.AGY_STDIN_MIN_VERSION)
    timeout_seconds = runner.parse_duration(request["timeout"])
    command = consult_command(
        executable=executable,
        engine=engine,
        model=model,
        variant=variant,
        workspace=workspace,
        reading=reading,
        session_id=session_id,
        timeout=request["timeout"],
        prompt_path=prompt_path,
        final_output_path=final_output_path,
        log_path=log_path,
        effort=effort["bound"] if engine in ("codex", "claude") else None,
        agy_stdin=agy_stdin,
        new_session_id=new_session_id,
        executor_version=version,
    )
    result = {
        **base_result(request, started_at=started_at),
        "model": model,
        "model_preflight": runner.MODEL_PREFLIGHT[selection],
        "model_selection": selection,
        "model_catalog": {
            "source": catalog.get("source"),
            "discovered_at": catalog.get("discovered_at"),
            "cache": catalog.get("cache"),
        },
        "executor_version": version,
        "effort": effort,
        "variant": variant,
        "tool_policy": TOOL_POLICIES["read" if reading else "question"][engine],
        "workspace": str(workspace),
        "command": runner.command_preview(command),
        "prompt_path": str(prompt_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "log_path": str(log_path),
        "final_output_path": str(final_output_path),
    }
    if dry_run:
        result.update(
            status="dry_run",
            task_outcome="not_run",
            exit_code=0,
            sections=None,
            answer=None,
            uncertainty=None,
            answer_format=None,
            session_id=session_id,
            model_observed=[],
            finished_at=runner.utc_now(),
            duration_seconds=round(time.monotonic() - monotonic_start, 3),
        )
        return finish(
            result,
            result_path=result_path,
            completion_event=completion_event,
            notification_mode=notification_mode,
            output_format=output_format,
        )

    environment = consult_environment(
        engine, workspace, request.get("claude_config_dir"), session_id or new_session_id, reading=reading
    )
    stdin_path: Path | None = prompt_path if engine in ("codex", "claude") else None
    if agy_stdin:
        stdin_path = directory / "agy-input.jsonl"
        stdin_path.write_bytes(runner.agy_stream_input(prompt_path.read_text(encoding="utf-8")))
        stdin_path.chmod(0o600)
    runner.emit_lifecycle("consultant_started", engine=engine, model=model, intent=intent)
    stdin_handle: Any = stdin_path.open("rb") if stdin_path is not None else subprocess.DEVNULL
    repository_before = runner.git_state(workspace) if reading else None
    try:
        with contextlib.ExitStack() as stack:
            if reading:
                # Readers share the repository; a writer (an implementation run) holds it exclusively.
                try:
                    stack.enter_context(
                        support.workspace_lease(workspace, runner.default_agent_cache_dir(), read_only=True)
                    )
                except support.ContractError as exc:
                    raise _error(str(exc), EXIT_LEASE_HELD) from exc
            stdout_handle = stack.enter_context(runner.open_private_binary(stdout_path))
            stderr_handle = stack.enter_context(runner.open_private_binary(stderr_path))
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=environment,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            with runner.forward_termination_signals(process):
                timed_out = runner.wait_with_heartbeats(
                    process,
                    timeout_seconds=timeout_seconds + 60,
                    heartbeat_seconds=0,
                    monotonic_start=monotonic_start,
                )
    finally:
        if stdin_handle is not subprocess.DEVNULL:
            stdin_handle.close()
    for artifact in (stdout_path, stderr_path, log_path, final_output_path):
        if artifact.exists():
            artifact.chmod(0o600)
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    runner.emit_lifecycle("consultant_finished", exit_code=process.returncode)
    # Informational: the engine's no-write mode is what prevents edits; the conductor or the user may
    # legitimately edit a repository while a reader looks at it, so a change is reported, not judged.
    repository_delta = (
        runner.state_delta(repository_before, runner.git_state(workspace)) if repository_before is not None else None
    )

    provider_status: str | None = None
    if engine == "codex":
        text = final_output_path.read_text(encoding="utf-8", errors="replace") if final_output_path.exists() else ""
        observed_session = runner.parse_codex_session_id(stdout_text)
    elif engine == "opencode":
        text, observed_session = runner.parse_opencode_output(stdout_text)
    elif engine == "claude":
        text, observed_session, provider_status, _ = runner.parse_claude_output(stdout_text)
    else:
        text, observed_session, provider_status = runner.parse_agy_output(stdout_text)
        observed_session = observed_session or runner.parse_agy_session_id(log_path)
    if engine != "codex":
        final_output_path.write_text(text, encoding="utf-8")
    session_id = observed_session or session_id or new_session_id
    provider_error = runner.provider_terminal_error(engine, stdout_text)
    provider_failed = provider_error is not None or (
        engine in ("agy", "claude") and provider_status not in (None, "success", "unknown")
    )
    quota_structured = runner.structured_quota_signal(engine, stdout_text)
    denied_actions = agy_denied_actions(stdout_text) if engine == "agy" else []
    usage = usage_module.normalize_usage(
        engine, stdout_text, resumed=bool(previous), baseline=previous.get("usage_cumulative")
    )
    error: str | None = None
    if timed_out:
        status, exit_code = "timed_out", EXIT_TIMED_OUT
        error = f"the consultant did not answer within {request['timeout']}"
    elif (process.returncode != 0 or provider_failed or quota_structured) and (
        quota_structured or runner.provider_quota_exhausted(provider_error, stderr_text)
    ):
        status, exit_code = "provider_quota_exhausted", runner.EXIT_PROVIDER_QUOTA
        error = provider_error or "the provider reported exhausted quota or credits"
    elif process.returncode != 0 or provider_failed:
        status, exit_code = "failed", EXIT_FAILED
        error = provider_error or f"{engine} exited with {process.returncode}"
    elif not text.strip():
        status, exit_code = "empty_output", EXIT_EMPTY_OUTPUT
        error = "the consultant produced no answer"
        if denied_actions:
            error = (
                f"Antigravity ended the turn with no answer after denying {', '.join(denied_actions)}: the consultant "
                "tried an action its plan mode forbids. Follow up in this session asking it to answer from what it "
                "has read, or ask a question that needs no commands."
            )
    else:
        status, exit_code = "answered", 0
    sections, answer_format = parse_sections(text, intent) if status == "answered" else (None, None)
    headings = INTENT_HEADINGS[intent]
    result.update(
        status=status,
        task_outcome="answered" if status == "answered" else "failed",
        exit_code=exit_code,
        error=error,
        executor_exit_code=process.returncode,
        timed_out=timed_out,
        provider_error=provider_error,
        provider_terminal_status=provider_status,
        denied_actions=denied_actions,
        session_id=session_id,
        model_observed=observed_models(engine, stdout_text, usage),
        sections=sections,
        answer=(sections or {}).get(headings[0]),
        uncertainty=(sections or {}).get("UNCERTAINTY"),
        answer_format=answer_format,
        repository_delta=repository_delta,
        final_message=runner.bound_final_message(text.strip()),
        stderr_tail=[line for line in stderr_text.splitlines() if line][-20:],
        usage=usage,
        finished_at=runner.utc_now(),
        duration_seconds=round(time.monotonic() - monotonic_start, 3),
    )
    return finish(
        result,
        result_path=result_path,
        completion_event=completion_event,
        notification_mode=notification_mode,
        output_format=output_format,
    )


def launch_detached(request: dict[str, Any], args: argparse.Namespace) -> int:
    runner = _runner()
    if args.out_dir:
        raise _error("--out-dir cannot be combined with --detach; the job directory holds the run")
    if args.dry_run:
        raise _error("--dry-run cannot be combined with --detach")
    # Fail here, not in a completion event: catalog, effort, the native-route rule and quota. The resolved
    # model is what the background job runs, so a selector cannot move between now and then.
    _executable, _version, model, _selection, _catalog, effort, variant = preflight(request)
    runner.quota_gate(
        request["engine"],
        model,
        ignore=request.get("ignore_quota", False),
        claude_config_dir=request.get("claude_config_dir"),
    )
    request = {**request, "model": model, "effort": effort["requested"], "variant": variant}
    job_dir = prepare_consult_dir(None, request.get("read"))
    request_path = job_dir / "request.json"
    runner.write_private_text(request_path, json.dumps(request, indent=2, ensure_ascii=True) + "\n")
    run_dir = job_dir / "run"
    event_path = runner.prepare_completion_event_path()
    notification_mode = args.notify or "desktop"
    command = [
        sys.executable,
        str(Path(__file__).with_name("run_agent.py").resolve()),
        "consult",
        "--request",
        str(request_path),
        "--out-dir",
        str(run_dir),
        "--completion-event",
        str(event_path),
        "--notify",
        notification_mode,
        "--format",
        args.format,
    ]
    stdout_path = job_dir / "runner.stdout"
    stderr_path = job_dir / "runner.stderr"
    with (
        runner.open_private_binary(stdout_path) as stdout_handle,
        runner.open_private_binary(stderr_path) as stderr_handle,
    ):
        process = subprocess.Popen(
            command,
            cwd=job_dir,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    threading.Thread(target=process.wait, daemon=True).start()
    job = {
        "schema": "agent-executor.consultation-job.v1",
        "runner_version": runner.RUNNER_VERSION,
        "status": "started",
        "pid": process.pid,
        "started_at": runner.utc_now(),
        "intent": request["intent"],
        "engine": request["engine"],
        "model": model,
        "requested_model": request.get("requested_model"),
        "repository": request.get("read"),
        "job_dir": str(job_dir),
        "run_dir": str(run_dir),
        "result_path": str(run_dir / "result.json"),
        "completion_event_path": str(event_path),
        "completion_event_id": event_path.stem,
        "notification_mode": notification_mode,
        "command": runner.command_preview(command),
    }
    runner.write_json(job_dir / "job.json", job)
    print("AGENT_JOB_STATUS=started")
    print(f"AGENT_JOB_PID={process.pid}")
    print(f"AGENT_JOB={job_dir / 'job.json'}")
    print(f"AGENT_RESULT={run_dir / 'result.json'}")
    print(f"AGENT_EVENT={event_path}")
    print(f"AGENT_EVENT_ID={event_path.stem}")
    return 0


def main(argv: list[str] | None = None) -> int:
    runner = _runner()
    args = parse_args(argv)
    if (args.completion_event or args.notify) and not (args.detach or args.request):
        raise _error("--notify is only valid with --detach")
    request = build_request(args)
    if args.detach:
        return launch_detached(request, args)
    if request["transport"] == "native":
        return native_handoff(request, out_dir=args.out_dir, output_format=args.format)
    completion_event = runner.normalize_completion_event_path(args.completion_event) if args.completion_event else None
    return perform(
        request,
        out_dir=args.out_dir,
        completion_event=completion_event,
        notification_mode=args.notify or "none",
        output_format=args.format,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    runner_module = _runner()
    try:
        raise SystemExit(main())
    except runner_module.RunnerError as exc:
        print(f"agent-executor: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code) from None
