#!/usr/bin/env python3
"""Check installed peer messaging with isolated, model-free message/reply round trips."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any


class RpcClient:
    """Keep a native stdio client alive while requests and asynchronous notices interleave."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None):
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env
        )
        self.responses: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.identifier = 0
        self.notifications: list[dict[str, Any]] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.responses.put(None)

    def write(self, value: dict[str, Any]) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any], *, timeout: float = 40) -> Any:
        self.identifier += 1
        identifier = self.identifier
        self.write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        until = time.monotonic() + timeout
        while True:
            try:
                row = self.responses.get(timeout=max(0.01, until - time.monotonic()))
            except queue.Empty as error:
                raise RuntimeError(f"{method} timed out") from error
            if row is None:
                raise RuntimeError(f"client process ended during {method}")
            if row.get("id") != identifier:
                self.notifications.append(row)
                continue
            if "error" in row:
                raise RuntimeError(f"{method}: {row['error']}")
            return row.get("result")

    def close(self) -> None:
        assert self.process.stdin and self.process.stdout
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def initialize_codex(client: RpcClient) -> None:
    client.request(
        "initialize",
        {
            "clientInfo": {"name": "agent-executor-mailbox-check", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    )
    client.write({"method": "initialized"})


def result_text(result: dict[str, Any]) -> str:
    if result.get("isError"):
        raise RuntimeError(json.dumps(result))
    return "\n".join(block["text"] for block in result.get("content", []) if block.get("type") == "text")


def check() -> dict[str, Any]:
    from install_mailbox import configuration_status, mailbox_hook_updates

    configuration = configuration_status()
    missing = {name: row["issues"] for name, row in configuration.items() if not row["configured"]}
    if missing:
        raise RuntimeError(f"messaging needs setup: {missing}; run doctor --repair-messaging")
    scripts = Path(__file__).resolve().parent
    # Retain evidence; never prune the user's real peer store or send to their active sessions.
    evidence = Path.home() / ".cache/agent-executor/checks" / f"mailbox-{uuid.uuid4().hex}"
    evidence.mkdir(parents=True)
    env = {**os.environ, "AGENT_EXECUTOR_HOME": str(evidence)}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CLAUDE_PID", None)
    clients: dict[str, RpcClient] = {}
    sessions = {name: f"linux-check-{name}-{uuid.uuid4().hex}" for name in ("claude", "codex", "opencode", "gemini")}
    clients_info = {
        "claude": "claude-code",
        "codex": "codex-mcp-client",
        "opencode": "opencode",
        "gemini": "antigravity",
    }
    native: RpcClient | None = None
    exchanges = []
    try:
        for harness, name in clients_info.items():
            if harness == "codex" and shutil.which("codex"):
                native = RpcClient(["codex", "app-server", "--stdio"], env)
                initialize_codex(native)
                config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
                hooks = native.request("hooks/list", {"cwds": [str(Path.home())]})
                expected_hooks = mailbox_hook_updates(
                    hooks,
                    Path.home() / ".agents/skills/agent-executor/scripts/peer_mailbox.py",
                    config.with_name("hooks.json"),
                )
                trusted = {
                    hook["key"]: hook.get("trustStatus")
                    for entry in hooks.get("data", [])
                    for hook in entry.get("hooks", [])
                    if hook["key"] in expected_hooks
                }
                if trusted != dict.fromkeys(expected_hooks, "trusted"):
                    raise RuntimeError(
                        "Codex mailbox hooks need review: use /hooks or doctor --repair-messaging --trust-codex-hooks"
                    )
                installed = tomllib.loads(config.read_text())
                # Only the installed mailbox is exercised; no remote servers or providers are contacted.
                overrides = {
                    f"mcp_servers.{key}.enabled": False
                    for key in installed.get("mcp_servers", {})
                    if key != "peer-mailbox"
                }
                # Native MCP process environments are filtered; explicitly point the test server at
                # this isolated store instead of relying on the app-server's inherited environment.
                overrides["mcp_servers.peer-mailbox.env.AGENT_EXECUTOR_HOME"] = str(evidence)
                overrides.update({f"plugins.{key}.enabled": False for key in installed.get("plugins", {})})
                thread = native.request("thread/start", {"cwd": os.getcwd(), "ephemeral": True, "config": overrides})
                sessions[harness] = thread["thread"]["id"]
                continue
            client = RpcClient([sys.executable, "-B", str(scripts / "mailbox_mcp.py")], env)
            clients[harness] = client
            client.request(
                "initialize",
                {"protocolVersion": "2025-11-25", "clientInfo": {"name": name, "version": "1"}, "capabilities": {}},
            )
            client.write({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(harness: str, tool: str, arguments: dict[str, Any]) -> str:
            if tool == "send":
                arguments = {**arguments, "wake": False}
            if harness == "codex" and native:
                result = native.request(
                    "mcpServer/tool/call",
                    {"threadId": sessions[harness], "server": "peer-mailbox", "tool": tool, "arguments": arguments},
                )
            else:
                meta = {"sessionId": sessions[harness]}
                if harness == "opencode":
                    meta = {"ai.opencode/sessionID": sessions[harness]}
                result = clients[harness].request("tools/call", {"name": tool, "arguments": arguments, "_meta": meta})
            return result_text(result)

        for harness in sessions:
            call(harness, "peers", {})
        for sender in sessions:
            for receiver in sessions:
                if sender == receiver:
                    continue
                token = f"check-{sender}-to-{receiver}-{uuid.uuid4().hex}"
                message = json.loads(call(sender, "send", {"to": sessions[receiver], "text": token}))
                incoming = call(receiver, "inbox", {})
                if token not in incoming or "<peer-message" not in incoming:
                    raise RuntimeError(f"{sender} → {receiver}: message missing")
                if call(receiver, "inbox", {}) != "no unread peer messages":
                    raise RuntimeError(f"{sender} → {receiver}: message delivered twice")
                call(receiver, "send", {"to": sessions[sender], "text": "reply-" + token, "replyTo": message["id"]})
                response = call(sender, "wait", {"replyTo": message["id"], "timeoutSeconds": 5})
                if "reply-" + token not in response:
                    raise RuntimeError(f"{sender} → {receiver}: reply missing")
                exchanges.append(f"{sender} → {receiver} → {sender}")
        report = {
            "status": "passed",
            "round_trips": exchanges,
            "native_codex": native is not None,
            "evidence": str(evidence),
            "configuration": configuration,
            "codex_hook_trust": "verified" if native else "not applicable",
        }
        (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        for client in clients.values():
            client.close()
        if native:
            native.close()


def main() -> int:
    try:
        report = check()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"mailbox check: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
