"""JSON-RPC over Codex's existing local Unix WebSocket, using only the standard library."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from pathlib import Path


class ControlError(RuntimeError):
    pass


class CodexSocket:
    def __init__(self, path: Path, timeout: float = 5):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.timeout = timeout
        self.identifier = 0
        self.buffer = bytearray()
        try:
            self.socket.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n"
            )
            self.socket.sendall(request.encode())
            while b"\r\n\r\n" not in self.buffer:
                self.buffer.extend(self._receive())
                if len(self.buffer) > 65536:
                    raise ControlError("Codex handshake exceeds limit")
            header, rest = self.buffer.split(b"\r\n\r\n", 1)
            self.buffer = bytearray(rest)
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            fields = {
                name.lower(): value.strip()
                for name, value in (bytes(line).split(b":", 1) for line in header.split(b"\r\n")[1:])
            }
            if b" 101 " not in header.split(b"\r\n")[0] or fields.get(b"sec-websocket-accept") != accept:
                raise ControlError("Codex rejected the WebSocket handshake")
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "peer-mailbox-wake", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send({"method": "initialized"})
        except BaseException:
            self.socket.close()
            raise

    def _receive(self) -> bytes:
        data = self.socket.recv(65536)
        if not data:
            raise ControlError("Codex control connection closed")
        return data

    def read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self.buffer.extend(self._receive())
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def frame(self, opcode: int, payload: bytes) -> None:
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        self.socket.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))

    def send(self, value: dict) -> None:
        self.frame(1, json.dumps(value).encode())

    def message(self) -> dict:
        payload = bytearray()
        while True:
            first, second = self.read(2)
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read(8))[0]
            if size + len(payload) > 16 * 1024 * 1024 or second & 128:
                raise ControlError("Invalid Codex WebSocket frame")
            body = self.read(size)
            opcode = first & 15
            if opcode == 8:
                raise ControlError("Codex closed its WebSocket")
            if opcode == 9:
                self.frame(10, body)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise ControlError("Expected a Codex text frame")
            payload.extend(body)
            if first & 128:
                return json.loads(payload)

    def request(self, method: str, params: dict) -> dict:
        self.identifier += 1
        self.send({"id": self.identifier, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError(f"Codex {method} timed out")
            self.socket.settimeout(remaining)
            value = self.message()
            if value.get("id") != self.identifier:
                continue
            if "error" in value:
                raise ControlError(f"Codex {method}: {value['error'].get('message', 'request rejected')}")
            return value.get("result", {})

    def __enter__(self) -> CodexSocket:
        return self

    def __exit__(self, *_args) -> None:
        self.socket.close()
