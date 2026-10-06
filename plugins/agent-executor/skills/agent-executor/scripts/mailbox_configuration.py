"""Targeted, credential-safe edits of host MCP configuration files."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

TRANSPORT_FIELDS = {
    "type",
    "command",
    "args",
    "env",
    "env_vars",
    "environment",
    "cwd",
    "url",
    "serverUrl",
    "headers",
    "http_headers",
    "env_http_headers",
    "bearer_token_env_var",
    "disabled",
    "enabled",
}


def read_json(path: Path) -> dict[str, Any]:
    text = path.read_text() if path.exists() else "{}"
    clean = re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/', lambda match: match[0] if match[0].startswith('"') else " ", text
    )
    clean = re.sub(
        r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])', lambda match: match[0] if match[0].startswith('"') else "", clean
    )
    value = json.loads(clean or "{}")
    if not isinstance(value, dict):
        raise ValueError("host configuration must be an object")
    return value


def skip(text: str, index: int) -> int:
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end == -1 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated configuration comment")
            index = end + 2
        else:
            break
    return index


def value_end(text: str, index: int) -> int:
    if text[index] == '"':
        _, used = json.JSONDecoder().raw_decode(text[index:])
        return index + used
    if text[index] in "{[":
        close = "}" if text[index] == "{" else "]"
        cursor = index + 1
        while True:
            cursor = skip(text, cursor)
            if cursor >= len(text):
                raise ValueError("unterminated configuration object")
            if text[cursor] == close:
                return cursor + 1
            if text[cursor] in ",:":
                cursor += 1
            else:
                cursor = value_end(text, cursor)
    match = re.match(r"[^\s,}\]:/]+", text[index:])
    if not match:
        raise ValueError("invalid configuration value")
    return index + len(match[0])


def members(text: str, start: int) -> tuple[dict[str, tuple[int, int]], int, bool]:
    cursor = skip(text, start)
    if text[cursor] != "{":
        raise ValueError("MCP parent configuration must be an object")
    cursor += 1
    found = {}
    comma = False
    while True:
        cursor = skip(text, cursor)
        if text[cursor] == "}":
            return found, cursor, comma
        if text[cursor] != '"':
            raise ValueError("configuration keys must be quoted")
        key, used = json.JSONDecoder().raw_decode(text[cursor:])
        cursor = skip(text, cursor + used)
        if text[cursor] != ":":
            raise ValueError("configuration key is missing a value")
        begin = skip(text, cursor + 1)
        end = value_end(text, begin)
        if key in found:
            raise ValueError("duplicate configuration key")
        found[key] = begin, end
        cursor = skip(text, end)
        comma = text[cursor] == ","
        if comma:
            cursor += 1
        elif text[cursor] != "}":
            raise ValueError("configuration member separator is missing")


def set_json_path(text: str, path: list[str], value: Any, *, preserved_comments: str = "") -> str:
    def edit(start: int, keys: list[str]) -> str:
        entries, close, trailing = members(text, start)
        if keys[0] in entries:
            begin, end = entries[keys[0]]
            if len(keys) > 1:
                return edit(begin, keys[1:])
            return text[:begin] + preserved_comments + json.dumps(value, indent=2, ensure_ascii=True) + text[end:]
        nested = value
        for key in reversed(keys[1:]):
            nested = {key: nested}
        line_start = text.rfind("\n", 0, close) + 1
        indent = re.match(r"[ \t]*", text[line_start:close])[0]
        addition = (
            ("," if entries and not trailing else "")
            + "\n"
            + indent
            + "  "
            + json.dumps(keys[0])
            + ": "
            + json.dumps(nested, indent=2)
            + "\n"
            + indent
        )
        return text[:close] + addition + text[close:]

    return edit(skip(text, 0), path)


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("credential-bearing configuration must be a regular file")
    target = path.with_name(path.name + f".mailbox-bak.{time.time_ns()}")
    shutil.copy2(path, target)
    target.chmod(0o600)
    return target


def write_private(path: Path, text: str, *, expected: str | None = None) -> None:
    if path.is_symlink():
        raise ValueError("credential-bearing configuration cannot replace a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".mailbox-config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None and (path.read_text() if path.exists() else "{}\n") != expected:
            raise ValueError("configuration changed concurrently; rerun repair after the other writer finishes")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value)


def scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(scalar(item) for item in value) + "]"
    raise ValueError("unsupported MCP configuration value")


def toml_table(path: list[str], data: dict[str, Any]) -> str:
    lines = ["[" + ".".join(key(part) for part in path) + "]"]
    for name, value in data.items():
        if not isinstance(value, dict):
            lines.append(key(name) + " = " + scalar(value))
    for name, value in data.items():
        if isinstance(value, dict):
            lines += ["", toml_table([*path, name], value)]
    return "\n".join(lines) + "\n"


def comment(line: str) -> str:
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote and char == quote:
            quote = None
        elif not quote and char in "\"'":
            quote = char
        elif not quote and char == "#":
            return line[index:].rstrip()
    return ""


def inline_table(data: dict[str, Any]) -> str:
    return (
        "{ "
        + ", ".join(
            key(name) + " = " + (inline_table(value) if isinstance(value, dict) else scalar(value))
            for name, value in data.items()
        )
        + " }"
    )


def set_toml_server(text: str, server: dict[str, Any]) -> str:
    parsed = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    section = ""
    for index, line in enumerate(lines):
        annotation = comment(line)
        code = line[: line.find(annotation)] if annotation else line.rstrip("\n")
        if code.strip().startswith("["):
            section = code.strip()
        full = (
            re.match(r'^([ \t]*(?:mcp_servers|"mcp_servers")\s*=\s*)(\{.*\})([ \t]*)$', code) if not section else None
        )
        direct = (
            re.match(
                r'^([ \t]*(?:mcp_servers|"mcp_servers")\.(?:peer-mailbox|"peer-mailbox")\s*=\s*)(\{.*\})([ \t]*)$', code
            )
            if not section
            else None
        )
        nested = (
            re.match(r'^([ \t]*(?:peer-mailbox|"peer-mailbox")\s*=\s*)(\{.*\})([ \t]*)$', code)
            if section in {"[mcp_servers]", '["mcp_servers"]'}
            else None
        )
        match = full or direct or nested
        if match:
            value = {**parsed.get("mcp_servers", {}), "peer-mailbox": server} if full else server
            lines[index] = (
                match[1] + inline_table(value) + match[3] + annotation + ("\n" if line.endswith("\n") else "")
            )
            result = "".join(lines)
            tomllib.loads(result)
            return result
    dotted = re.compile(r'^[ \t]*(?:mcp_servers|"mcp_servers")\.(?:peer-mailbox|"peer-mailbox")\.')
    root_dotted = set()
    section = ""
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            section = line.strip()
        if not section and dotted.match(line):
            root_dotted.add(index)
    if root_dotted:
        retained = []
        comments = []
        for index, line in enumerate(lines):
            if index in root_dotted:
                if annotation := comment(line):
                    comments.append(annotation)
            else:
                retained.append(line)
        result = (
            "".join(retained).rstrip()
            + "\n\n"
            + "\n".join(comments)
            + ("\n" if comments else "")
            + toml_table(["mcp_servers", "peer-mailbox"], server)
        )
        tomllib.loads(result)
        return result
    sections = list(re.finditer(r"(?m)^\s*\[[^\n]+\][ \t]*(?:#[^\n]*)?\n?", text))
    owned = []
    for index, match in enumerate(sections):
        header = match[0].split("#", 1)[0].strip()[1:-1]
        if re.match(r'^(?:mcp_servers|"mcp_servers")\.(?:peer-mailbox|"peer-mailbox")(?:\.|$)', header):
            end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
            owned.append((match.start(), end))
    comments = [comment(line) for start, end in owned for line in text[start:end].splitlines() if comment(line)]
    replacement = "\n".join(comments) + ("\n" if comments else "") + toml_table(["mcp_servers", "peer-mailbox"], server)
    if not owned:
        result = text.rstrip() + "\n\n" + replacement
    else:
        result = text
        for start, end in reversed(owned):
            result = result[:start] + (replacement if start == owned[0][0] else "") + result[end:]
    tomllib.loads(result)
    return result
