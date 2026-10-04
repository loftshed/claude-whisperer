#!/usr/bin/env python3
"""Register the peer mailbox and turn hooks for installed local harnesses."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from check_mailbox import RpcClient, check, initialize_codex

EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
TOOLS = ("peers", "send", "inbox", "wait", "ack", "focus")


def read_json_config(path: Path) -> dict:
    text = path.read_text()
    if path.suffix == ".jsonc":
        # Keep quoted strings intact when removing JSONC comments and trailing commas.
        text = re.sub(
            r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
            lambda match: match[0] if match[0].startswith('"') else " ",
            text,
        )
        text = re.sub(
            r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])',
            lambda match: match[0] if match[0].startswith('"') else "",
            text,
        )
    return json.loads(text)


def configuration_status(home: Path | None = None, binaries: set[str] | None = None) -> dict:
    """Inspect only messaging settings; never return credentials or unrelated config."""
    home = home or Path.home()
    binaries = (
        binaries
        if binaries is not None
        else {name for name in ("claude", "codex", "agy", "opencode") if shutil.which(name)}
    )
    expected = home / ".agents/skills/agent-executor/scripts/mailbox_mcp.py"
    checks = {
        "claude": (home / ".claude.json", "mcpServers", home / ".claude/settings.json"),
        "codex": (
            Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "config.toml",
            "mcp_servers",
            Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "hooks.json",
        ),
        "agy": (home / ".gemini/config/mcp_config.json", "mcpServers", None),
        "opencode": (
            Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "opencode/opencode.jsonc",
            "mcp",
            None,
        ),
    }
    result = {}
    for harness, (path, section, hooks_path) in checks.items():
        if harness not in binaries:
            continue
        if harness == "opencode" and not path.exists():
            path = path.with_suffix(".json")
        issues = []
        try:
            data = tomllib.loads(path.read_text()) if section == "mcp_servers" else read_json_config(path)
            entries = data.get(section, {})
            server = entries.get("servers", entries).get("peer-mailbox", {})
            command = server.get("command")
            args = server.get("args", [])
            if isinstance(command, list):
                args, command = command[1:], command[0] if command else None
            matches = command == "python3" and args == [str(expected)]
            if not matches:
                issues.append("mailbox MCP registration missing or stale")
            if server.get("disabled", False) or not server.get("enabled", True):
                issues.append("mailbox MCP is disabled")
            if harness == "codex" and server.get("tool_timeout_sec", 0) < 660:
                issues.append("Codex mailbox wait timeout is too short")
            if harness == "codex":
                for tool in TOOLS:
                    if server.get("tools", {}).get(tool, {}).get("approval_mode") != "approve":
                        issues.append(f"Codex mailbox {tool} needs scoped tool permission")
        except (OSError, ValueError):
            issues.append("mailbox configuration could not be read")
        if hooks_path:
            try:
                hooks = json.loads(hooks_path.read_text()).get("hooks", {})
                command = shlex.join(
                    ["python3", str(expected.with_name("peer_mailbox.py")), "hook", "--harness", harness]
                )
                for event in EVENTS:
                    if not any(
                        entry.get("command") == command
                        for group in hooks.get(event, [])
                        for entry in group.get("hooks", [])
                    ):
                        issues.append(f"{event} mailbox hook missing or stale")
            except (OSError, ValueError):
                issues.append("mailbox turn hooks missing or unreadable")
        result[harness] = {"configured": not issues, "issues": issues}
    return result


def backup(path: Path) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(f"{path.name}.mailbox-bak.{time.time_ns()}"))


def ensure_hub(hub: Path, skill: Path, *, dry_run: bool = False) -> None:
    if (hub / "scripts/mailbox_mcp.py").is_file():
        return
    if dry_run:
        print(f"would link messaging skill hub: {hub} -> {skill}")
        return
    hub.parent.mkdir(parents=True, exist_ok=True)
    if hub.exists() or hub.is_symlink():
        hub.rename(hub.with_name(f"{hub.name}.mailbox-bak.{time.time_ns()}"))
    hub.symlink_to(skill, target_is_directory=True)


def install_hooks(path: Path, script: Path, harness: str, *, dry_run: bool = False) -> bool:
    settings = json.loads(path.read_text()) if path.exists() else {}
    before = json.dumps(settings, sort_keys=True)
    command = shlex.join(["python3", str(script), "hook", "--harness", harness])
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        entries = [
            hook
            for group in groups
            for hook in group.get("hooks", [])
            if "peer_mailbox.py hook" in hook.get("command", "") or "peer_mailbox.py' hook" in hook.get("command", "")
        ]
        desired = {"type": "command", "command": command, "timeout": 3 if event == "SessionEnd" else 10}
        if entries:
            for entry in entries:
                entry.update(desired)
        else:
            groups.append({"hooks": [desired]})
    if before == json.dumps(settings, sort_keys=True):
        print(f"• hooks already installed: {path}")
        return False
    if dry_run:
        print(f"would install {harness} turn hooks: {path}")
        return True
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"✓ {harness} turn hooks: {path}")
    return True


def set_codex_timeout(path: Path, *, dry_run: bool = False) -> None:
    text = path.read_text()
    value = tomllib.loads(text).get("mcp_servers", {}).get("peer-mailbox", {})
    if value.get("tool_timeout_sec", 0) >= 660:
        return
    # `codex mcp add` creates this table. Preserve all other config and its comments.
    table = re.search(r'^\[mcp_servers\.(?:peer-mailbox|"peer-mailbox")\]\s*\n', text, re.MULTILINE)
    if not table:
        raise ValueError("Codex did not create the peer-mailbox configuration table")
    following = re.search(r"^\[", text[table.end() :], re.MULTILINE)
    end = table.end() + following.start() if following else len(text)
    block = text[table.end() : end]
    timeout = re.compile(r"^tool_timeout_sec\s*=.*$", re.MULTILINE)
    if timeout.search(block):
        block = timeout.sub("tool_timeout_sec = 660", block)
    else:
        block = "tool_timeout_sec = 660\n" + block
    updated = text[: table.end()] + block + text[end:]
    tomllib.loads(updated)
    if dry_run:
        print(f"would allow 600-second mailbox waits: {path}")
    else:
        backup(path)
        path.write_text(updated)
        print("✓ Codex mailbox tool timeout: 660 seconds")


def register(
    name: str,
    config: Path,
    section: str,
    command: list[str],
    *,
    dry_run: bool,
    env: dict[str, str] | None = None,
) -> None:
    current = {}
    if config.exists():
        if config.suffix == ".toml":
            current = tomllib.loads(config.read_text()).get(section, {}).get("peer-mailbox", {})
        else:
            entries = read_json_config(config).get(section, {})
            current = entries.get("servers", entries).get("peer-mailbox", {})
    server = str(Path.home() / ".agents/skills/agent-executor/scripts/mailbox_mcp.py")
    matches = current.get("command") == "python3" and current.get("args") == [server]
    if section == "mcp":
        matches = current.get("command") == ["python3", server] and current.get("enabled", True)
    matches = matches and current.get("disabled", False) is False and current.get("enabled", True)
    if matches:
        print(f"• {name} mailbox already registered")
        return
    if dry_run:
        print(f"would register {name}: {shlex.join(command)}")
        return
    backup(config)
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f"{name} registration failed: {(result.stderr or result.stdout).strip()[:300]}")
    print(f"✓ {name} mailbox registered")


def mailbox_hook_updates(response: dict, script: Path, hooks_file: Path) -> dict:
    """Only the four installer-owned commands qualify for explicit scoped trust."""
    expected = ["python3", str(script), "hook", "--harness", "codex"]
    events = {"sessionStart", "userPromptSubmit", "stop", "sessionEnd"}
    matches = []
    for entry in response.get("data", []):
        for hook in entry.get("hooks", []):
            if (
                hook.get("handlerType") == "command"
                and Path(hook.get("sourcePath", "")).resolve() == hooks_file.resolve()
                and shlex.split(hook.get("command", "")) == expected
                and hook.get("eventName") in events
            ):
                matches.append(hook)
    if len(matches) != 4 or {hook["eventName"] for hook in matches} != events:
        raise ValueError("Expected exactly four mailbox hooks; refusing to trust other hook definitions")
    return {hook["key"]: {"trusted_hash": hook["currentHash"]} for hook in matches}


def trust_codex(script: Path, codex: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        print("would trust only the four installer-owned Codex mailbox hooks")
        return
    with RpcClient(["codex", "app-server", "--stdio"]) as client:
        initialize_codex(client)
        response = client.request("hooks/list", {"cwds": [str(Path.home())]})
        updates = mailbox_hook_updates(response, script, codex / "hooks.json")
        current = tomllib.loads((codex / "config.toml").read_text()).get("hooks", {}).get("state", {})
        if all(current.get(key, {}).get("trusted_hash") == value["trusted_hash"] for key, value in updates.items()):
            print("• Codex mailbox hooks already trusted")
            return
        backup(codex / "config.toml")
        client.request(
            "config/batchWrite",
            {
                "edits": [{"keyPath": "hooks.state", "value": updates, "mergeStrategy": "upsert"}],
                "reloadUserConfig": True,
            },
        )
        response = client.request("hooks/list", {"cwds": [str(Path.home())]})
        trusted = {
            hook["key"]: hook["trustStatus"]
            for entry in response.get("data", [])
            for hook in entry.get("hooks", [])
            if hook["key"] in updates
        }
        if trusted != dict.fromkeys(updates, "trusted"):
            raise RuntimeError("Codex did not confirm trust for all four mailbox hooks")
        print("✓ trusted exactly four Codex mailbox hooks")


def codex_tool_edits(server: dict) -> list[dict]:
    """Permit the six local mailbox operations without changing other tool policy."""
    return [
        {
            "keyPath": f"mcp_servers.peer-mailbox.tools.{tool}.approval_mode",
            "value": "approve",
            "mergeStrategy": "replace",
        }
        for tool in TOOLS
        if server.get("tools", {}).get(tool, {}).get("approval_mode") != "approve"
    ]


def permit_codex_tools(config: Path, *, dry_run: bool = False) -> None:
    server = tomllib.loads(config.read_text()).get("mcp_servers", {}).get("peer-mailbox", {})
    edits = codex_tool_edits(server)
    if not edits:
        return
    if dry_run:
        print("would permit only the six local Codex mailbox tools")
        return
    backup(config)
    with RpcClient(["codex", "app-server", "--stdio"]) as client:
        initialize_codex(client)
        client.request("config/batchWrite", {"edits": edits, "reloadUserConfig": True})
    print("✓ scoped permission for six local Codex mailbox tools")


def install(*, dry_run: bool = False, trust_codex_hooks: bool = False, verify: bool = True) -> None:
    home = Path.home()
    hub = home / ".agents/skills/agent-executor/scripts"
    ensure_hub(hub.parent, Path(__file__).resolve().parent.parent, dry_run=dry_run)
    server = ["python3", str(hub / "mailbox_mcp.py")]
    if shutil.which("claude"):
        profiles = [home / ".claude"] + sorted(p for p in home.glob(".claude-*") if p.is_dir())
        for profile in profiles:
            if not profile.is_dir():
                continue
            env = dict(os.environ)
            env.pop("CLAUDE_CONFIG_DIR", None)
            config = home / ".claude.json"
            if profile.name != ".claude":
                env["CLAUDE_CONFIG_DIR"] = str(profile)
                config = profile / ".claude.json"
            register(
                f"Claude ({profile.name})",
                config,
                "mcpServers",
                ["claude", "mcp", "add", "--scope", "user", "peer-mailbox", "--", *server],
                dry_run=dry_run,
                env=env,
            )
            install_hooks(profile / "settings.json", hub / "peer_mailbox.py", "claude", dry_run=dry_run)
    if shutil.which("codex"):
        codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
        config = codex / "config.toml"
        register(
            "Codex", config, "mcp_servers", ["codex", "mcp", "add", "peer-mailbox", "--", *server], dry_run=dry_run
        )
        if not dry_run or config.exists() and "peer-mailbox" in config.read_text():
            set_codex_timeout(config, dry_run=dry_run)
            permit_codex_tools(config, dry_run=dry_run)
        install_hooks(codex / "hooks.json", hub / "peer_mailbox.py", "codex", dry_run=dry_run)
        if trust_codex_hooks:
            trust_codex(hub / "peer_mailbox.py", codex, dry_run=dry_run)
        else:
            print(
                "Codex automatic delivery needs hook trust: use /hooks, or rerun with --trust-codex-hooks after review."
            )
    if shutil.which("agy"):
        register(
            "Antigravity",
            home / ".gemini/config/mcp_config.json",
            "mcpServers",
            ["agy", "mcp", "add", "peer-mailbox", *server],
            dry_run=dry_run,
        )
    if shutil.which("opencode"):
        directory = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "opencode"
        config = directory / "opencode.jsonc"
        if not config.exists():
            config = directory / "opencode.json"
        register(
            "OpenCode",
            config,
            "mcp",
            ["opencode", "mcp", "add", "--global", "peer-mailbox", "--", *server],
            dry_run=dry_run,
        )
    if verify and not dry_run:
        report = check()
        print(f"✓ {len(report['round_trips'])} mailbox message/reply round trips; evidence: {report['evidence']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--trust-codex-hooks", action="store_true", help="explicitly trust only the four installed mailbox hooks"
    )
    args = parser.parse_args(argv)
    try:
        install(dry_run=args.dry_run, trust_codex_hooks=args.trust_codex_hooks)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"mailbox install: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
