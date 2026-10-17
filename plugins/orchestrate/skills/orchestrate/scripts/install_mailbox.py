#!/usr/bin/env python3
"""Register the peer mailbox and turn hooks for installed local harnesses."""

from __future__ import annotations

import argparse
import copy
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import mailbox_configuration as configuration
import mailbox_daemon
import peer_mailbox as mailbox
from check_mailbox import RpcClient, check, initialize_codex

EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
TOOLS = ("peers", "send", "inbox", "wait", "ack", "focus", "register", "feedback")


def legacy_opencode_map(entries: dict) -> bool:
    nested = entries.get("servers")
    return (
        isinstance(nested, dict)
        and bool(nested)
        and not configuration.TRANSPORT_FIELDS.intersection(nested)
        and all(isinstance(value, dict) for value in nested.values())
    )


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


def configuration_status(
    home: Path | None = None, binaries: set[str] | None = None, *, base: Path | None = None
) -> dict:
    """Inspect only messaging settings; never return credentials or unrelated config."""
    home = home or Path.home()
    base = base or mailbox.root()
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
    for profile in sorted(home.glob(".claude-*")):
        if profile.is_dir() and ((profile / "settings.json").is_file() or (profile / ".claude.json").is_file()):
            checks["claude:" + profile.name] = (profile / ".claude.json", "mcpServers", profile / "settings.json")
    result = {}
    for identity, (path, section, hooks_path) in checks.items():
        harness = "claude" if identity.startswith("claude:") else identity
        if harness not in binaries:
            continue
        if harness == "opencode" and not path.exists():
            path = path.with_suffix(".json")
        issues = []
        try:
            data = tomllib.loads(path.read_text()) if section == "mcp_servers" else read_json_config(path)
            entries = data.get(section, {})
            if harness == "opencode" and legacy_opencode_map(entries):
                issues.append("legacy OpenCode nested MCP map; rerun messaging repair")
            server = entries.get("peer-mailbox", {})
            command = server.get("command")
            args = server.get("args", [])
            if isinstance(command, list):
                args, command = command[1:], command[0] if command else None

            matches_stdio = command == "python3" and args == [str(expected)]

            url = server.get("url") or server.get("serverUrl")
            has_auth = False
            if section == "mcp_servers" and harness == "codex":
                has_auth = bool(server.get("http_headers", {}).get("Authorization"))
            else:
                has_auth = bool(server.get("headers", {}).get("Authorization"))

            matches_http = False
            if url and has_auth:
                try:
                    mailbox_daemon.endpoint(url)
                    matches_http = not command and not server.get("env")
                except mailbox.MailboxError:
                    issues.append("mailbox HTTP endpoint is invalid")

            if not matches_stdio and not matches_http:
                issues.append("mailbox MCP registration missing or stale")
            if matches_http:
                live = mailbox_daemon.owned_state(base)
                auth = server.get("http_headers" if harness == "codex" else "headers", {}).get("Authorization")
                if not live:
                    issues.append("mailbox daemon unavailable; run mailbox daemon start")
                elif live.get("runtimeRevision") != mailbox_daemon.runtime_revision():
                    issues.append("mailbox daemon uses stale code; restart after upgrade and refresh the host catalog")
                elif url != live["url"] or auth != "Bearer " + mailbox_daemon.read_token(base):
                    issues.append(
                        "mailbox HTTP registration is stale; rerun messaging repair and refresh the host catalog"
                    )
                if any(field in server for field in ("command", "args", "env", "env_vars", "environment", "cwd")):
                    issues.append("mailbox HTTP entry still contains stdio fields")
                if path.stat().st_mode & 0o077:
                    issues.append("mailbox HTTP credentials require owner-only configuration permissions")

            if server.get("disabled", False) or not server.get("enabled", True):
                issues.append("mailbox MCP is disabled")
            if harness == "codex" and server.get("tool_timeout_sec", 0) < 660:
                issues.append("Codex mailbox wait timeout is too short")
            if harness == "opencode":
                if server.get("type") != ("remote" if url else "local"):
                    issues.append("OpenCode mailbox transport type is invalid")
                if server.get("timeout", 0) < 660000:
                    issues.append("OpenCode mailbox wait timeout is too short")
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
        result[identity] = {"configured": not issues, "issues": issues}
    return result


def backup(path: Path) -> None:
    configuration.backup(path)


def ensure_hub(hub: Path, skill: Path, *, dry_run: bool = False) -> None:
    if hub.exists() and hub.resolve() == skill.resolve() and (hub / "scripts/mailbox_mcp.py").is_file():
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
    configuration.write_private(path, json.dumps(settings, indent=2) + "\n")
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
    harness: str,
    *,
    dry_run: bool,
    stdio: bool = False,
    daemon_url: str = "",
    daemon_token: str = "",
    env: dict[str, str] | None = None,
) -> None:
    del env  # Structured local writes never send credentials through process arguments.
    if config.is_symlink():
        raise ValueError("MCP configuration must be a regular local file")
    text = config.read_text() if config.exists() else ("" if config.suffix == ".toml" else "{}\n")
    settings = tomllib.loads(text) if config.suffix == ".toml" else configuration.read_json(config)
    entries = settings.get(section, {})
    nested = section == "mcp" and legacy_opencode_map(entries)
    servers = entries.get("servers", {}) if nested else entries
    current = servers.get("peer-mailbox", {})
    if not isinstance(current, dict):
        raise ValueError("mailbox configuration entry must be an object")
    desired = copy.deepcopy({key: value for key, value in current.items() if key not in configuration.TRANSPORT_FIELDS})
    script = str(Path.home() / ".agents/skills/agent-executor/scripts/mailbox_mcp.py")
    if stdio:
        if harness == "opencode":
            desired = {key: value for key, value in desired.items() if key in {"enabled", "timeout"}}
            desired.update(
                type="local", command=["python3", script], enabled=True, timeout=max(desired.get("timeout", 0), 660000)
            )
        else:
            desired.update(command="python3", args=[script])
            if harness != "agy":
                desired["type"] = "stdio"
    else:
        mailbox_daemon.endpoint(daemon_url)
        if not daemon_token:
            raise ValueError("local daemon credentials are missing")
        if harness == "codex":
            desired.update(url=daemon_url, http_headers={"Authorization": "Bearer " + daemon_token})
        elif harness == "agy":
            desired.update(serverUrl=daemon_url, headers={"Authorization": "Bearer " + daemon_token})
        else:
            if harness == "opencode":
                desired = {key: value for key, value in desired.items() if key in {"enabled", "timeout", "oauth"}}
                desired["timeout"] = max(desired.get("timeout", 0), 660000)
                desired["oauth"] = False
            desired.update(
                type="remote" if harness == "opencode" else "http",
                url=daemon_url,
                headers={"Authorization": "Bearer " + daemon_token},
            )
    if harness == "codex":
        desired["tool_timeout_sec"] = max(desired.get("tool_timeout_sec", 0), 660)
        tools = desired.setdefault("tools", {})
        for tool in TOOLS:
            tools.setdefault(tool, {})["approval_mode"] = "approve"
        updated = configuration.set_toml_server(text, desired)
    else:
        if harness == "opencode" and nested:
            # Migrate the former nested shape into the current direct server map.
            # Preserve every other server and comment; only the owned entry changes transport.
            flattened = {key: value for key, value in entries.items() if key != "servers"}
            if any(key in flattened for key in servers):
                raise ValueError("legacy OpenCode server names conflict with direct entries")
            flattened.update(servers)
            flattened["peer-mailbox"] = desired
            root_members, _, _ = configuration.members(text, configuration.skip(text, 0))
            begin, end = root_members["mcp"]
            comments = [
                match[0]
                for match in re.finditer(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/', text[begin:end])
                if not match[0].startswith('"')
            ]
            updated = configuration.set_json_path(
                text, ["mcp"], flattened, preserved_comments="\n".join(comments) + ("\n" if comments else "")
            )
        else:
            updated = configuration.set_json_path(text or "{}", [section, "peer-mailbox"], desired)
        # Validate the actual edited text before replacing the user's file.
        clean = re.sub(
            r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
            lambda match: match[0] if match[0].startswith('"') else " ",
            updated,
        )
        clean = re.sub(
            r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])', lambda match: match[0] if match[0].startswith('"') else "", clean
        )
        json.loads(clean)
    if current == desired or updated == text:
        if not dry_run and config.exists():
            config.chmod(0o600)
        print(f"• {name} mailbox already registered ({'stdio' if stdio else 'http'})")
        return
    if dry_run:
        print(f"would register {name} ({'stdio' if stdio else 'http'}); credential values omitted")
        return
    backup(config)
    configuration.write_private(config, updated, expected=text if config.exists() else None)
    print(f"✓ {name} mailbox registered ({'stdio' if stdio else 'http'})")


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
    """Permit the local mailbox operations without changing other tool policy."""
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
        print(f"would permit only the {len(TOOLS)} local Codex mailbox tools")
        return
    backup(config)
    with RpcClient(["codex", "app-server", "--stdio"]) as client:
        initialize_codex(client)
        client.request("config/batchWrite", {"edits": edits, "reloadUserConfig": True})
    print(f"✓ scoped permission for {len(TOOLS)} local Codex mailbox tools")


def install_login_startup(home: Path, base: Path, script: Path, *, stdio: bool, dry_run: bool) -> None:
    """Queue startup at macOS login; the current service is already started explicitly by install."""
    if sys.platform != "darwin":
        return
    path = home / "Library/LaunchAgents/dev.loftshed.agent-mailbox.plist"
    if stdio:
        if path.exists() and not dry_run:
            backup(path)
            path.unlink()
        return
    data = {
        "Label": "dev.loftshed.agent-mailbox",
        "ProgramArguments": [
            sys.executable,
            str(script if dry_run else mailbox_daemon.runtime_bundle(base)),
            "serve",
            "--root",
            str(base),
            "--json",
        ],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {key: os.environ[key] for key in ("PATH", "CODEX_HOME") if key in os.environ},
    }
    content = plistlib.dumps(data).decode("utf-8")
    if path.exists() and path.read_text() == content:
        return
    if dry_run:
        print("would install owner-only mailbox startup at macOS login")
        return
    backup(path)
    configuration.write_private(path, content)


def install(
    *, dry_run: bool = False, trust_codex_hooks: bool = False, verify: bool = True, stdio: bool = False
) -> None:
    home = Path.home()
    hub = home / ".agents/skills/agent-executor/scripts"
    ensure_hub(hub.parent, Path(__file__).resolve().parent.parent, dry_run=dry_run)

    daemon_url = ""
    daemon_token = ""
    if not stdio:
        if dry_run:
            daemon_url = "http://127.0.0.1:49152/mcp"
            daemon_token = "mock"
        else:
            base = mailbox.root()
            state = mailbox_daemon.ensure_running(base)
            if state.get("runtimeRevision") != mailbox_daemon.runtime_revision():
                mailbox_daemon.stop_service(base)
                state = mailbox_daemon.ensure_running(base)
            daemon_url = state["url"]
            daemon_token = mailbox_daemon.read_token(base)
            mailbox_daemon.set_mode(base, "http")

    if shutil.which("claude"):
        profiles = [home / ".claude"] + sorted(
            p
            for p in home.glob(".claude-*")
            if p.is_dir() and ((p / "settings.json").is_file() or (p / ".claude.json").is_file())
        )
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
                "claude",
                dry_run=dry_run,
                stdio=stdio,
                daemon_url=daemon_url,
                daemon_token=daemon_token,
                env=env,
            )
            install_hooks(profile / "settings.json", hub / "peer_mailbox.py", "claude", dry_run=dry_run)

    if shutil.which("codex"):
        codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
        config = codex / "config.toml"
        register(
            "Codex",
            config,
            "mcp_servers",
            "codex",
            dry_run=dry_run,
            stdio=stdio,
            daemon_url=daemon_url,
            daemon_token=daemon_token,
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
            "agy",
            dry_run=dry_run,
            stdio=stdio,
            daemon_url=daemon_url,
            daemon_token=daemon_token,
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
            "opencode",
            dry_run=dry_run,
            stdio=stdio,
            daemon_url=daemon_url,
            daemon_token=daemon_token,
        )

    install_login_startup(home, mailbox.root(), hub / "mailbox_daemon.py", stdio=stdio, dry_run=dry_run)
    if not dry_run:
        mailbox_daemon.set_mode(mailbox.root(), "stdio" if stdio else "http")
    if verify and not dry_run:
        report = check()
        print(f"✓ {len(report['round_trips'])} mailbox message/reply round trips; evidence: {report['evidence']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--trust-codex-hooks", action="store_true", help="explicitly trust only the four installed mailbox hooks"
    )
    parser.add_argument("--stdio", action="store_true", help="install using local stdio rather than the daemon")
    args = parser.parse_args(argv)
    try:
        install(dry_run=args.dry_run, trust_codex_hooks=args.trust_codex_hooks, stdio=args.stdio)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"mailbox install: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
