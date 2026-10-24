#!/usr/bin/env python3
"""Admission gate for heavy test, build, typecheck and lint commands on a machine several agents share.

    gate.py [--heavy] [--label TEXT] -- COMMAND...   run COMMAND once the gate lets it in
    gate.py status [--all] [--json]                  who holds the gate and recent runs
    gate.py hook --harness claude|codex|gemini       PreToolUse: refuse a heavy run outside the gate
    gate.py install|uninstall [--dry-run]            add or remove that hook for each installed harness

A focused run takes one of a few slots. A heavy run (a whole suite, a production build, a project-wide
typecheck or lint) holds every slot, so it never runs beside another gated run, and while it waits no
new focused run gets in ahead of it. Waiters print who holds the gate. Slots are kernel file locks,
released when their holder exits, so a crash cannot leak one. Inside the gate Vitest and Jest get at
most two workers, the command runs at nice 10, and the full output of a heavy or failed run is kept in
a log and recorded in a rolling ledger so nobody reruns a suite to recover output that scrolled away.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

SLOTS = 2
WORKERS = 2
NICE = 10
POLL_SECONDS = 0.5
STATUS_EVERY_SECONDS = 30
LOGS_KEPT = 40
RUNS_KEPT = 200
STATUS_RUNS = 8
MAX_STATUS_ENTRIES = 5000
# Type-aware ESLint builds the whole program whatever it lints; past this many files it is a heavy run.
MANY_FILES = 10
INSTALL_STATES = ("node_modules/.yarn-state.yml", "node_modules/.package-lock.json", "node_modules/.modules.yaml")

RUNNERS = {
    "vitest",
    "jest",
    "tsc",
    "eslint",
    "pytest",
    "playwright",
    "vite",
    "next",
    "webpack",
    "rollup",
    "tsup",
    "mocha",
}
PACKAGE_MANAGERS = {"yarn", "npm", "pnpm", "bun"}
EXECUTORS = {"npx", "bunx", "pnpx"}
SHELLS = {"sh", "bash", "zsh", "dash"}
# Package scripts that check or build the project; a name like test:changed narrows itself.
SCRIPT = re.compile(r"(test|tests|e2e|lint|eslint|typecheck|type-check|types|tsc|build|check)([:_-].*)?")
NARROW_SCRIPT = re.compile(r"changed|related|staged|affected|watch")
SOURCE = re.compile(r"\.(c|m)?(j|t)sx?$|\.py$|\.vue$|\.svelte$")
INFO_FLAGS = {"-h", "--help", "-v", "--version", "--listTests", "--showConfig", "--print-config", "--init"}
TEST_VALUES = {
    "-t", "--testNamePattern", "--project", "-c", "--config", "--reporter", "--outputFile", "--maxWorkers",
    "-w", "--root", "-r", "--dir", "--shard", "--environment", "--pool", "--exclude", "--selectProjects",
    "--testPathIgnorePatterns", "--coverageDirectory", "--testTimeout", "--seed", "--mode",
}  # fmt: skip
ESLINT_VALUES = {
    "-c", "--config", "--ext", "--rule", "-f", "--format", "-o", "--output-file", "--cache-location",
    "--parser", "--parser-options", "--plugin", "--rulesdir", "--max-warnings", "--ignore-pattern", "--env",
    "--global", "--resolve-plugins-relative-to", "--flag",
}  # fmt: skip
PYTEST_VALUES = {"-k", "-m", "-p", "-c", "--rootdir", "-n", "--maxfail", "--junitxml", "-o", "--durations", "--tb"}
PLAYWRIGHT_VALUES = {"-c", "--config", "--project", "-g", "--grep", "-j", "--workers", "--reporter", "--shard"}
TSC_VALUES = {"-p", "--project", "--outDir", "--target", "--module", "--lib", "--jsx"}
PACKAGE_VALUES = {"--cwd", "-C", "--prefix", "--filter", "-F", "--workspace", "-w", "--dir"}
SESSION_VARIABLES = (
    "AGENT_MAILBOX_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
)


class GateError(Exception):
    pass


def iso_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


# --- What a command line runs ---------------------------------------------------------------------


def targets(args: list[str], values: set[str]) -> list[str]:
    """Positional arguments: files, paths or filters, never an option's value."""
    found, skip, literal = [], False, False
    for arg in args:
        if skip:
            skip = False
        elif literal or not arg.startswith("-"):
            found.append(arg)
        elif arg == "--":
            literal = True
        elif "=" not in arg and arg in values:
            skip = True
    return found


def strip_prefixes(words: list[str]) -> list[str]:
    """Drop variable assignments and wrappers such as time, nice, env or timeout that run the rest."""
    while words:
        name = Path(words[0]).name
        if name == "command" and words[1:2] in (["-v"], ["-V"]):
            return []  # looks a command up without running it
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        elif name in {"time", "nohup", "caffeinate", "command", "exec", "env", "nice", "timeout", "gtimeout"}:
            words = words[1:]
            while words and (words[0].startswith("-") or (name == "env" and "=" in words[0])):
                words = words[2:] if words[0] in {"-n", "-k", "-s", "-u"} else words[1:]
            if name in {"timeout", "gtimeout"} and words:
                words = words[1:]  # the duration
        else:
            return words
    return words


def strip_package_options(args: list[str]) -> list[str]:
    """Drop package-manager flags (--cwd, --filter, workspace <pkg>, …) before the subcommand or script."""
    while args:
        arg = args[0]
        if (arg == "workspace" or arg in PACKAGE_VALUES) and len(args) > 1:
            args = args[2:]
        elif arg.startswith("-") and arg != "--":
            args = args[1:]
        else:
            break
    return args


def classify(words: list[str]) -> tuple[str, bool] | None:
    """(what runs, whether it is heavy) for one simple command, or None when it checks nothing."""
    words = strip_prefixes(words)
    if not words:
        return None
    head, rest = Path(words[0]).name, words[1:]
    if any(arg in INFO_FLAGS for arg in rest):
        return None
    if head in {"node", "bun", "deno"} and rest:  # node node_modules/.bin/vitest, node …/jest/bin/jest.js
        stem = re.sub(r"\.(c|m)?js$", "", Path(rest[0]).name)
        if stem not in RUNNERS:
            return None
        head, rest = stem, rest[1:]
    if re.fullmatch(r"python3?(\.\d+)?", head) and rest[:1] == ["-m"] and len(rest) > 1:
        if rest[1] == "unittest":
            args = rest[2:]
            narrow = any(a in {"-p", "--pattern"} for a in args) and not any("*" in a for a in args)
            return ("unittest", (not args or "discover" in args) and not narrow)
        if rest[1] != "pytest":
            return None
        head, rest = "pytest", rest[2:]
    if head in EXECUTORS and rest:
        rest = strip_package_options(rest)
        if not rest:
            return None
        head, rest = Path(rest[0]).name, rest[1:]
    elif head in PACKAGE_MANAGERS and rest:
        rest = strip_package_options(rest)
        if not rest:
            return None
        sub, args = rest[0], rest[1:]
        if sub in {"exec", "dlx", "x"}:
            args = strip_package_options(args)
            if not args:
                return None
            head, rest = Path(args[0]).name, args[1:]
        elif sub in RUNNERS:
            head, rest = sub, args  # yarn vitest: the package manager runs the binary
        else:
            if sub in {"run", "run-script"}:
                args = strip_package_options(args)
                if not args:
                    return None
                script, args = args[0], args[1:]
            else:
                script = sub
            if not SCRIPT.fullmatch(script):
                return None
            found = targets(args, set())
            return (f"{head} {script}", (not NARROW_SCRIPT.search(script) and not found) or len(found) > MANY_FILES)
    return runner(head, rest)


def runner(head: str, rest: list[str]) -> tuple[str, bool] | None:
    if head == "vitest":
        if rest[:1] and rest[0] in {"list", "init"}:
            return None
        args = rest[1:] if rest[:1] and rest[0] in {"run", "related", "watch", "dev", "bench"} else rest
        found = targets(args, TEST_VALUES)
        changed = any(a == "--changed" or a.startswith("--changed=") for a in args)
        return ("vitest", (not found and not changed) or len(found) > MANY_FILES)
    if head == "jest":
        found = targets(rest, TEST_VALUES)
        changed = any(
            a in {"-o", "--onlyChanged", "--lastCommit", "--findRelatedTests"} or a.startswith("--changedSince")
            for a in rest
        )
        return ("jest", (not found and not changed) or len(found) > MANY_FILES)
    if head == "tsc":
        return ("tsc", not any(SOURCE.search(t) for t in targets(rest, TSC_VALUES)))
    if head == "eslint":
        found = targets(rest, ESLINT_VALUES)
        broad = not found or any(not SOURCE.search(t) or any(c in t for c in "*?{") for t in found)
        return ("eslint", broad or len(found) > MANY_FILES)
    if head == "pytest":
        return ("pytest", not targets(rest, PYTEST_VALUES))
    if head == "playwright":
        if rest[:1] != ["test"]:
            return None
        return ("playwright test", not targets(rest[1:], PLAYWRIGHT_VALUES))
    if head == "mocha":
        return ("mocha", not targets(rest, set()))
    if head in {"vite", "next"}:
        return (f"{head} build", True) if rest[:1] == ["build"] else None
    if head in {"webpack", "rollup", "tsup"}:
        return None if {"serve", "--watch", "-w"} & set(rest) else (head, True)
    return None


def drop_heredocs(line: str) -> str:
    """A here-document's body is data, such as a commit message, not commands."""
    lines, kept, delimiters = line.split("\n"), [], []
    for text in lines:
        if delimiters:
            if text.strip() == delimiters[0]:
                delimiters.pop(0)
            continue
        kept.append(text)
        delimiters += [match[1] for match in re.findall(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_]\w*)\1", text)]
    return "\n".join(kept)


def simple_commands(line: str, depth: int = 0) -> list[list[str]]:
    """The simple commands of a shell line, looking inside sh/bash/zsh -c strings."""
    lexer = shlex.shlex(drop_heredocs(line), posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return []  # unbalanced quotes: leave the call to the harness
    found: list[list[str]] = []
    words: list[str] = []
    redirect = False
    for token in [*tokens, ";"]:
        if redirect:
            redirect = False
        elif token and set(token) <= set("<>&") and ("<" in token or ">" in token):
            if words and words[-1].isdigit():
                words.pop()  # the 2 of 2>&1
            redirect = True
        elif token and set(token) <= set("();|&\n"):
            if words:
                found.append(words)
            words = []
        else:
            words.append(token)
    expanded = []
    for words in found:
        bare = strip_prefixes(words)
        script = None
        if depth < 3 and bare and Path(bare[0]).name in SHELLS:
            for index, arg in enumerate(bare[1:-1], 1):
                if re.fullmatch(r"-[a-z]*c[a-z]*", arg):
                    script = bare[index + 1]
                    break
        expanded.extend(simple_commands(script, depth + 1) if script is not None else [words])
    return expanded


def gated(words: list[str]) -> bool:
    bare = strip_prefixes(words)
    if not bare:
        return False
    head = Path(bare[0]).name
    if head == "ork" and bare[1:2] == ["gate"]:
        return True
    return head == "gate.py" or (head.startswith("python") and len(bare) > 1 and Path(bare[1]).name == "gate.py")


def heavy_runs_with_argv(line: str) -> list[tuple[str, str, list[str]]]:
    """(what runs, stripped command text, raw words) for each heavy run in a shell line outside the gate."""
    found: list[tuple[str, str, list[str]]] = []
    for words in simple_commands(line):
        if gated(words):
            continue
        kind = classify(words)
        if kind and kind[1]:
            found.append((kind[0], shlex.join(strip_prefixes(words)), list(words)))
    return found


def heavy_runs(line: str) -> list[tuple[str, str]]:
    """(what runs, its command text) for each heavy run in a shell line that the gate does not wrap."""
    return [(kind, text) for kind, text, _ in heavy_runs_with_argv(line)]


# --- Source metadata observation ------------------------------------------------------------------


def parse_porcelain_z(raw: bytes) -> list[tuple[str, str]] | None:
    """(status, path) pairs from `git status --porcelain=v1 -z`, or None when malformed or too large."""
    tokens = raw.split(b"\x00")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4 or token[2:3] != b" ":
            return None
        status_code = token[:2].decode("ascii", errors="replace")
        path = os.fsdecode(token[3:])
        entries.append((status_code, path))
        if "R" in status_code or "C" in status_code:
            if index >= len(tokens):
                return None
            entries.append((f"{status_code}<-", os.fsdecode(tokens[index])))
            index += 1
        if len(entries) > MAX_STATUS_ENTRIES:
            return None
    return entries


def fingerprint(top: Path) -> str | None:
    """Advisory stat-only digest of HEAD, dirty/untracked files, and known node_modules state files."""
    git_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        head = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(top), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            env=git_env,
            timeout=3,
            check=False,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return None
        porcelain = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(top), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            env=git_env,
            timeout=5,
            check=False,
        )
        if porcelain.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    entries = parse_porcelain_z(porcelain.stdout)
    if entries is None:
        return None
    digest = hashlib.sha256(f"v1\0head:{head.stdout.strip()}\0".encode())
    records: list[str] = []
    for status_code, rel_path in entries:
        target = top / rel_path
        try:
            st = target.lstat()
            records.append(f"{status_code}:{rel_path}:{st.st_mode}:{st.st_size}:{st.st_mtime_ns}")
        except FileNotFoundError:
            records.append(f"{status_code}:{rel_path}:deleted")
        except OSError:
            return None
    for rel_path in INSTALL_STATES:
        target = top / rel_path
        try:
            st = target.lstat()
            records.append(f"install:{rel_path}:{st.st_mode}:{st.st_size}:{st.st_mtime_ns}")
        except FileNotFoundError:
            continue
        except OSError:
            return None
    for item in sorted(records):
        digest.update(item.encode("utf-8", errors="surrogateescape") + b"\0")
    return f"v1:{digest.hexdigest()[:24]}"


def inspect_checkout(
    cwd: str | Path | None, *, include_fingerprint: bool = True
) -> tuple[str | None, str | None, str | None]:
    """(checkout root, cwd relative to checkout, source metadata fingerprint) or (None, None, None)."""
    if not cwd:
        return (None, None, None)
    try:
        resolved = Path(cwd).resolve()
        if not resolved.is_dir():
            return (None, None, None)
        top_proc = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if top_proc.returncode != 0 or not top_proc.stdout.strip():
            return (None, None, None)
        top = Path(top_proc.stdout.strip()).resolve()
        rel = resolved.relative_to(top).as_posix()
    except (OSError, ValueError, subprocess.SubprocessError):
        return (None, None, None)
    return (str(top), rel, fingerprint(top) if include_fingerprint else None)


def source_state(run_entry: Mapping[str, Any], current_fp: str | None = None) -> str:
    """How a recorded run's source metadata compares with itself and with current_fp."""
    start_fp = run_entry.get("startFingerprint")
    end_fp = run_entry.get("endFingerprint")
    if run_entry.get("sourceChangedDuringRun") or (start_fp and end_fp and start_fp != end_fp):
        return "source changed during the run"
    if not start_fp or not end_fp or not current_fp:
        return "source metadata unknown"
    return "source metadata matched" if end_fp == current_fp else "source metadata changed"


# --- Holding the gate and the run ledger ----------------------------------------------------------


def gate_dir() -> Path:
    return Path(os.environ.get("AGENT_GATE_DIR") or f"/tmp/agent-gate-{os.getuid()}")


def slot_count() -> int:
    value = os.environ.get("AGENT_GATE_SLOTS", "")
    return int(value) if value.isdigit() and int(value) > 0 else SLOTS


def agent_name() -> str:
    """This agent's peer-mailbox name when it has one, so waiters can say who holds the gate."""
    session = next((os.environ[name] for name in SESSION_VARIABLES if os.environ.get(name)), None)
    if not session:
        return "an unnamed session"
    with contextlib.suppress(Exception):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import peer_mailbox

        peer = peer_mailbox.load(peer_mailbox.peer_path(peer_mailbox.root(), session))
        if peer and isinstance(peer.get("name"), str):
            return peer["name"]
    return f"session {session[:12]}"


def try_lock(handle: IO[str]) -> bool:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def record(handle: IO[str], holder: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(holder))
    handle.flush()


def holders(directory: Path) -> list[dict[str, Any]]:
    """The records of every slot or queue lock someone holds right now, deduplicated by run."""
    found: list[dict[str, Any]] = []
    seen: set[Any] = set()
    paths = [*sorted(directory.glob("slot-*.lock")), directory / "queue.lock"]
    for path in paths:
        if not path.exists():
            continue
        with contextlib.suppress(OSError), path.open("r+") as handle:
            if try_lock(handle):
                fcntl.flock(handle, fcntl.LOCK_UN)  # free: nobody holds it
                continue
            with contextlib.suppress(ValueError):
                holder = json.loads(handle.read() or "{}")
                key = holder.get("runId") or holder.get("pid")
                if holder and key not in seen:
                    seen.add(key)
                    found.append({**holder, "lock": path.stem})
    return found


def describe(holder: dict[str, Any]) -> str:
    started = holder.get("startedAt") or holder.get("queuedAt")
    elapsed = ""
    with contextlib.suppress(TypeError, ValueError):
        seconds = max(0, int((dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(started)).total_seconds()))
        elapsed = f", {seconds // 60}m{seconds % 60:02d}s"
    what = "queued heavy run" if holder.get("lock") == "queue" else "heavy run" if holder.get("heavy") else "run"
    return f"{holder.get('agent', '?')}'s {what} `{holder.get('label', '?')}` (pid {holder.get('pid', '?')}{elapsed})"


@contextlib.contextmanager
def runs_locked(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "runs.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def reconcile_log(entry: dict[str, Any]) -> dict[str, Any]:
    log_path = entry.get("logPath")
    if isinstance(log_path, str) and log_path:
        if Path(log_path).is_file():
            return entry
        return {**entry, "logPath": None, "logExpired": True}
    return entry


def load_runs(directory: Path) -> list[dict[str, Any]]:
    path = directory / "runs.jsonl"
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict) and item.get("runId"):
                    entries.append(reconcile_log(item))
    return entries


def prune_logs(directory: Path, active_logs: set[str] | None = None) -> None:
    protected = (
        active_logs
        if active_logs is not None
        else {str(h["logPath"]) for h in holders(directory) if h.get("logPath") and h.get("pid") != os.getpid()}
    )
    logs = [
        path for path in sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime) if str(path) not in protected
    ]
    for path in logs[:-LOGS_KEPT]:
        path.unlink(missing_ok=True)


def record_run(directory: Path, entry: dict[str, Any]) -> None:
    with runs_locked(directory):
        current = load_runs(directory)
        active_logs = {
            str(h["logPath"]) for h in holders(directory) if h.get("logPath") and h.get("pid") != os.getpid()
        }
        with contextlib.suppress(OSError):
            prune_logs(directory, active_logs)
        updated_entries = [reconcile_log(item) for item in [*current, entry][-RUNS_KEPT:]]
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".runs-", suffix=".jsonl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for item in updated_entries:
                    handle.write(json.dumps(item, ensure_ascii=True) + "\n")
                handle.flush()
            os.replace(tmp_name, directory / "runs.jsonl")
        finally:
            Path(tmp_name).unlink(missing_ok=True)


def format_age(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    with contextlib.suppress(TypeError, ValueError):
        seconds = max(0, int((dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(timestamp)).total_seconds()))
        return f"{seconds // 60}m{seconds % 60:02d}s ago"
    return ""


def format_duration(seconds: Any) -> str:
    if isinstance(seconds, int | float) and seconds >= 0:
        whole = int(round(seconds))
        return f"{whole // 60}m{whole % 60:02d}s"
    return "?s"


def describe_run(entry: Mapping[str, Any], *, current_fp: str | None = None) -> str:
    what = "heavy run" if entry.get("heavy") else "run"
    outcome = entry.get("outcome") or ("passed" if entry.get("exitCode") == 0 else "failed")
    code = entry.get("exitCode")
    if outcome == "failed" and code is not None:
        status_text = f"failed (exit {code})"
    elif outcome == "signaled":
        sig = entry.get("signal")
        status_text = f"killed by signal {sig}" if sig else f"signaled (exit {code})"
    elif outcome == "launch_error":
        status_text = "failed to launch"
    else:
        status_text = str(outcome)
    age = format_age(entry.get("finishedAt"))
    age_part = f" {age}" if age else ""
    duration = format_duration(entry.get("durationSeconds"))
    src = source_state(entry, current_fp)
    log_part = ""
    if entry.get("logPath"):
        log_part = f"; log: {entry['logPath']}"
    elif entry.get("logExpired"):
        log_part = "; log expired"
    return (
        f"{entry.get('agent', '?')}'s {what} `{entry.get('label', '?')}` "
        f"{status_text}{age_part} ({duration}, {src}{log_part})"
    )


def is_exact_match(
    entry: Mapping[str, Any],
    *,
    checkout: str | None,
    relative_cwd: str | None,
    requested_argv: list[str],
    effective_argv: list[str],
    workers: int,
) -> bool:
    if not checkout or not relative_cwd:
        return False
    return (
        entry.get("checkout") == checkout
        and entry.get("relativeCwd") == relative_cwd
        and entry.get("requestedArgv") == requested_argv
        and entry.get("effectiveArgv") == effective_argv
        and entry.get("workers") == workers
    )


def acquire(directory: Path, holder: dict[str, Any], *, heavy: bool, slots: int) -> tuple[list[IO[str]], float]:
    """Take one slot, or every slot for a heavy run, behind the queue lock that keeps heavy runs from
    starving. Prints who holds the gate while waiting."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    started, last_status = time.monotonic(), -STATUS_EVERY_SECONDS
    held: list[IO[str]] = []

    def wait() -> None:
        nonlocal last_status
        if time.monotonic() - last_status >= STATUS_EVERY_SECONDS:
            last_status = time.monotonic()
            busy = [describe(h) for h in holders(directory) if h.get("pid") != os.getpid()]
            print(f"gate: waiting for {'; '.join(busy) or 'a free slot'}", file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)

    queue = (directory / "queue.lock").open("a+")
    try:
        while not try_lock(queue):
            wait()
        record(queue, holder)
        paths = [directory / f"slot-{index}.lock" for index in range(slots)]
        if heavy:
            for path in paths:
                handle = path.open("a+")
                while not try_lock(handle):
                    wait()
                record(handle, holder)  # at once, so a status read never shows the previous holder
                held.append(handle)
        else:
            while not held:
                for path in paths:
                    handle = path.open("a+")
                    if try_lock(handle):
                        record(handle, holder)
                        held.append(handle)
                        break
                    handle.close()
                else:
                    wait()
    except BaseException:
        for handle in held:
            handle.close()
        raise
    finally:
        record(queue, {})
        queue.close()  # closing releases the lock
    waited = time.monotonic() - started
    if waited >= 1:
        print(f"gate: in after {waited:.0f}s", file=sys.stderr, flush=True)
    return held, waited


# --- Running inside the gate ----------------------------------------------------------------------


def capped(command: list[str], workers: int) -> list[str]:
    """Jest has no environment variable for its worker count; pass the flag unless the command sets one."""
    kind = classify(command)
    if not kind or kind[0] != "jest":
        return command
    names = [re.sub(r"\.(c|m)?js$", "", Path(word).name) for word in command]
    jest_args = command[names.index("jest") + 1 :] if "jest" in names else command[1:]
    sets_workers = any(a.startswith(("--maxWorkers", "-w")) or a in {"-i", "--runInBand"} for a in jest_args)
    return [*command, f"--maxWorkers={workers}"] if not sets_workers else command


def runnable(command: list[str]) -> bool:
    """The gate runs checks and builds only, so allowing it in a harness does not allow everything."""
    if classify(command):
        return True
    bare = strip_prefixes(command)
    if len(bare) > 1 and Path(bare[0]).name in {"node", "bun", "deno", *SHELLS} and not bare[1].startswith("-"):
        return bool(SCRIPT.search(Path(bare[1]).stem))  # a repository's own test or lint script
    return False


def pump(source: IO[bytes], target: IO[bytes], log: IO[bytes], lock: threading.Lock) -> None:
    for chunk in iter(lambda: source.read1(65536), b""):
        target.write(chunk)
        target.flush()
        with lock:
            log.write(chunk)


def lower_priority() -> None:
    with contextlib.suppress(OSError):
        os.setpriority(os.PRIO_PROCESS, 0, max(NICE, os.getpriority(os.PRIO_PROCESS, 0)))


def matching_notice(
    entries: list[dict[str, Any]],
    *,
    checkout: str | None,
    relative_cwd: str | None,
    requested_argv: list[str],
    effective_argv: list[str],
    workers: int,
    current_fp: str | None = None,
) -> tuple[str | None, str | None]:
    """(runId, human notice) for the latest exact command match in entries."""
    if not checkout:
        return (None, None)
    same_checkout = [r for r in entries if r.get("checkout") == checkout]
    exact = [
        r
        for r in same_checkout
        if is_exact_match(
            r,
            checkout=checkout,
            relative_cwd=relative_cwd,
            requested_argv=requested_argv,
            effective_argv=effective_argv,
            workers=workers,
        )
    ]
    if exact:
        latest = exact[-1]
        fp = current_fp if current_fp is not None else fingerprint(Path(checkout))
        return (latest.get("runId"), f"previous run of this command: {describe_run(latest, current_fp=fp)}")
    return (None, None)


def safe_record_run(directory: Path, entry: dict[str, Any]) -> None:
    try:
        record_run(directory, entry)
    except Exception as error:
        print(f"gate: could not record the run: {error}", file=sys.stderr, flush=True)


def run(command: list[str], *, heavy: bool, label: str | None, workers: int) -> int:
    if not runnable(command):
        raise GateError(
            "the gate runs test, build, typecheck and lint commands only; run anything else directly: "
            + shlex.join(command)
        )
    directory = gate_dir()
    cwd = os.getcwd()
    checkout, relative_cwd, _ = inspect_checkout(cwd, include_fingerprint=False)
    classified = classify(command)
    kind_name = classified[0] if classified else "script"
    effective = capped(command, workers)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{os.urandom(2).hex()}"
    log_candidate = directory / f"{run_id}.log"
    queued_at = iso_now()
    seen_id: str | None = None
    with contextlib.suppress(Exception):
        seen_id, pre_notice = matching_notice(
            load_runs(directory),
            checkout=checkout,
            relative_cwd=relative_cwd,
            requested_argv=list(command),
            effective_argv=list(effective),
            workers=workers,
        )
        if pre_notice:
            print(f"gate: {pre_notice}", file=sys.stderr, flush=True)
    holder: dict[str, Any] = {
        "runId": run_id,
        "pid": os.getpid(),
        "heavy": heavy,
        "kind": kind_name,
        "label": label or shlex.join(command)[:120],
        "command": shlex.join(command)[:300],
        "requestedArgv": list(command),
        "effectiveArgv": list(effective),
        "workers": workers,
        "agent": agent_name(),
        "cwd": cwd,
        "checkout": checkout,
        "relativeCwd": relative_cwd,
        "queuedAt": queued_at,
        "startedAt": None,
        "startFingerprint": None,
        "logPath": str(log_candidate),
    }
    try:
        held, waited = acquire(directory, holder, heavy=heavy, slots=slot_count())
    except OSError as error:
        print(f"gate: cannot use {directory} ({error}); running without the gate", file=sys.stderr, flush=True)
        held, waited = [], 0.0
    start_fp = fingerprint(Path(checkout)) if checkout else None
    started_at = iso_now()
    run_started = time.monotonic()
    log_path: Path | None = log_candidate
    try:
        log: IO[bytes] = log_candidate.open("wb")
    except OSError:
        log = open(os.devnull, "wb")  # noqa: SIM115 -- closed with the run below
        log_path = None
    holder["startedAt"] = started_at
    holder["startFingerprint"] = start_fp
    holder["logPath"] = str(log_path) if log_path else None
    for handle in held:
        with contextlib.suppress(OSError):
            record(handle, holder)
    with contextlib.suppress(Exception):
        post_id, post_notice = matching_notice(
            load_runs(directory),
            checkout=checkout,
            relative_cwd=relative_cwd,
            requested_argv=list(command),
            effective_argv=list(effective),
            workers=workers,
            current_fp=start_fp,
        )
        if post_notice and (post_id != seen_id or waited >= 1):
            print(f"gate: after waiting, {post_notice}", file=sys.stderr, flush=True)
    environment = {**os.environ, "VITEST_MAX_WORKERS": str(workers)}
    try:
        child = subprocess.Popen(
            effective,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=lower_priority,  # noqa: PLW1509 -- only setpriority, before exec
            pass_fds=[handle.fileno() for handle in held],  # the slot lives as long as the workload
        )
    except OSError as error:
        log.close()
        if log_path:
            log_path.unlink(missing_ok=True)
        safe_record_run(
            directory,
            {
                **holder,
                "finishedAt": iso_now(),
                "durationSeconds": round(time.monotonic() - run_started, 2),
                "exitCode": None,
                "outcome": "launch_error",
                "signal": None,
                "error": str(error),
                "endFingerprint": start_fp,
                "sourceChangedDuringRun": False,
                "logPath": None,
            },
        )
        for handle in held:
            handle.close()
        raise GateError(f"cannot run {command[0]}: {error}") from error
    forward = lambda signum, _frame: child.send_signal(signum)  # noqa: E731
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    lock = threading.Lock()
    pumps = [
        threading.Thread(target=pump, args=(child.stdout, sys.stdout.buffer, log, lock), daemon=True),
        threading.Thread(target=pump, args=(child.stderr, sys.stderr.buffer, log, lock), daemon=True),
    ]
    for thread in pumps:
        thread.start()
    code = child.wait()
    for thread in pumps:
        thread.join()
    log.close()
    duration = round(time.monotonic() - run_started, 2)
    finished_at = iso_now()
    end_fp = fingerprint(Path(checkout)) if checkout else None
    exit_code = 128 - code if code < 0 else code
    outcome = "signaled" if code < 0 else "passed" if code == 0 else "failed"
    kept_log: str | None = None
    if log_path:
        if heavy or code != 0:
            kept_log = str(log_path)
            print(
                f"gate: full output saved to {log_path}; read it instead of running the command again",
                file=sys.stderr,
                flush=True,
            )
        else:
            log_path.unlink(missing_ok=True)
    safe_record_run(
        directory,
        {
            **holder,
            "finishedAt": finished_at,
            "durationSeconds": duration,
            "exitCode": exit_code,
            "outcome": outcome,
            "signal": -code if code < 0 else None,
            "error": None,
            "endFingerprint": end_fp,
            "sourceChangedDuringRun": bool(start_fp and end_fp and start_fp != end_fp),
            "logPath": kept_log,
        },
    )
    for handle in held:
        handle.close()
    return exit_code


# --- The PreToolUse hook --------------------------------------------------------------------------


def gate_command() -> str:
    return "ork gate" if shutil.which("ork") else shlex.join(["python3", str(Path(__file__).resolve())])


def hook_context(payload: dict[str, Any], line: str) -> tuple[str | None, str | None, str | None]:
    """Resolve (checkout, relative_cwd, current_fp) from the PreToolUse payload, never the hook's own cwd."""
    raw_cwd: str | None = None
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("workdir", "cwd"):
            if isinstance(tool_input.get(key), str) and tool_input[key].strip():
                raw_cwd = tool_input[key]
                break
    if not raw_cwd and isinstance(payload.get("cwd"), str) and payload["cwd"].strip():
        raw_cwd = payload["cwd"]
    if not raw_cwd:
        tool_call = payload.get("toolCall")
        if isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict):
            for key in ("cwd", "workingDirectory", "workdir"):
                val = tool_call["args"].get(key)
                if isinstance(val, str) and val.strip():
                    raw_cwd = val
                    break
    if not raw_cwd:
        spaces = payload.get("workspacePaths")
        if isinstance(spaces, list) and len(spaces) == 1 and isinstance(spaces[0], str) and spaces[0].strip():
            raw_cwd = spaces[0]
    checkout, relative_cwd, _ = inspect_checkout(raw_cwd, include_fingerprint=False)
    if not checkout or not raw_cwd:
        return (None, None, None)
    cur_dir = Path(raw_cwd)
    for words in simple_commands(line):
        bare = strip_prefixes(words)
        if bare[:1] == ["cd"]:
            if len(bare) != 2 or bare[1].startswith("-"):
                return (None, None, None)
            target = (cur_dir / os.path.expanduser(bare[1])) if not os.path.isabs(bare[1]) else Path(bare[1])
            new_checkout, _, _ = inspect_checkout(target, include_fingerprint=False)
            if new_checkout != checkout:
                return (None, None, None)
            cur_dir = target
            relative_cwd = None
        elif any(
            w in PACKAGE_VALUES or w == "workspace" or w.startswith(tuple(f"{f}=" for f in PACKAGE_VALUES))
            for w in bare
        ):
            relative_cwd = None
    return (checkout, relative_cwd, None)


def refusal(
    runs: list[tuple[str, str]],
    *,
    raw_argv: list[str] | None = None,
    directory: Path | None = None,
    checkout: str | None = None,
    relative_cwd: str | None = None,
    current_fp: str | None = None,
) -> str:
    kind, text = runs[0]
    base = (
        f"`{text}` runs a whole {kind} pass, and several agents share this machine. To check whether your "
        "change broke something, run the tests that exercise it instead: the test file beside the code you "
        "changed, or `vitest related` / `jest --findRelatedTests` with the source files you changed. If you "
        f"really need the full run, as the final check, wrap it so it waits for other heavy runs: "
        f"`{gate_command()} --heavy -- {text}`."
    )
    if not directory or not checkout:
        return base
    active = [h for h in holders(directory) if h.get("checkout") == checkout and h.get("pid") != os.getpid()]
    history = [r for r in load_runs(directory) if r.get("checkout") == checkout]
    if raw_argv is not None:
        requested = list(raw_argv)
    else:
        try:
            requested = shlex.split(text)
        except ValueError:
            requested = []
    effective = capped(requested, WORKERS) if requested else []
    exact = (
        [
            r
            for r in history
            if is_exact_match(
                r,
                checkout=checkout,
                relative_cwd=relative_cwd,
                requested_argv=requested,
                effective_argv=effective,
                workers=WORKERS,
            )
        ]
        if requested
        else []
    )
    notes: list[str] = []
    if active:
        notes.append(f"Active in this checkout: {describe(active[0])}.")
    if exact:
        fp = current_fp if current_fp is not None else fingerprint(Path(checkout))
        notes.append(f"Last matching run in this checkout: {describe_run(exact[-1], current_fp=fp)}.")
    if not notes and not history:
        return base
    if len(notes) < 2:
        notes.append(f"Run `{gate_command()} status` to see active and recent runs.")
    return "\n".join([base, *notes[:2]])


def command_line(payload: dict[str, Any]) -> str | None:
    """The shell command a PreToolUse payload is about to run: Claude Code, Codex or Antigravity."""
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, list) and all(isinstance(word, str) for word in command):
            # Codex may pass argv, often ["bash", "-lc", script].
            return shlex.join(command)
        return command if isinstance(command, str) else None
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict):
        command = tool_call["args"].get("CommandLine")
        return command if isinstance(command, str) else None
    return None


def hook(payload: dict[str, Any], harness: str) -> dict[str, Any] | None:
    """A deny for a heavy run outside the gate; None leaves the harness's own decision alone."""
    line = command_line(payload)
    detailed = heavy_runs_with_argv(line) if line else []
    if not detailed or not line:
        return None
    runs = [(kind, text) for kind, text, _ in detailed]
    checkout, relative_cwd, current_fp = hook_context(payload, line)
    reason = refusal(
        runs,
        raw_argv=detailed[0][2],
        directory=gate_dir(),
        checkout=checkout,
        relative_cwd=relative_cwd,
        current_fp=current_fp,
    )
    if harness == "gemini":
        return {"decision": "deny", "reason": reason}
    decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
    return {"hookSpecificOutput": decision}


# --- Installing the hook --------------------------------------------------------------------------

HOOK_NAME = "agent-gate"
SHELL_TOOLS = "Bash|shell|exec_command|local_shell|unified_exec"


def hook_command(script: Path, harness: str) -> str:
    return shlex.join(["python3", str(script), "hook", "--harness", harness])


def hook_files(home: Path) -> list[tuple[str, Path]]:
    """Each installed harness's hook file: every Claude profile, Codex, Antigravity."""
    found = []
    if shutil.which("claude"):
        profiles = [home / ".claude", *sorted(p for p in home.glob(".claude-*") if (p / "settings.json").is_file())]
        found += [("claude", profile / "settings.json") for profile in profiles if profile.is_dir()]
    if shutil.which("codex"):
        found.append(("codex", Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser() / "hooks.json"))
    if shutil.which("agy"):
        found.append(("gemini", home / ".gemini/config/hooks.json"))
    return found


def ours(entry: dict[str, Any]) -> bool:
    return "gate.py" in str(entry.get("command", "")) and " hook " in f" {entry.get('command', '')} "


def updated(settings: dict[str, Any], harness: str, script: Path | None) -> dict[str, Any]:
    """Settings with this hook added (script) or removed (None); everything else stays as it was."""
    settings = json.loads(json.dumps(settings))
    if harness == "gemini":
        settings.pop(HOOK_NAME, None)
        if script:
            entry = {"type": "command", "command": hook_command(script, harness), "timeout": 10}
            settings[HOOK_NAME] = {"PreToolUse": [{"matcher": "run_command", "hooks": [entry]}]}
        return settings
    hooks = settings.setdefault("hooks", {})
    groups = [
        {**group, "hooks": [entry for entry in group.get("hooks", []) if not ours(entry)]}
        for group in hooks.get("PreToolUse", [])
    ]
    groups = [group for group in groups if group["hooks"]]
    if script:
        entry = {"type": "command", "command": hook_command(script, harness), "timeout": 10}
        groups.append({"matcher": "Bash" if harness == "claude" else SHELL_TOOLS, "hooks": [entry]})
    if groups:
        hooks["PreToolUse"] = groups
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        settings.pop("hooks")
    return settings


def install(*, remove: bool = False, dry_run: bool = False, home: Path | None = None) -> int:
    """Add (or remove) the PreToolUse hook in each harness's hook file, backing up what it changes."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mailbox_configuration as configuration

    home = home or Path.home()
    hub = home / ".agents/skills/agent-executor/scripts/gate.py"
    script = None if remove else (hub if hub.is_file() else Path(__file__).resolve())
    for harness, path in hook_files(home):
        settings = json.loads(path.read_text()) if path.exists() else {}
        result = updated(settings, harness, script)
        verb = "remove" if remove else "install"
        if result == settings:
            print(f"• nothing to {verb}: {path}")
            continue
        if dry_run:
            print(f"would {verb} the gate hook: {path}")
            continue
        configuration.backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        configuration.write_private(path, json.dumps(result, indent=2) + "\n")
        print(f"✓ {'removed' if remove else 'installed'} the gate hook: {path}")
        if harness == "codex" and not remove:
            print("  Codex runs a new hook only once trusted: review it with /hooks in Codex.")
    return 0


# --- Command line ---------------------------------------------------------------------------------


def select_status_runs(
    entries: list[dict[str, Any]], *, checkout: str | None, cwd: str, all_checkouts: bool
) -> list[dict[str, Any]]:
    if all_checkouts:
        pool = entries
    elif checkout:
        pool = [r for r in entries if r.get("checkout") == checkout]
    else:
        pool = [r for r in entries if r.get("cwd") == cwd]
    priority = [r for r in reversed(pool) if r.get("heavy") or r.get("outcome") != "passed"]
    ordinary = [r for r in reversed(pool) if not (r.get("heavy") or r.get("outcome") != "passed")]
    return (priority + ordinary)[:STATUS_RUNS]


def status(*, as_json: bool = False, all_checkouts: bool = False) -> int:
    directory = gate_dir()
    active = holders(directory)
    cwd = os.getcwd()
    checkout, _, current_fp = inspect_checkout(cwd)
    selected = select_status_runs(load_runs(directory), checkout=checkout, cwd=cwd, all_checkouts=all_checkouts)
    fp_by_checkout: dict[str | None, str | None] = {checkout: current_fp}
    views: list[dict[str, Any]] = []
    for entry in selected:
        entry_checkout = entry.get("checkout")
        if entry_checkout not in fp_by_checkout:
            fp_by_checkout[entry_checkout] = fingerprint(Path(entry_checkout)) if entry_checkout else None
        views.append({**entry, "sourceState": source_state(entry, fp_by_checkout.get(entry_checkout))})
    if as_json:
        print(json.dumps({"slots": slot_count(), "checkout": checkout, "holders": active, "runs": views}, indent=2))
        return 0
    if not active:
        print(f"gate: free ({slot_count()} slots)")
    else:
        for holder in active:
            print(f"gate: {describe(holder)}")
    for view in views:
        print(f"gate: recent: {describe_run(view, current_fp=fp_by_checkout.get(view.get('checkout')))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv[:1] == ["status"]:
            parser = argparse.ArgumentParser(prog="gate.py status")
            parser.add_argument("--json", action="store_true", dest="as_json")
            parser.add_argument("--all", action="store_true", dest="all_checkouts")
            opts = parser.parse_args(argv[1:])
            return status(as_json=opts.as_json, all_checkouts=opts.all_checkouts)
        if argv[:1] == ["hook"]:
            parser = argparse.ArgumentParser(prog="gate.py hook")
            parser.add_argument("--harness", choices=("claude", "codex", "gemini"), default="claude")
            harness = parser.parse_args(argv[1:]).harness
            try:
                payload = json.loads(sys.stdin.read() or "{}")
            except ValueError:
                return 0  # never break a turn over a payload this cannot read
            decision = hook(payload if isinstance(payload, dict) else {}, harness)
            if decision:
                print(json.dumps(decision))
            return 0
        if argv[:1] in (["install"], ["uninstall"]):
            return install(remove=argv[0] == "uninstall", dry_run="--dry-run" in argv[1:])
        if "--" not in argv:
            raise GateError("usage: gate.py [--heavy] [--label TEXT] -- COMMAND...")
        split = argv.index("--")
        parser = argparse.ArgumentParser(prog="gate.py")
        parser.add_argument("--heavy", action="store_true", help="a whole suite, build or project-wide check")
        parser.add_argument("--label", help="what waiters see instead of the command")
        parser.add_argument("--workers", type=int, default=WORKERS, help="Vitest and Jest workers")
        options = parser.parse_args(argv[:split])
        command = argv[split + 1 :]
        if not command:
            raise GateError("no command after --")
        return run(command, heavy=options.heavy, label=options.label, workers=max(1, options.workers))
    except GateError as error:
        print(f"gate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
