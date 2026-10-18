"""Behavioral coverage for the Python-to-Node room command bridge."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[5]
SCRIPTS = ROOT / "plugins" / "orchestrate" / "skills" / "orchestrate" / "scripts"
SPEC = importlib.util.spec_from_file_location("stooges_cmd_under_test", SCRIPTS / "run_agent.py")
assert SPEC and SPEC.loader
COMMAND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMAND)


class StoogesCommandTests(unittest.TestCase):
    def test_missing_node_has_actionable_error(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["run_agent.py", "stooges", "--json"]),
            mock.patch.object(COMMAND.subprocess, "run", side_effect=FileNotFoundError("node")),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(COMMAND.main(), 127)
        self.assertIn("requires Node.js", stderr.getvalue())
        self.assertIn("PATH", stderr.getvalue())

    def test_failed_exec_has_distinct_exit_code(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["run_agent.py", "stooges"]),
            mock.patch.object(COMMAND.subprocess, "run", side_effect=OSError("permission denied")),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(COMMAND.main(), 126)
        self.assertIn("could not start Node.js: permission denied", stderr.getvalue())

    def test_literal_arguments_and_environment_are_forwarded_without_a_shell(self) -> None:
        args = ["--fixture", "/tmp/room with spaces $(touch nope).json", "--json"]
        with (
            mock.patch.object(sys, "argv", ["run_agent.py", "stooges", *args]),
            mock.patch.object(COMMAND.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run,
        ):
            self.assertEqual(COMMAND.main(), 1)
        positional, keywords = run.call_args
        self.assertEqual(positional[0][0], "node")
        self.assertEqual(positional[0][2:], ["stooges", *args])
        self.assertEqual(keywords, {"check": False})

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the integration bridge")
    def test_real_runner_dispatch_preserves_fixture_arguments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stooges bridge ") as temporary:
            root = Path(temporary)
            fixture = root / "room $(touch SHOULD_NOT_EXIST).json"
            room = {
                "schema": "agent-executor.room.v1",
                "id": "bridge-room",
                "agents": [{"id": "haiku-1", "engine": "claude", "model": "haiku", "status": "queued"}],
            }
            fixture.write_text(json.dumps(room), encoding="utf-8")
            env = {
                **os.environ,
                "AGENT_EXECUTOR_HOME": str(root / "cache"),
                "NO_COLOR": "1",
                "MONITOR_CACHE_DIR": str(root / "monitor"),
                "MONITOR_CONFIG": str(root / "monitor-config.json"),
            }
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "run_agent.py"), "stooges", "--fixture", str(fixture), "--json"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), room)
            self.assertFalse((root / "SHOULD_NOT_EXIST").exists())
            self.assertFalse((root / "cache").exists())

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the integration bridge")
    def test_real_runner_preserves_error_exit_and_cache_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stooges-cache-") as temporary:
            root = Path(temporary)
            env = {
                **os.environ,
                "AGENT_EXECUTOR_HOME": str(root),
                "NO_COLOR": "1",
                "MONITOR_CACHE_DIR": str(root / "monitor"),
                "MONITOR_CONFIG": str(root / "monitor-config.json"),
            }
            command = [sys.executable, str(SCRIPTS / "run_agent.py"), "stooges"]
            invalid = subprocess.run(
                command + ["--unknown-option"], env=env, capture_output=True, text=True, check=False, timeout=15
            )
            self.assertEqual(invalid.returncode, 1, invalid.stderr)
            self.assertIn("ork:", invalid.stderr)
            created = subprocess.run(
                command + ["--team", "claude:haiku", "--json"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            room = json.loads(created.stdout)
            saved = json.loads((root / "mailbox-v1" / "rooms" / f"{room['id']}.json").read_text())
            self.assertEqual(saved, room)
            self.assertEqual([agent["status"] for agent in room["agents"]], ["queued"])


if __name__ == "__main__":
    unittest.main()
