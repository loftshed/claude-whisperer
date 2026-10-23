#!/usr/bin/env python3
"""Admission gate for heavy test, build, typecheck and lint commands on a machine several agents share.

    gate.py [--heavy] [--label TEXT] -- COMMAND...   run COMMAND once the gate lets it in
    gate.py status [--json]                           who holds the gate
    gate.py hook --harness claude|codex|gemini       PreToolUse: refuse a heavy run outside the gate
    gate.py install|uninstall [--dry-run]            add or remove that hook for each installed harness

A focused run takes one of a few slots. A heavy run (a whole suite, a production build, a project-wide
typecheck or lint) holds every slot, so it never runs beside another gated run, and while it waits no
new focused run gets in ahead of it. Waiters print who holds the gate. Slots are kernel file locks,
released when their holder exits, so a crash cannot leak one. Inside the gate Vitest and Jest get at
most two workers, the command runs at nice 10, and the full output of a heavy or failed run is kept in
a log so nobody reruns a suite to recover output that scrolled away.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

SLOTS = 2
WORKERS = 2
NICE = 10
POLL_SECONDS = 0.5
STATUS_EVERY_SECONDS = 30
LOGS_KEPT = 40
# Type-aware ESLint builds the whole program whatever it lints; past this many files it is a heavy run.
MANY_FILES = 10

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
SESSION_VARIABLES = (
    "AGENT_MAILBOX_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
)


class GateError(Exception):
    pass


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
        head, rest = Path(rest[0]).name, rest[1:]
    elif head in PACKAGE_MANAGERS and rest:
        sub, args = rest[0], rest[1:]
        if sub in {"exec", "dlx", "x"} and args:
            head, rest = Path(args[0]).name, args[1:]
        elif sub in RUNNERS:
            head, rest = sub, args  # yarn vitest: the package manager runs the binary
        else:
            script = args[0] if sub in {"run", "run-script"} and args else sub
            args = args[1:] if sub in {"run", "run-script"} else args
            if not SCRIPT.fullmatch(script):
                return None
            return (f"{head} {script}", not NARROW_SCRIPT.search(script) and not targets(args, set()))
    return runner(head, rest)


def runner(head: str, rest: list[str]) -> tuple[str, bool] | None:
    if head == "vitest":
        if rest[:1] and rest[0] in {"list", "init"}:
            return None
        args = rest[1:] if rest[:1] and rest[0] in {"run", "related", "watch", "dev", "bench"} else rest
        changed = any(a == "--changed" or a.startswith("--changed=") for a in args)
        return ("vitest", not (targets(args, TEST_VALUES) or changed))
    if head == "jest":
        changed = any(
            a in {"-o", "--onlyChanged", "--lastCommit", "--findRelatedTests"} or a.startswith("--changedSince")
            for a in rest
        )
        return ("jest", not (targets(rest, TEST_VALUES) or changed))
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


def heavy_runs(line: str) -> list[tuple[str, str]]:
    """(what runs, its command text) for each heavy run in a shell line that the gate does not wrap."""
    found = []
    for words in simple_commands(line):
        if gated(words):
            continue
        kind = classify(words)
        if kind and kind[1]:
            found.append((kind[0], shlex.join(strip_prefixes(words))))
    return found


# --- Holding the gate -----------------------------------------------------------------------------


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
    """The records of every slot or queue lock someone holds right now."""
    found = []
    for path in sorted(directory.glob("*.lock")):
        with contextlib.suppress(OSError), path.open("r+") as handle:
            if try_lock(handle):
                fcntl.flock(handle, fcntl.LOCK_UN)  # free: nobody holds it
                continue
            with contextlib.suppress(ValueError):
                holder = json.loads(handle.read() or "{}")
                if holder:
                    found.append({**holder, "lock": path.stem})
    return found


def describe(holder: dict[str, Any]) -> str:
    started = holder.get("startedAt")
    elapsed = ""
    with contextlib.suppress(TypeError, ValueError):
        seconds = int((dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(started)).total_seconds())
        elapsed = f", {seconds // 60}m{seconds % 60:02d}s"
    what = "queued heavy run" if holder.get("lock") == "queue" else "heavy run" if holder.get("heavy") else "run"
    return f"{holder.get('agent', '?')}'s {what} `{holder.get('label', '?')}` (pid {holder.get('pid', '?')}{elapsed})"


def acquire(directory: Path, holder: dict[str, Any], *, heavy: bool, slots: int) -> list[IO[str]]:
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
    return held


# --- Running inside the gate ----------------------------------------------------------------------


def capped(command: list[str], workers: int) -> list[str]:
    """Jest has no environment variable for its worker count; pass the flag unless the command sets one."""
    names = [re.sub(r"\.(c|m)?js$", "", Path(word).name) for word in command[:3]]
    sets_workers = any(a.startswith(("--maxWorkers", "-w")) or a in {"-i", "--runInBand"} for a in command)
    return [*command, f"--maxWorkers={workers}"] if "jest" in names and not sets_workers else command


def runnable(command: list[str]) -> bool:
    """The gate runs checks and builds only, so allowing it in a harness does not allow everything."""
    if classify(command):
        return True
    bare = strip_prefixes(command)
    if len(bare) > 1 and Path(bare[0]).name in {"node", "bun", "deno", *SHELLS} and not bare[1].startswith("-"):
        return bool(SCRIPT.search(Path(bare[1]).stem))  # a repository's own test or lint script
    return False


def prune_logs(directory: Path) -> None:
    logs = sorted(directory.glob("*.log"), key=lambda path: path.stat().st_mtime)
    for path in logs[:-LOGS_KEPT]:
        path.unlink(missing_ok=True)


def pump(source: IO[bytes], target: IO[bytes], log: IO[bytes], lock: threading.Lock) -> None:
    for chunk in iter(lambda: source.read1(65536), b""):
        target.write(chunk)
        target.flush()
        with lock:
            log.write(chunk)


def lower_priority() -> None:
    with contextlib.suppress(OSError):
        os.setpriority(os.PRIO_PROCESS, 0, max(NICE, os.getpriority(os.PRIO_PROCESS, 0)))


def run(command: list[str], *, heavy: bool, label: str | None, workers: int) -> int:
    if not runnable(command):
        raise GateError(
            "the gate runs test, build, typecheck and lint commands only; run anything else directly: "
            + shlex.join(command)
        )
    directory = gate_dir()
    holder = {
        "pid": os.getpid(),
        "heavy": heavy,
        "label": label or shlex.join(command)[:120],
        "agent": agent_name(),
        "cwd": os.getcwd(),
        "startedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    try:
        held = acquire(directory, holder, heavy=heavy, slots=slot_count())
    except OSError as error:
        print(f"gate: cannot use {directory} ({error}); running without the gate", file=sys.stderr, flush=True)
        held = []
    environment = {**os.environ, "VITEST_MAX_WORKERS": str(workers)}
    log_path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
    try:
        log: IO[bytes] = log_path.open("wb")
    except OSError:
        log = open(os.devnull, "wb")  # noqa: SIM115 -- closed with the run below
        log_path = None
    try:
        child = subprocess.Popen(
            capped(command, workers),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=lower_priority,  # noqa: PLW1509 -- only setpriority, before exec
            pass_fds=[handle.fileno() for handle in held],  # the slot lives as long as the workload
        )
    except OSError as error:
        log.close()
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
    for handle in held:
        handle.close()
    if log_path:
        if heavy or code != 0:
            print(
                f"gate: full output saved to {log_path}; read it instead of running the command again",
                file=sys.stderr,
                flush=True,
            )
        else:
            log_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            prune_logs(directory)
    return 128 - code if code < 0 else code


# --- The PreToolUse hook --------------------------------------------------------------------------


def gate_command() -> str:
    return "ork gate" if shutil.which("ork") else shlex.join(["python3", str(Path(__file__).resolve())])


def refusal(runs: list[tuple[str, str]]) -> str:
    kind, text = runs[0]
    return (
        f"`{text}` runs a whole {kind} pass, and several agents share this machine. To check whether your "
        "change broke something, run the tests that exercise it instead: the test file beside the code you "
        "changed, or `vitest related` / `jest --findRelatedTests` with the source files you changed. If you "
        f"really need the full run, as the final check, wrap it so it waits for other heavy runs: "
        f"`{gate_command()} --heavy -- {text}`."
    )


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
    runs = heavy_runs(line) if line else []
    if not runs:
        return None
    reason = refusal(runs)
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


def status(as_json: bool) -> int:
    found = holders(gate_dir())
    if as_json:
        print(json.dumps({"slots": slot_count(), "holders": found}, indent=2))
    elif not found:
        print(f"gate: free ({slot_count()} slots)")
    else:
        for holder in found:
            print(f"gate: {describe(holder)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv[:1] == ["status"]:
            return status("--json" in argv[1:])
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
