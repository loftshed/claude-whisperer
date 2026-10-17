"""One authenticated loopback MCP service for the shared durable mailbox."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import functools
import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import secrets
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

import mailbox_mcp as protocol
import peer_mailbox as mailbox

DAEMON_VERSION = "2"
HTTP_PROTOCOLS = tuple(version for version in protocol.PROTOCOLS if version != "2024-11-05")
RESERVED_PORTS = {3000, 3001, 3002, 4001, 4002, 8080, 9222}
MAX_BODY = 256 * 1024
MAX_CONNECTIONS = 64
MAX_WAITS = 32
CALLER_WAITS = 4
READ_TIMEOUT = 5
LEGACY_TTL = 2 * 60 * 60
MAX_LEGACY = 128
DRAIN_TIMEOUT = 12


def daemon_base(base: Path) -> Path:
    suffix = "v1" if base.name == "mailbox-v1" else hashlib.sha256(str(base.resolve()).encode()).hexdigest()[:12]
    return base.parent / ("mailbox-daemon-" + suffix)


def store_id(base: Path) -> str:
    return hashlib.sha256(str(base.resolve()).encode()).hexdigest()


@functools.cache
def runtime_revision() -> str:
    digest = hashlib.sha256()
    skill = Path(__file__).resolve().parent.parent
    for folder in ("scripts", "references", "templates"):
        directory = skill / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                digest.update(str(path.relative_to(skill)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def runtime_bundle(base: Path) -> Path:
    revision = runtime_revision()
    directory = daemon_base(base) / "runtimes"
    private_directory(directory)
    destination = directory / revision
    if not destination.exists():
        stage = Path(tempfile.mkdtemp(prefix=".runtime-", dir=directory))
        try:
            skill = Path(__file__).resolve().parent.parent
            for folder in ("scripts", "references", "templates"):
                if (skill / folder).is_dir():
                    shutil.copytree(
                        skill / folder, stage / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                    )
            mailbox.write(stage / "runtime.json", {"revision": revision, "storeId": store_id(base)})
            os.replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    private_directory(destination)
    return destination / "scripts/mailbox_daemon.py"


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise mailbox.MailboxError("daemon directory must be owned by this user and cannot be a symlink")
    path.chmod(0o700)


def private_file(path: Path) -> None:
    stat = path.lstat()
    if path.is_symlink() or not path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise mailbox.MailboxError("daemon state/credentials must be regular owner-only files")


def endpoint(url: str) -> urllib.parse.ParseResult:
    value = urllib.parse.urlparse(url)
    if (
        value.scheme != "http"
        or value.hostname != "127.0.0.1"
        or value.username
        or value.password
        or value.path != "/mcp"
        or value.query
        or value.fragment
    ):
        raise mailbox.MailboxError("daemon endpoint must be a loopback HTTP /mcp URL")
    if value.port is None or value.port in RESERVED_PORTS:
        raise mailbox.MailboxError("daemon endpoint uses an invalid or reserved port")
    return value


def load_state(base: Path) -> dict[str, Any] | None:
    path = daemon_base(base) / "state.json"
    if not path.exists():
        return None
    private_file(path)
    value = mailbox.load(path)
    if (
        not value
        or value.get("storeId") != store_id(base)
        or not isinstance(value.get("instance"), str)
        or type(value.get("pid")) is not int
    ):
        raise mailbox.MailboxError("daemon state is invalid; inspect the local state before restarting")
    endpoint(value.get("url", ""))
    return value


def read_token(base: Path) -> str:
    path = daemon_base(base) / "token.txt"
    if not path.exists():
        return ""
    private_file(path)
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", value):
        raise mailbox.MailboxError("daemon credentials are invalid; repair local configuration")
    return value


def generate_token(base: Path) -> str:
    existing = read_token(base)
    if existing:
        return existing
    path = daemon_base(base) / "token.txt"
    token = secrets.token_urlsafe(32)
    descriptor, temporary = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return token


@contextlib.contextmanager
def locked_file(path: Path, blocking: bool = True, *, timeout: float | None = None):
    private_directory(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    private_file(path)
    try:
        try:
            if timeout is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise mailbox.MailboxError(
                                "daemon startup busy; retry after the current lifecycle operation"
                            ) from None
                        time.sleep(min(0.01, remaining))
        except BlockingIOError:
            yield None
        else:
            yield descriptor
    finally:
        os.close(descriptor)


def safe_error(identifier: Any, code: int, message: str) -> dict[str, Any]:
    value = protocol.rpc_error(identifier, code, message)
    if identifier is None:
        value.pop("id")
    return value


def decode_name(value: str) -> str:
    if value.startswith("=?base64?") and value.endswith("?="):
        return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
    if not value or value != value.strip() or any(ord(char) < 0x20 or ord(char) >= 0x7F for char in value):
        raise ValueError("invalid header value")
    return value


class HTTPDaemon(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def __init__(self, address: tuple[str, int], base: Path, token: str):
        super().__init__(address, DaemonHandler)
        self.base = base
        self.token = token
        self.guard = threading.RLock()
        self.commit = threading.Lock()
        self.stopping = threading.Event()
        self.connections: set[socket.socket] = set()
        self.events: set[threading.Event] = set()
        self.mutations: set[object] = set()
        self.drained = threading.Condition(self.guard)
        self.waits: Counter[str] = Counter()
        self.legacy: dict[str, tuple[protocol.Server, float]] = {}
        self.info = {
            "url": f"http://127.0.0.1:{self.server_address[1]}/mcp",
            "pid": os.getpid(),
            "instance": secrets.token_urlsafe(24),
            "version": DAEMON_VERSION,
            "runtimeRevision": runtime_revision(),
            "protocolVersions": list(HTTP_PROTOCOLS),
            "storeId": store_id(base),
        }

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        with self.guard:
            if self.stopping.is_set() or len(self.connections) >= MAX_CONNECTIONS:
                with contextlib.suppress(OSError):
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 1\r\n\r\n"
                    )
                self.shutdown_request(request)
                return
            self.connections.add(request)
        request.settimeout(READ_TIMEOUT)
        super().process_request(request, client_address)

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.guard:
                self.connections.discard(request)

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        # Normal disconnects must not log payloads or noisy stack traces.
        print("mailbox daemon request failed", file=sys.stderr)

    def stop(self) -> None:
        self.stopping.set()
        with self.guard:
            for event in self.events:
                event.set()
        self.shutdown()

    def legacy_server(self, identifier: str) -> protocol.Server | None:
        now = time.monotonic()
        with self.guard:
            self.legacy = {key: value for key, value in self.legacy.items() if now - value[1] < LEGACY_TTL}
            found = self.legacy.get(identifier)
            if not found:
                return None
            self.legacy[identifier] = found[0], now
            return found[0]


class DaemonHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()

        def expire() -> None:
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

        self.read_deadline = threading.Timer(READ_TIMEOUT, expire)
        self.read_deadline.daemon = True
        self.read_deadline.start()

    def finish(self) -> None:
        self.read_deadline.cancel()
        super().finish()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def reply(self, status: int, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        data = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
            if body is not None
            else b""
        )
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except (OSError, ValueError):
            return

    def authorize(self) -> bool:
        if any(
            len(self.headers.get_all(name, [])) > 1
            for name in ("Authorization", "Content-Length", "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name")
        ):
            self.reply(400, safe_error(None, -32600, "duplicate transport headers"))
            return False
        if sum(len(key) + len(value) for key, value in self.headers.items()) > 16 * 1024:
            self.reply(431)
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urllib.parse.urlparse(origin)
                valid = (
                    parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost"}
                    and not parsed.username
                    and not parsed.password
                    and parsed.port == self.server.server_address[1]
                    and parsed.path in {"", "/"}
                    and not parsed.query
                    and not parsed.fragment
                )
            except ValueError:
                valid = False
            if not valid:
                self.reply(403, safe_error(None, -32600, "Origin is not allowed"))
                return False
        wanted = ("Bearer " + self.server.token).encode()
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, wanted):
            self.reply(401, safe_error(None, -32600, "local daemon credentials required"))
            return False
        return True

    def do_GET(self) -> None:
        if not self.authorize():
            return
        if self.path == "/health":
            self.reply(200, self.server.info)
        elif self.path == "/mcp":
            self.reply(405, headers={"Allow": "POST"})
        else:
            self.reply(404)

    def do_DELETE(self) -> None:
        if not self.authorize():
            return
        if self.path == "/health":
            self.reply(200, {"status": "shutting_down", "instance": self.server.info["instance"]})
            threading.Thread(target=self.server.stop, daemon=True).start()
        elif self.path == "/mcp" and self.headers.get("Mcp-Session-Id"):
            with self.server.guard:
                found = self.server.legacy.pop(self.headers["Mcp-Session-Id"], None)
            self.reply(200 if found else 404)
        elif self.path == "/mcp":
            self.reply(405, headers={"Allow": "POST"})
        else:
            self.reply(404)

    def legacy_batch(self, requests: list[Any]) -> None:
        server = self.server.legacy_server(self.headers.get("Mcp-Session-Id", ""))
        if not requests or len(requests) > 20 or server is None:
            self.reply(
                404 if server is None else 400,
                safe_error(None, -32602, "initialize a legacy session and use a batch of at most 20 messages"),
            )
            return
        timeout_budget = 0.0
        for request in requests:
            if not isinstance(request, dict) or request.get("method") == "initialize":
                self.reply(
                    400, safe_error(None, -32600, "legacy batches require ordinary request/notification objects")
                )
                return
            if (
                request.get("method") == "tools/call"
                and isinstance(request.get("params"), dict)
                and request["params"].get("name") == "wait"
            ):
                arguments = request["params"].get("arguments", {})
                seconds = (
                    arguments.get("timeoutSeconds", protocol.WAIT_DEFAULT)
                    if isinstance(arguments, dict)
                    else protocol.WAIT_DEFAULT
                )
                if type(seconds) not in {int, float} or not 0 < seconds <= protocol.WAIT_MAX:
                    self.reply(400, safe_error(None, -32602, "batch wait timeout is invalid"))
                    return
                timeout_budget += seconds
        if timeout_budget > protocol.WAIT_MAX:
            self.reply(400, safe_error(None, -32602, "total legacy batch wait budget exceeds 600 seconds"))
            return
        cancelled = threading.Event()
        finished = threading.Event()
        responses = []
        mutation = object()
        with self.server.guard:
            if self.server.stopping.is_set():
                self.reply(503, headers={"Retry-After": "1"})
                return
            key = "legacy-batch-" + self.headers["Mcp-Session-Id"]
            if sum(self.server.waits.values()) >= MAX_WAITS or self.server.waits[key] >= CALLER_WAITS:
                self.reply(503, headers={"Retry-After": "1"})
                return
            self.server.waits["legacy-batch-" + self.headers["Mcp-Session-Id"]] += 1
            if timeout_budget:
                self.server.events.add(cancelled)
            self.server.mutations.add(mutation)

        def disconnected() -> None:
            while not finished.wait(0.1):
                try:
                    ready, _, _ = select.select([self.connection], [], [], 0)
                    if ready and not self.connection.recv(1, socket.MSG_PEEK):
                        cancelled.set()
                        return
                except OSError:
                    cancelled.set()
                    return

        monitor = threading.Thread(target=disconnected, daemon=True)
        monitor.start()
        server.commit = self.server.commit
        try:
            for index, request in enumerate(requests):
                if protocol.is_modern_request(request):
                    self.reply(400, safe_error(None, -32600, "modern requests cannot use JSON-RPC batches"))
                    return
                response = protocol.respond(server, request, cancelled)
                if response is not None:
                    if response.get("id") is None:
                        response.pop("id", None)
                    responses.append(response)
                if cancelled.is_set():
                    if not self.server.stopping.is_set():
                        return
                    pending = requests[index + 1 :] if response is not None else requests[index:]
                    responses.extend(
                        safe_error(item["id"], -32000, "daemon shutting down; retry this request")
                        for item in pending
                        if "id" in item
                    )
                    break
            self.reply(200 if responses else 202, responses if responses else None)
        finally:
            finished.set()
            monitor.join(timeout=1)
            with self.server.drained:
                self.server.events.discard(cancelled)
                self.server.mutations.discard(mutation)
                key = "legacy-batch-" + self.headers["Mcp-Session-Id"]
                self.server.waits[key] -= 1
                if not self.server.waits[key]:
                    del self.server.waits[key]
                self.server.drained.notify_all()

    def do_POST(self) -> None:
        if not self.authorize():
            return
        if self.path != "/mcp":
            self.reply(404)
            return
        if self.server.stopping.is_set():
            self.reply(503, headers={"Retry-After": "1"})
            return
        if self.headers.get_content_type() != "application/json" or self.headers.get_param(
            "charset", "utf-8"
        ).lower() not in {"utf-8", "utf8"}:
            self.reply(415)
            return
        length = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]+", length):
            self.reply(
                400, safe_error(None, -32600, "bounded Content-Length is required; chunked bodies are unsupported")
            )
            return
        if len(length) > 9 or int(length) > MAX_BODY:
            self.reply(413)
            return
        try:
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                self.reply(400, safe_error(None, -32700, "incomplete JSON request"))
                return

            def invalid_constant(value: str) -> None:
                raise ValueError(value)

            request = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
            self.read_deadline.cancel()
        except (ValueError, RecursionError):
            self.reply(400, safe_error(None, -32700, "invalid UTF-8 JSON request"))
            return
        except OSError:
            self.reply(408)
            return
        if isinstance(request, list):
            legacy = self.server.legacy_server(self.headers.get("Mcp-Session-Id", ""))
            version = self.headers.get("MCP-Protocol-Version") or (legacy.legacy_version if legacy else None)
            if version == "2025-03-26":
                self.legacy_batch(request)
                return
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
        ):
            self.reply(400, safe_error(None, -32600, "one JSON-RPC request or notification is required"))
            return
        identifier = request.get("id")
        if "id" in request and not protocol.valid_id(identifier):
            self.reply(400, safe_error(None, -32600, "invalid request id"))
            return
        params = request.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("_meta", {}), dict):
            self.reply(400, safe_error(identifier, -32602, "params and _meta must be objects"))
            return
        meta = params.get("_meta", {})
        body_version = meta.get(protocol.PROTOCOL_KEY)
        header_version = self.headers.get("MCP-Protocol-Version")
        modern = (
            header_version == protocol.LATEST_PROTOCOL
            or body_version == protocol.LATEST_PROTOCOL
            or request["method"] == "server/discover"
        )
        if modern:
            if not header_version or not self.headers.get("Mcp-Method"):
                self.reply(400, safe_error(identifier, -32020, "required MCP transport headers are missing"))
                return
            if body_version is None or not isinstance(meta.get(protocol.CAPABILITIES_KEY), dict):
                self.reply(400, safe_error(identifier, -32602, "modern protocol metadata is required"))
                return
            if header_version != body_version or self.headers["Mcp-Method"] != request["method"]:
                self.reply(400, safe_error(identifier, -32020, "transport headers do not match the request"))
                return
            if request["method"] == "tools/call":
                try:
                    name = decode_name(self.headers.get("Mcp-Name", ""))
                except (ValueError, UnicodeError):
                    name = None
                if name != params.get("name"):
                    self.reply(400, safe_error(identifier, -32020, "Mcp-Name must match params.name"))
                    return
        selected = header_version or body_version
        if selected is not None and selected not in HTTP_PROTOCOLS:
            error = safe_error(identifier, -32022, "Unsupported protocol version")
            error["error"]["data"] = {"supported": list(HTTP_PROTOCOLS), "requested": selected}
            self.reply(400, error)
            return
        # HTTP notifications are one-way. Legacy initialized is accepted; cancellation uses disconnect.
        if "id" not in request:
            self.reply(202)
            return
        headers: dict[str, str] = {}
        if modern:
            server = protocol.Server(base=self.server.base, environ={}, stdio=False)
        elif request["method"] == "initialize":
            with self.server.guard:
                self.server.legacy_server("")
                if len(self.server.legacy) >= MAX_LEGACY:
                    self.reply(503, headers={"Retry-After": "1"})
                    return
                server = protocol.Server(base=self.server.base, environ={}, stdio=False)
                session = secrets.token_urlsafe(24)
                self.server.legacy[session] = server, time.monotonic()
            headers["Mcp-Session-Id"] = session
            wanted = params.get("protocolVersion")
            params = {**params, "protocolVersion": wanted if wanted in HTTP_PROTOCOLS[1:] else HTTP_PROTOCOLS[1]}
            request = {**request, "params": params}
        else:
            server = self.server.legacy_server(self.headers.get("Mcp-Session-Id", ""))
            if server is None:
                self.reply(404, safe_error(identifier, -32602, "legacy session expired; initialize and retry"))
                return
        server.commit = self.server.commit
        is_wait = request["method"] == "tools/call" and params.get("name") == "wait"
        arguments = params.get("arguments", {})
        caller = arguments.get("session") if isinstance(arguments, dict) else None
        caller = caller or meta.get("sessionId") or meta.get("threadId") or meta.get("ai.opencode/sessionID")
        caller = caller if isinstance(caller, str) and len(caller) <= 200 else "unbound"
        cancelled = threading.Event()
        finished = threading.Event()
        if is_wait:
            with self.server.guard:
                if sum(self.server.waits.values()) >= MAX_WAITS or self.server.waits[caller] >= CALLER_WAITS:
                    self.reply(503, headers={"Retry-After": "1"})
                    return
                self.server.waits[caller] += 1
                self.server.events.add(cancelled)
        monitor = None
        if is_wait:

            def disconnected() -> None:
                while not finished.wait(0.1) and not cancelled.is_set():
                    try:
                        ready, _, _ = select.select([self.connection], [], [], 0)
                        if ready and not self.connection.recv(1, socket.MSG_PEEK):
                            with self.server.commit:
                                cancelled.set()
                            return
                    except OSError:
                        cancelled.set()
                        return

            monitor = threading.Thread(target=disconnected, daemon=True)
            monitor.start()
        mutation = object()
        if not is_wait:
            with self.server.guard:
                if self.server.stopping.is_set():
                    self.reply(503, headers={"Retry-After": "1"})
                    return
                self.server.mutations.add(mutation)
        try:
            response = protocol.respond(server, request, cancelled)
            if response is None:
                self.close_connection = True
                return
            if response.get("id") is None:
                response.pop("id", None)
            result = response.get("result", {})
            if request["method"] == "server/discover" and result:
                result["supportedVersions"] = list(HTTP_PROTOCOLS)
            status = 400 if response.get("error", {}).get("code") in {-32600, -32602, -32700, -32020, -32022} else 200
            if not cancelled.is_set():
                self.reply(status, response, headers)
        finally:
            if not is_wait:
                with self.server.drained:
                    self.server.mutations.discard(mutation)
                    self.server.drained.notify_all()
            finished.set()
            if monitor:
                monitor.join(timeout=1)
            if is_wait:
                with self.server.guard:
                    self.server.events.discard(cancelled)
                    self.server.waits[caller] -= 1
                    if not self.server.waits[caller]:
                        del self.server.waits[caller]


def check_daemon(url: str, token: str, *, timeout: float = 1) -> dict[str, Any] | None:
    connection = None
    try:
        parsed = endpoint(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        connection.request("GET", "/health", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        if response.status == 200:
            value = json.loads(response.read(MAX_BODY))
            return value if isinstance(value, dict) else None
    except (OSError, ValueError, http.client.HTTPException, mailbox.MailboxError):
        return None
    finally:
        if connection:
            connection.close()
    return None


def stop_daemon(url: str, token: str) -> bool:
    connection = None
    try:
        parsed = endpoint(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1)
        connection.request("DELETE", "/health", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        response.read(MAX_BODY)
        return response.status == 200
    except (OSError, ValueError, http.client.HTTPException, mailbox.MailboxError):
        return False
    finally:
        if connection:
            connection.close()


def owned_state(base: Path, *, timeout: float = 1) -> dict[str, Any] | None:
    state = load_state(base)
    if not state:
        return None
    token = read_token(base)
    if not token:
        return None
    live = check_daemon(state["url"], token, timeout=timeout)
    if live and all(live.get(key) == state.get(key) for key in ("pid", "instance", "storeId", "url")):
        return live
    return None


def start_daemon_process(base: Path, port: int) -> None:
    directory = daemon_base(base)
    script = runtime_bundle(base)
    log = directory / "daemon.log"
    if log.exists():
        private_file(log)
        if log.stat().st_size > 1024 * 1024:
            log.replace(directory / "daemon.previous.log")
    descriptor = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab", buffering=0) as output:
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CODEX_HOME")
            if key in os.environ
        }
        child = subprocess.Popen(
            [sys.executable, "-B", str(script), "serve", "--root", str(base), "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        # Retain/reap a child while this controller lives; no stdin/host lifetime is inherited.
        threading.Thread(target=child.wait, daemon=True).start()


def ensure_running(base: Path, port: int = 0, *, startup_timeout: float = 15) -> dict[str, Any]:
    base = base.expanduser().resolve()
    if type(port) is not int or not 0 <= port <= 65535 or port in RESERVED_PORTS:
        raise mailbox.MailboxError("choose an available, unreserved port or zero for first startup")
    until = time.monotonic() + startup_timeout
    with locked_file(daemon_base(base) / "startup.lock", timeout=startup_timeout):
        live = owned_state(base, timeout=max(0.01, min(1, (until - time.monotonic()) / 2)))
        if live:
            if port and endpoint(live["url"]).port != port:
                raise mailbox.MailboxError(
                    "daemon already owns a different endpoint; stop it explicitly before changing port"
                )
            return live
        state = load_state(base)
        if state:
            existing_port = endpoint(state["url"]).port
            if port and port != existing_port:
                raise mailbox.MailboxError("configured endpoint must remain stable; use the persisted port")
            port = existing_port
        start_daemon_process(base, port)
        while time.monotonic() < until:
            live = owned_state(base, timeout=max(0.01, min(1, (until - time.monotonic()) / 2)))
            if live:
                return live
            time.sleep(0.05)
        raise mailbox.MailboxError(
            "daemon startup failed; check its private log or endpoint conflict; no unrelated process was stopped"
        )


def stop_service(base: Path) -> bool:
    with locked_file(daemon_base(base) / "startup.lock"):
        state = owned_state(base)
        if not state:
            return False
        if not stop_daemon(state["url"], read_token(base)):
            raise mailbox.MailboxError("owned daemon did not accept shutdown")
        until = time.monotonic() + DRAIN_TIMEOUT + 3
        while time.monotonic() < until:
            if not check_daemon(state["url"], read_token(base)):
                with locked_file(daemon_base(base) / "service.lock", blocking=False) as released:
                    if released is not None:
                        return True
            time.sleep(0.05)
        raise mailbox.MailboxError("owned daemon shutdown timed out; no signal was sent to another process")


def serve(base: Path, port: int = 0) -> None:
    base = base.expanduser().resolve()
    if type(port) is not int or not 0 <= port <= 65535 or port in RESERVED_PORTS:
        raise mailbox.MailboxError("daemon port is invalid or reserved")
    with locked_file(daemon_base(base) / "service.lock", blocking=False) as descriptor:
        if descriptor is None:
            raise mailbox.MailboxError("one service already owns this mailbox store")
        token = generate_token(base)
        mailbox.prune_send_journals(base)
        state = load_state(base)
        if state and not port:
            port = endpoint(state["url"]).port
        try:
            server = HTTPDaemon(("127.0.0.1", port), base, token)
        except OSError as failure:
            raise mailbox.MailboxError(
                "persisted daemon endpoint is occupied; resolve the conflict without stopping its owner"
            ) from failure
        mailbox.write(daemon_base(base) / "state.json", server.info)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.stopping.set()
            with server.drained:
                for event in server.events:
                    event.set()
                deadline = time.monotonic() + DRAIN_TIMEOUT
                while server.mutations and time.monotonic() < deadline:
                    server.drained.wait(max(0.01, deadline - time.monotonic()))
                for connection in tuple(server.connections):
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)
                        connection.close()
            server.server_close()
        # Keep endpoint and credentials for recovery and client configuration stability.


def get_mode(base: Path) -> str:
    path = daemon_base(base) / "mode.json"
    if not path.exists():
        return "stdio"
    private_file(path)
    value = mailbox.load(path)
    if not value or value.get("mode") not in {"http", "stdio"}:
        raise mailbox.MailboxError("mailbox transport mode is invalid; repair local registration")
    return value["mode"]


def set_mode(base: Path, mode: str) -> None:
    if mode not in {"http", "stdio"}:
        raise mailbox.MailboxError("mailbox transport mode must be http or stdio")
    private_directory(daemon_base(base))
    mailbox.write(daemon_base(base) / "mode.json", {"mode": mode})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "start", "status", "stop", "restart"))
    parser.add_argument("--root", type=Path, default=mailbox.root())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    base = args.root.expanduser().resolve()
    try:
        if args.command == "serve":
            serve(base, args.port)
            return 0
        if args.command == "status":
            state = owned_state(base)
            data = (
                {**state, "status": "running", "staleImplementation": state["runtimeRevision"] != runtime_revision()}
                if state
                else {"status": "stopped"}
            )
        elif args.command == "stop":
            data = {"status": "stopped" if stop_service(base) else "already_stopped"}
        else:
            if args.command == "restart":
                stop_service(base)
            data = {**ensure_running(base, args.port), "status": "running"}
        print(
            json.dumps(data)
            if args.json
            else ("Daemon " + data["status"] + (" at " + data["url"] if "url" in data else ""))
        )
        return 1 if args.command == "status" and data["status"] == "stopped" else 0
    except (mailbox.MailboxError, OSError, ValueError) as failure:
        message = (
            str(failure)
            if isinstance(failure, mailbox.MailboxError)
            else "daemon storage or configuration error; inspect local permissions"
        )
        print(json.dumps({"error": message}) if args.json else message, file=sys.stdout if args.json else sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
