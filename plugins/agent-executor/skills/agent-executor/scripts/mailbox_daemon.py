import argparse
import contextlib
import fcntl
import http.client
import http.server
import json
import os
import secrets
import select
import socketserver
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import mailbox_mcp
import peer_mailbox as mailbox

DAEMON_VERSION = "1"
INSTANCE_ID = str(uuid.uuid4())


def daemon_base(base: Path) -> Path:
    name = base.name
    new_name = name.replace("mailbox-v1", "mailbox-daemon-v1") if "mailbox" in name else "mailbox-daemon-v1"
    return base.parent / new_name


def load_state(base: Path) -> dict[str, Any] | None:
    return mailbox.load(daemon_base(base) / "state.json")


def read_token(base: Path) -> str:
    try:
        return (daemon_base(base) / "token.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def generate_token(base: Path) -> str:
    token = secrets.token_urlsafe(32)
    path = daemon_base(base) / "token.txt"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


@contextlib.contextmanager
def locked_file(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
            yield fd
        except BlockingIOError:
            yield None
    finally:
        os.close(fd)


def idempotency_path(base: Path, key: str) -> Path:
    import hashlib

    safe_key = hashlib.sha256(key.encode()).hexdigest()
    return daemon_base(base) / "idempotency" / f"{safe_key}.json"


class HTTPDaemon(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = False  # Let requests finish if they can, but we cancel waits

    def __init__(self, server_address, RequestHandlerClass, base: Path, token: str):
        super().__init__(server_address, RequestHandlerClass)
        self.base = base
        self.token = token
        self.mcp_server = mailbox_mcp.Server(base=base, environ={}, stdio=False)
        self.is_shutting_down = False
        self.active_events = set()
        self.events_lock = threading.Lock()

    def cancel_all(self):
        with self.events_lock:
            for ev in self.active_events:
                ev.set()


class DaemonHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json_response(self, code: int, data: dict[str, Any] | list):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            if self.headers.get("Authorization") != f"Bearer {self.server.token}":
                self.send_error(401)
                return
            state = load_state(self.server.base)
            self.send_json_response(200, state or {})
        else:
            self.send_error(404)

    def do_DELETE(self):
        if self.path == "/health":
            if self.headers.get("Authorization") != f"Bearer {self.server.token}":
                self.send_error(401)
                return
            self.send_json_response(200, {"status": "shutting_down"})
            self.server.is_shutting_down = True
            self.server.cancel_all()
            threading.Thread(target=self.server.shutdown).start()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/mcp":
            self.send_error(404)
            return

        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed_origin = urllib.parse.urlparse(origin)
                if parsed_origin.hostname not in {"127.0.0.1", "localhost"}:
                    self.send_error(403, "Invalid Origin")
                    return
            except Exception:
                self.send_error(403, "Invalid Origin")
                return

        if self.headers.get("Authorization") != f"Bearer {self.server.token}":
            self.send_error(401, "Unauthorized")
            return

        if self.headers.get("Content-Type") != "application/json":
            self.send_error(415, "Unsupported Media Type")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 1024 * 1024:
            self.send_error(413, "Payload Too Large")
            return

        body = self.rfile.read(content_length)
        try:
            req = json.loads(body)
        except ValueError:
            self.send_error(400, "Bad Request")
            return

        if self.server.is_shutting_down:
            self.send_error(503, "Service Unavailable")
            return

        cancelled = threading.Event()
        monitor_stop = threading.Event()
        with self.server.events_lock:
            self.server.active_events.add(cancelled)

        try:

            def monitor_disconnect():
                try:
                    import socket

                    while not monitor_stop.is_set() and not cancelled.is_set():
                        r, _, _ = select.select([self.connection], [], [], 1.0)
                        if r:
                            try:
                                data = self.connection.recv(1, socket.MSG_PEEK)
                                if not data:
                                    cancelled.set()
                                    break
                            except BlockingIOError:
                                pass
                            except Exception:
                                cancelled.set()
                                break
                except Exception:
                    cancelled.set()

            monitor_thread = threading.Thread(target=monitor_disconnect, daemon=True)
            monitor_thread.start()

            resp = mailbox_mcp.respond(self.server.mcp_server, req, cancelled)

            if resp is not None:
                self.send_json_response(200, resp)
            elif "id" not in req:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
        finally:
            monitor_stop.set()
            with self.server.events_lock:
                self.server.active_events.discard(cancelled)

    def log_message(self, format, *args):
        pass


def start_daemon_process(base: Path, port: int):
    if os.fork() > 0:
        return
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    for k in list(os.environ):
        if k.startswith("AGENT_") or k.startswith("CLAUDE_"):
            del os.environ[k]

    os.environ["MAILBOX_DAEMON_DETACHED"] = "1"

    args = [sys.executable, str(Path(__file__).resolve()), "serve", "--root", str(base)]
    if port:
        args.extend(["--port", str(port)])
    os.execv(sys.executable, args)


def ensure_running(base: Path, port: int = 0) -> dict[str, Any]:
    with locked_file(daemon_base(base) / "startup.lock"):
        state = load_state(base)
        token = read_token(base)
        if state and state.get("url") and token:
            live = check_daemon(state["url"], token)
            if live and live.get("instance") == state.get("instance"):
                return state

        start_daemon_process(base, port)

        for _ in range(50):
            state = load_state(base)
            token = read_token(base)
            if state and state.get("url") and token:
                live = check_daemon(state["url"], token)
                if live and live.get("instance") == state.get("instance"):
                    return state
            time.sleep(0.1)
        raise mailbox.MailboxError("Daemon failed to start")


def check_daemon(url: str, token: str) -> dict[str, Any] | None:
    conn = None
    try:
        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        conn.request("GET", "/health", headers={"Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        if resp.status == 200:
            return json.loads(resp.read().decode())
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
    return None


def stop_daemon(url: str, token: str) -> bool:
    conn = None
    try:
        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        conn.request("DELETE", "/health", headers={"Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        return resp.status == 200
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


def serve(base: Path, port: int):
    with locked_file(daemon_base(base) / "service.lock", blocking=False) as fd:
        if fd is None:
            sys.stderr.write("Service already running\n")
            sys.exit(1)

        state = load_state(base)
        token = read_token(base)
        if not token:
            token = generate_token(base)

        if not port and state and state.get("url"):
            try:
                parsed = urllib.parse.urlparse(state["url"])
                if parsed.port:
                    port = parsed.port
            except Exception:
                pass

        try:
            server = HTTPDaemon(("127.0.0.1", port), DaemonHandler, base, token)
        except OSError:
            server = HTTPDaemon(("127.0.0.1", 0), DaemonHandler, base, token)

        bound_port = server.server_address[1]

        new_state = {
            "url": f"http://127.0.0.1:{bound_port}/mcp",
            "pid": os.getpid(),
            "instance": INSTANCE_ID,
            "version": DAEMON_VERSION,
            "protocolVersions": list(mailbox_mcp.PROTOCOLS),
        }

        mailbox.write(daemon_base(base) / "state.json", new_state)

        server.serve_forever()
        server.server_close()

        try:
            (daemon_base(base) / "state.json").unlink(missing_ok=True)
            # Do NOT unlink token.txt to persist it across restarts
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "start", "status", "stop", "restart"])
    parser.add_argument("--root", type=Path, default=mailbox.root())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    base = args.root.expanduser().resolve()

    if args.command == "serve":
        serve(base, args.port)

    elif args.command == "start":
        try:
            state = ensure_running(base, args.port)
            if args.json:
                print(json.dumps(state))
            else:
                print(f"Daemon running at {state['url']} (PID {state['pid']})")
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "status":
        state = load_state(base)
        token = read_token(base)
        if state and state.get("url") and token:
            live = check_daemon(state["url"], token)
            if live and live.get("pid") == state.get("pid"):
                if args.json:
                    print(json.dumps(live))
                else:
                    print(f"Daemon running at {live['url']} (PID {live['pid']})")
                sys.exit(0)

        if args.json:
            print(json.dumps({"status": "stopped"}))
        else:
            print("Daemon is stopped")
        sys.exit(1)

    elif args.command == "stop":
        state = load_state(base)
        token = read_token(base)
        stopped = False
        if state and state.get("url") and token:
            live = check_daemon(state["url"], token)
            if live and live.get("instance") == state.get("instance"):
                stop_daemon(state["url"], token)
                stopped = True
                for _ in range(50):
                    if not mailbox.pid_alive(state.get("pid")):
                        break
                    time.sleep(0.1)

        if stopped:
            if args.json:
                print(json.dumps({"status": "stopped"}))
            else:
                print("Daemon stopped")
        else:
            if args.json:
                print(json.dumps({"status": "already_stopped"}))
            else:
                print("Daemon is not running")

    elif args.command == "restart":
        state = load_state(base)
        token = read_token(base)
        if state and state.get("url") and token:
            live = check_daemon(state["url"], token)
            if live and live.get("instance") == state.get("instance"):
                stop_daemon(state["url"], token)
                for _ in range(50):
                    if not mailbox.pid_alive(state.get("pid")):
                        break
                    time.sleep(0.1)

        port = args.port
        if not port and state and state.get("url"):
            try:
                parsed = urllib.parse.urlparse(state["url"])
                if parsed.port:
                    port = parsed.port
            except Exception:
                pass

        try:
            new_state = ensure_running(base, port)
            if args.json:
                print(json.dumps(new_state))
            else:
                print(f"Daemon restarted at {new_state['url']} (PID {new_state['pid']})")
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e)}))
            else:
                print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
