"""`run_agent.py consult`: one question, one answer, no permission to act. Fake CLIs stand in for every engine."""

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
import time
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[5] / "plugins" / "orchestrate" / "skills" / "orchestrate"
SCRIPT = SKILL_DIR / "scripts" / "run_agent.py"
# Never query real subscription accounts through monitor, and use the bundled routes only.
os.environ["MONITOR_BIN"] = str(Path(__file__).parent / "no-monitor")
os.environ["AGENT_EXECUTOR_CONFIG"] = str(Path(tempfile.mkdtemp()) / "absent-config.json")
SPEC = importlib.util.spec_from_file_location("agent_executor_runner", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
CONSULT = RUNNER.bundled_module("consult")

ANSWER = """Thinking it through.

### ANSWER

Use a shared lock file; unlinking it races.

### UNCERTAINTY

I could not verify the kernel version; on 5.x the semantics differ.
"""

FINDINGS = """### FINDINGS

- high: lock.py:3 unlinks the lock while held.

### EVIDENCE

lock.py:3 `os.unlink(path)` runs before `close`.

### UNCERTAINTY

Did not check callers outside the repository.
"""

COMMON = r"""
import json
import os
import pathlib
import sys

def capture(extra=None):
    payload = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": {k: v for k, v in os.environ.items() if k.startswith(("OPENCODE", "CLAUDE", "TERM", "NO_COLOR"))}}
    if extra:
        payload.update(extra)
    pathlib.Path(os.environ["FAKE_CAPTURE"]).write_text(json.dumps(payload), encoding="utf-8")

def response():
    return os.environ.get("FAKE_RESPONSE", pathlib.Path(os.environ["FAKE_ANSWER_FILE"]).read_text(encoding="utf-8"))

def finish():
    if os.environ.get("FAKE_WRITE_FILE"):
        # A misbehaving consultant: writes into its working directory.
        pathlib.Path(os.environ["FAKE_WRITE_FILE"]).write_text("oops\n", encoding="utf-8")
    if os.environ.get("FAKE_STDERR"):
        print(os.environ["FAKE_STDERR"], file=sys.stderr)
    raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
"""

FAKE_CODEX = (
    "#!/usr/bin/env python3\n"
    + COMMON
    + r"""
if "--version" in sys.argv:
    print("codex-cli test 0.0")
elif sys.argv[1:3] == ["app-server", "--stdio"]:
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("method") == "initialize":
            print(json.dumps({"id": request["id"], "result": {}}), flush=True)
        elif request.get("method") == "model/list":
            efforts = [{"reasoningEffort": e} for e in ("low", "medium", "high", "max")]
            data = [
                {"id": "gpt-5.6-sol", "displayName": "Sol", "isDefault": False, "hidden": False, "supportedReasoningEfforts": efforts},
                {"id": "gpt-5.6-luna", "displayName": "Luna", "isDefault": True, "hidden": False, "supportedReasoningEfforts": efforts},
            ]
            print(json.dumps({"id": request["id"], "result": {"data": data, "nextCursor": None}}), flush=True)
else:
    prompt = sys.stdin.read()
    output_index = sys.argv.index("--output-last-message") + 1
    pathlib.Path(sys.argv[output_index]).write_text(response(), encoding="utf-8")
    capture({"stdin": prompt})
    print(json.dumps({"type": "thread.started", "thread_id": "codex-thread-7"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30}}))
    finish()
"""
)

FAKE_CLAUDE = (
    "#!/usr/bin/env python3\n"
    + COMMON
    + r"""
if "--version" in sys.argv:
    print("claude test 0.0")
elif "-p" in sys.argv:
    prompt = sys.stdin.read()
    capture({"stdin": prompt})
    session = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "claude-session-9"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
                      "result": response(), "total_cost_usd": 0.05,
                      "modelUsage": {"claude-opus-4-9": {"inputTokens": 10}},
                      "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1, "output_tokens": 4}}))
    finish()
else:
    pathlib.Path(os.environ["FAKE_UNSAFE_CALL"]).write_text("called\n", encoding="utf-8")
    raise SystemExit(99)
"""
)

FAKE_AGY = (
    "#!/usr/bin/env python3\n"
    + COMMON
    + r"""
if "--version" in sys.argv:
    print("agy test 1.3.0")
elif len(sys.argv) > 1 and sys.argv[1] == "models":
    print("gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)")
else:
    stream_input = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    log_index = sys.argv.index("--log-file") + 1
    pathlib.Path(sys.argv[log_index]).write_text("Created conversation 22222222-2222-2222-2222-222222222222\n", encoding="utf-8")
    capture({"stream_input": stream_input})
    model = sys.argv[sys.argv.index("--model") + 1]
    print(json.dumps({"type": "init", "conversation_id": "22222222-2222-2222-2222-222222222222", "model": model}))
    print(json.dumps({"type": "result", "status": os.environ.get("FAKE_AGY_STATUS", "success"), "conversation_id": "22222222-2222-2222-2222-222222222222", "response": response(), "error": os.environ.get("FAKE_AGY_ERROR", ""), "usage": {"input_tokens": 50, "output_tokens": 9, "cache_read_tokens": 0, "total_tokens": 59}, "denied_actions": json.loads(os.environ.get("FAKE_AGY_DENIED", "[]"))}))
    finish()
"""
)

FAKE_OPENCODE = (
    "#!/usr/bin/env python3\n"
    + COMMON
    + r"""
if "--version" in sys.argv:
    print("2.0.30")
elif len(sys.argv) > 1 and sys.argv[1] == "models":
    print("openrouter/~z-ai/glm-flash-latest\nopenrouter/deepseek/deepseek-v4-flash")
else:
    attached = pathlib.Path(sys.argv[sys.argv.index("--file") + 1]).read_text(encoding="utf-8")
    capture({"attached": attached})
    print(json.dumps({"type": "text", "sessionID": "ses_consult_1", "part": {"type": "text", "text": response()}}))
    finish()
"""
)


class ConsultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="consult-test-")
        self.root = Path(self.temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, source in (
            ("codex", FAKE_CODEX),
            ("claude", FAKE_CLAUDE),
            ("agy", FAKE_AGY),
            ("opencode", FAKE_OPENCODE),
        ):
            path = self.bin / name
            path.write_text(source, encoding="utf-8")
            path.chmod(0o700)
        self.answer_file = self.root / "answer.md"
        self.answer_file.write_text(ANSWER, encoding="utf-8")
        self.capture_path = self.root / "capture.json"
        self.unsafe_path = self.root / "unsafe-call"
        self.run_index = 0
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "consult@example.test")
        self.git("config", "user.name", "Consult Test")
        (self.repository / "lock.py").write_text(
            "import os\n\ndef release(path):\n    os.unlink(path)\n", encoding="utf-8"
        )
        self.git("add", "lock.py")
        self.git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repository, check=True, capture_output=True, text=True).stdout

    def environment(self, **extra: str) -> dict[str, str]:
        # Each run gets a fresh cache so catalogs, events and consult directories never cross tests.
        self.run_index += 1
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(self.root / f"cache-{self.run_index}"),
            "FAKE_CAPTURE": str(self.capture_path),
            "FAKE_ANSWER_FILE": str(self.answer_file),
            "FAKE_UNSAFE_CALL": str(self.unsafe_path),
        }
        # A test process may itself run inside a harness; the host is decided per test.
        for host_keys in RUNNER.HOST_MARKERS.values():
            for key in host_keys:
                env.setdefault(key, "")
        env.update(extra)
        return env

    def consult(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, dict, str]:
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, env or self.environment()),
            mock.patch.object(sys, "argv", [str(SCRIPT), "consult", *argv]),
            contextlib.redirect_stdout(output),
        ):
            try:
                code = RUNNER.main()
            except RUNNER.RunnerError as error:
                return error.exit_code, {"error": str(error)}, output.getvalue()
        text = output.getvalue()
        result_line = next((line for line in text.splitlines() if line.startswith("AGENT_RESULT=")), None)
        if result_line:
            result_path = result_line.split("=", 1)[1]
        else:
            # --format json prints the result itself instead of the AGENT_* lines.
            try:
                result_path = json.loads(text).get("result_path")
            except ValueError:
                result_path = None
        # A detached consultation announces its result path before the background job writes it.
        result = (
            json.loads(Path(result_path).read_text(encoding="utf-8"))
            if result_path and Path(result_path).exists()
            else {}
        )
        return code, result, text

    def capture(self) -> dict:
        return json.loads(self.capture_path.read_text(encoding="utf-8"))

    def test_default_route_asks_codex_read_only_and_returns_the_structured_answer(self) -> None:
        attachment = self.root / "lock.py"
        attachment.write_text("def lock():\n    ```not a fence```\n", encoding="utf-8")
        code, result, summary = self.consult(
            "--question",
            "Should the lock file be unlinked?",
            "--context",
            "We use flock on macOS.",
            "--attach",
            str(attachment),
        )
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["status"], result["intent"], result["transport"], result["engine"]),
            ("answered", "question", "executor", "codex"),
        )
        self.assertEqual(
            (result["model"], result["effort"]["bound"], result["route"]), ("gpt-5.6-sol", "high", "consultation")
        )
        self.assertEqual(result["model_preflight"], "live_family_resolved")
        self.assertEqual(result["answer"], "Use a shared lock file; unlinking it races.")
        self.assertIn("kernel version", result["uncertainty"])
        self.assertEqual(result["sections"], {"ANSWER": result["answer"], "UNCERTAINTY": result["uncertainty"]})
        self.assertEqual(
            (result["answer_format"], result["writes_allowed"], result["timeout"]), ("structured", False, "10m")
        )
        self.assertEqual(result["session_id"], "codex-thread-7")
        self.assertEqual(result["usage"]["totals"]["output"], 30)
        self.assertEqual(
            result["attachments"], [{"name": "lock.py", "path": str(attachment), "bytes": attachment.stat().st_size}]
        )
        self.assertIsNone(result["repository_delta"], "a question reads no repository")
        capture = self.capture()
        argv = capture["argv"]
        self.assertEqual(argv[:2], ["exec", "-C"])
        self.assertEqual(argv[2], result["workspace"])
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(sorted(os.listdir(result["workspace"])), [], "the consultant works in an empty workspace")
        prompt = capture["stdin"]
        self.assertIn("## Question\n\nShould the lock file be unlinked?", prompt)
        self.assertIn("## Context\n\nWe use flock on macOS.", prompt)
        self.assertIn("### lock.py\n\n````\ndef lock():", prompt, "a fence longer than the content's backticks")
        self.assertNotIn(str(attachment), prompt, "host paths stay out of the prompt")
        self.assertIn("no permission to edit files", prompt)
        self.assertIn("### UNCERTAINTY", prompt)
        self.assertIn("AGENT_CONSULT_STATUS=answered", summary)
        self.assertIn("AGENT_MODEL=gpt-5.6-sol", summary)
        self.assertIn("--- ANSWER ---\nUse a shared lock file; unlinking it races.\n--- UNCERTAINTY ---", summary)

    def test_claude_host_hands_a_claude_question_to_native_tools(self) -> None:
        env = self.environment(CLAUDECODE="1")
        code, result, summary = self.consult("--question", "Which lock?", "--model", "opus", env=env)
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["status"], result["transport"], result["host"], result["engine"]),
            ("native_handoff", "native", "claude", "claude"),
        )
        self.assertFalse(self.unsafe_path.exists(), "no CLI copy of Claude may be launched for a native question")
        self.assertFalse(self.capture_path.exists())
        self.assertIn("## Question\n\nWhich lock?", Path(result["prompt_path"]).read_text(encoding="utf-8"))
        self.assertIsNone(result["answer"])
        self.assertIn("AGENT_CONSULT_STATUS=native_handoff", summary)
        self.assertIn("Agent tool", summary)
        self.assertIn("--transport executor", summary)

    def test_transport_executor_runs_claude_code_without_tools_or_mcp(self) -> None:
        env = self.environment(
            CLAUDECODE="1", CLAUDE_CODE_MESSAGING_TOKEN="host-secret", CLAUDE_CONFIG_DIR="/host/profile"
        )
        code, result, _summary = self.consult(
            "--question", "Which lock?", "--model", "opus", "--effort", "xhigh", "--transport", "executor", env=env
        )
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["status"], result["transport"], result["engine"], result["model"]),
            ("answered", "executor", "claude", "opus"),
        )
        self.assertEqual(result["model_observed"], ["claude-opus-4-9"])
        self.assertEqual(result["session_id"], "claude-session-9")
        capture = self.capture()
        argv = capture["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "", "no built-in tools")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertIn("--session-id", argv)
        self.assertEqual(capture["env"].get("CLAUDECODE"), None, "the host session never leaks into the consultant")
        self.assertEqual(capture["env"].get("CLAUDE_CODE_MESSAGING_TOKEN"), None)
        self.assertEqual(
            capture["env"].get("CLAUDE_CONFIG_DIR"), None, "the default profile answers unless one is chosen"
        )

    def test_a_chosen_claude_profile_is_a_billing_boundary_native_tools_cannot_cross(self) -> None:
        profile = self.root / "other-profile"
        profile.mkdir()
        env = self.environment(CLAUDECODE="1")
        code, result, _summary = self.consult(
            "--question", "Which lock?", "--model", "opus", "--claude-config-dir", str(profile), env=env
        )
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["status"], result["transport"], result["claude_config_dir"]), ("answered", "executor", str(profile))
        )
        self.assertEqual(self.capture()["env"].get("CLAUDE_CONFIG_DIR"), str(profile))

    def test_transport_native_needs_a_host_that_runs_the_model(self) -> None:
        code, result, _summary = self.consult("--question", "Which lock?", "--model", "opus", "--transport", "native")
        self.assertEqual(code, 4)
        self.assertIn("--transport native", result["error"])

    def test_engine_is_inferred_from_the_model_family(self) -> None:
        code, result, _summary = self.consult("--question", "Which lock?", "--model", "gemini-3.8-flash-medium")
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["engine"], result["model"], result["model_observed"]),
            ("agy", "gemini-3.8-flash-medium", ["gemini-3.8-flash-medium"]),
        )
        self.assertEqual(result["effort"]["bound"], "medium")
        argv = self.capture()["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("--new-project", argv)
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(self.capture()["stream_input"][0]["event"], "user")
        self.assertEqual(result["session_id"], "22222222-2222-2222-2222-222222222222")

    def test_opencode_uses_the_read_only_plan_agent_without_auto_approval(self) -> None:
        code, result, _summary = self.consult("--question", "Which lock?", "--model", "glm:flash")
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["engine"], result["model"], result["variant"]),
            ("opencode", "openrouter/~z-ai/glm-flash-latest", "high"),
        )
        capture = self.capture()
        argv = capture["argv"]
        self.assertEqual(argv[argv.index("--agent") + 1], "plan")
        self.assertNotIn("--auto", argv)
        self.assertIn("--standalone", argv, "the shared background service would ignore this run's configuration")
        config = json.loads(capture["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["mcp"], {})
        self.assertFalse(any(config["tools"].values()), "a question runs with no tools at all")
        self.assertTrue({"read", "glob", "grep", "write", "shell", "execute"} <= set(config["tools"]))
        self.assertEqual(capture["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertEqual(argv[argv.index("--model") + 1], "openrouter/~z-ai/glm-flash-latest#high")
        self.assertEqual(json.loads(capture["env"]["OPENCODE_PERMISSION"])["bash"], "deny")
        self.assertIn("## Question\n\nWhich lock?", capture["attached"])
        self.assertEqual(result["session_id"], "ses_consult_1")

    def test_hosted_models_never_go_through_opencode(self) -> None:
        code, result, _summary = self.consult(
            "--question", "Which lock?", "--model", "openrouter/anthropic/claude-opus-4", "--engine", "opencode"
        )
        self.assertEqual(code, 17)
        self.assertIn("--engine claude", result["error"])

    def test_follow_up_resumes_the_session_and_pins_what_answered(self) -> None:
        code, first, _summary = self.consult("--question", "Which lock?", "--model", "gpt-5.6-luna", "--effort", "low")
        self.assertEqual(code, 0, first.get("error"))
        code, second, _summary = self.consult("--question", "And on Linux?", "--follow-up", first["result_path"])
        self.assertEqual(code, 0, second.get("error"))
        self.assertEqual(
            (second["status"], second["model"], second["effort"]["bound"]), ("answered", "gpt-5.6-luna", "low")
        )
        self.assertEqual(second["follow_up_of"], first["result_path"])
        argv = self.capture()["argv"]
        self.assertEqual(argv[:3], ["exec", "resume", "codex-thread-7"])
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertIn("# Follow-up to the consultation", self.capture()["stdin"])
        self.assertEqual(
            second["usage"]["totals"]["output"], 0, "a resumed Codex session reports usage since the baseline"
        )
        for refused_args, expected in (
            (["--model", "gpt-5.6-sol"], "stays on gpt-5.6-luna"),
            (["--effort", "max"], "stays on effort low"),
            (["--intent", "review"], "keeps the question intent"),
            (["--read", str(self.repository)], "keeps the repository"),
        ):
            with self.subTest(refused=refused_args):
                code, refused, _summary = self.consult(
                    "--question", "And on Linux?", "--follow-up", first["result_path"], *refused_args
                )
                self.assertEqual(code, 4)
                self.assertIn(expected, refused["error"])

    def test_follow_up_reuses_the_workspace_the_session_started_in(self) -> None:
        code, first, _summary = self.consult("--question", "Which lock?", "--model", "glm:flash")
        self.assertEqual(code, 0, first.get("error"))
        code, second, _summary = self.consult("--question", "Why?", "--follow-up", first["result_path"])
        self.assertEqual(code, 0, second.get("error"))
        self.assertEqual(second["workspace"], first["workspace"])
        self.assertEqual(self.capture()["cwd"], first["workspace"])
        self.assertEqual(self.capture()["argv"][:3], ["run", "--session", "ses_consult_1"])

    def test_attachments_refuse_secret_names_directories_symlinks_binaries_and_oversize_files(self) -> None:
        secret = self.root / "server.key"
        secret.write_text("not really\n", encoding="utf-8")
        cases: list[tuple[Path, str]] = [(secret, "holds credentials")]
        ssh = self.root / ".ssh"
        ssh.mkdir()
        (ssh / "config").write_text("Host x\n", encoding="utf-8")
        cases.append((ssh / "config", "holds credentials"))
        link = self.root / "harmless.txt"
        link.symlink_to(secret)
        cases.append((link, "holds credentials"))
        binary = self.root / "image.bin"
        binary.write_bytes(b"\x89PNG\x00\x00")
        cases.append((binary, "looks binary"))
        huge = self.root / "huge.log"
        huge.write_text("x" * (CONSULT.MAX_ATTACHMENT_BYTES + 1), encoding="utf-8")
        cases.append((huge, "KiB"))
        for path, message in cases:
            with self.subTest(path=path.name):
                code, result, _ = self.consult("--question", "Why?", "--attach", str(path))
                self.assertEqual(code, 4)
                self.assertIn(message, result["error"])
        self.assertFalse(self.capture_path.exists(), "nothing reaches a model when the inputs are refused")

    def test_an_unstructured_reply_is_still_the_answer(self) -> None:
        env = self.environment(FAKE_RESPONSE="Just unlink it after closing the descriptor.")
        code, result, summary = self.consult("--question", "Why?", "--model", "gpt-5.6-luna", env=env)
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["answer"], result["uncertainty"], result["answer_format"]),
            (
                "Just unlink it after closing the descriptor.",
                None,
                "unstructured",
            ),
        )
        self.assertIn("(the consultant gave no such section)", summary)

    def test_provider_quota_and_ordinary_failures_keep_their_exit_codes(self) -> None:
        env = self.environment(FAKE_EXIT="1", FAKE_STDERR="error: rate limit exceeded for this account")
        code, result, _ = self.consult("--question", "Why?", "--model", "gpt-5.6-luna", env=env)
        self.assertEqual((code, result["status"]), (16, "provider_quota_exhausted"))
        env = self.environment(FAKE_EXIT="2", FAKE_STDERR="boom")
        code, result, _ = self.consult("--question", "Why?", "--model", "gpt-5.6-luna", env=env)
        self.assertEqual((code, result["status"], result["answer"]), (1, "failed", None))
        self.assertEqual(result["stderr_tail"], ["boom"])
        env = self.environment(
            FAKE_AGY_STATUS="error", FAKE_AGY_ERROR="RESOURCE_EXHAUSTED (code 429)", FAKE_RESPONSE=""
        )
        code, result, _ = self.consult("--question", "Why?", "--model", "gemini-3.8-flash-medium", env=env)
        self.assertEqual((code, result["status"]), (16, "provider_quota_exhausted"))

    def test_dry_run_resolves_everything_without_calling_a_model(self) -> None:
        code, result, summary = self.consult(
            "--question", "Why?", "--model", "gpt-5.6-luna", "--dry-run", "--format", "json"
        )
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(result["status"], "dry_run")
        self.assertIn('sandbox_mode="read-only"', result["command"])
        self.assertFalse(self.capture_path.exists())
        self.assertEqual(json.loads(summary)["status"], "dry_run")

    def test_detached_consultation_runs_the_resolved_model_and_publishes_an_event(self) -> None:
        env = self.environment()
        code, _result, summary = self.consult(
            "--question", "Why?", "--model", "gpt:luna", "--detach", "--notify", "none", env=env
        )
        self.assertEqual(code, 0)
        lines = dict(line.split("=", 1) for line in summary.splitlines() if "=" in line)
        self.assertEqual(lines["AGENT_JOB_STATUS"], "started")
        request = json.loads((Path(lines["AGENT_JOB"]).parent / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (request["model"], request["requested_model"]), ("gpt-5.6-luna", "gpt:luna"), "resolved before detaching"
        )
        event_path = Path(lines["AGENT_EVENT"])
        result_path = Path(lines["AGENT_RESULT"])
        deadline = time.monotonic() + 30
        while not event_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(event_path.exists(), "the background consultation must publish its event")
        event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(
            (event["status"], event["task_outcome"], event["result_path"]), ("answered", "answered", str(result_path))
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(
            (result["answer"], result["model"], result["requested_model"]),
            ("Use a shared lock file; unlinking it races.", "gpt-5.6-luna", "gpt:luna"),
        )
        output = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(output):
            self.assertEqual(RUNNER.events_main(["wait", str(event_path), "--timeout", "5s"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "answered")

    def test_investigate_reads_the_repository_and_returns_findings_with_evidence(self) -> None:
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        code, result, summary = self.consult(
            "--question",
            "Why does the lease break?",
            "--read",
            str(self.repository),
            "--model",
            "gpt-5.6-luna",
            env=env,
        )
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(
            (result["intent"], result["read"], result["workspace"], result["timeout"]),
            ("investigate", str(self.repository), str(self.repository), "20m"),
        )
        self.assertEqual(result["sections"]["FINDINGS"], "- high: lock.py:3 unlinks the lock while held.")
        self.assertIn("os.unlink", result["sections"]["EVIDENCE"])
        self.assertEqual((result["answer"], result["answer_format"]), (result["sections"]["FINDINGS"], "structured"))
        self.assertEqual(result["repository_delta"], {"changed_paths": [], "tracked_paths_changed": []})
        self.assertIn("read tools", result["tool_policy"])
        capture = self.capture()
        self.assertEqual(capture["cwd"], str(self.repository))
        self.assertEqual(capture["argv"][:3], ["exec", "-C", str(self.repository)])
        self.assertIn('sandbox_mode="read-only"', capture["argv"])
        self.assertIn(f"investigating why something happens in the repository at {self.repository}", capture["stdin"])
        self.assertIn("### FINDINGS", capture["stdin"])
        self.assertIn("--- FINDINGS ---", summary)
        self.assertIn("--- EVIDENCE ---", summary)

    def test_review_captures_the_diff_and_reads_with_claude_read_tools_only(self) -> None:
        (self.repository / "lock.py").write_text("import os\n\ndef release(path):\n    pass\n", encoding="utf-8")
        (self.repository / "notes.md").write_text("untracked\n", encoding="utf-8")
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        code, result, _summary = self.consult(
            "--question",
            "Is this change safe?",
            "--intent",
            "review",
            "--read",
            str(self.repository),
            "--diff",
            "--attach",
            str(self.repository / "notes.md"),
            "--model",
            "opus",
            env=env,
        )
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual((result["intent"], result["diff_ref"]), ("review", "HEAD"))
        capture = self.capture()
        argv = capture["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(capture["cwd"], str(self.repository))
        prompt = capture["stdin"]
        self.assertIn("## Change under review (git diff HEAD)", prompt)
        self.assertIn("?? notes.md", prompt)
        self.assertIn("-    os.unlink(path)\n+    pass", prompt)
        self.assertIn("### notes.md\n", prompt, "attachments inside the repository are labelled by relative path")
        self.assertIn("report findings, do not fix them", prompt)

    def test_read_intents_need_a_repository_and_diff_needs_read(self) -> None:
        code, result, _ = self.consult("--question", "Why?", "--intent", "investigate")
        self.assertEqual(code, 4)
        self.assertIn("needs --read", result["error"])
        code, result, _ = self.consult("--question", "Why?", "--diff")
        self.assertEqual(code, 4)
        self.assertIn("--diff needs --read", result["error"])
        code, result, _ = self.consult("--question", "Why?", "--read", str(self.root / "missing"))
        self.assertEqual(code, 4)

    def test_a_repository_change_during_reading_is_reported_not_judged(self) -> None:
        env = self.environment(FAKE_RESPONSE=FINDINGS, FAKE_WRITE_FILE=str(self.repository / "scratch.txt"))
        code, result, summary = self.consult(
            "--question", "Why?", "--read", str(self.repository), "--model", "gpt-5.6-luna", env=env
        )
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["repository_delta"]["changed_paths"], ["scratch.txt"])
        self.assertIn("AGENT_NOTE=repository_changed_during_consultation", summary)

    def test_a_reader_waits_for_no_one_but_is_refused_while_a_writer_holds_the_repository(self) -> None:
        support = RUNNER.bundled_module("execution_support")
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        with (
            mock.patch.dict(os.environ, env),
            support.workspace_lease(self.repository, RUNNER.default_agent_cache_dir()),
        ):
            code, result, _ = self.consult(
                "--question", "Why?", "--read", str(self.repository), "--model", "gpt-5.6-luna", env=env
            )
        self.assertEqual(code, 27)
        self.assertIn("another executor owns the worktree", result["error"])

    def test_antigravity_denied_action_is_explained_and_the_session_can_be_nudged(self) -> None:
        env = self.environment(
            FAKE_RESPONSE="", FAKE_AGY_DENIED='[{"action": "command", "display_name": "RunCommand"}]'
        )
        code, result, summary = self.consult(
            "--question", "Why?", "--read", str(self.repository), "--model", "gemini-3.8-flash-medium", env=env
        )
        self.assertEqual((code, result["status"], result["denied_actions"]), (3, "empty_output", ["RunCommand"]))
        self.assertIn("denying RunCommand", result["error"])
        self.assertIn("AGENT_ERROR=Antigravity ended the turn", summary)
        self.assertIn("You cannot run commands", self.capture()["stream_input"][0]["message"]["content"])
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        code, nudged, _ = self.consult(
            "--question", "Answer from what you read.", "--follow-up", result["result_path"], env=env
        )
        self.assertEqual((code, nudged["status"], nudged["read"]), (0, "answered", str(self.repository)))
        self.assertEqual(self.capture()["argv"][:2], ["--conversation", "22222222-2222-2222-2222-222222222222"])

    def test_opencode_reading_keeps_only_the_read_tools(self) -> None:
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        code, result, _ = self.consult(
            "--question", "Why?", "--read", str(self.repository), "--model", "glm:flash", env=env
        )
        self.assertEqual(code, 0, result.get("error"))
        config = json.loads(self.capture()["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual([name for name, enabled in config["tools"].items() if enabled], [])
        self.assertTrue(
            {"read", "glob", "grep"}.isdisjoint(config["tools"]), "read tools stay at their default, enabled"
        )
        self.assertFalse(config["tools"]["write"] or config["tools"]["shell"] or config["tools"]["execute"])
        self.assertEqual(self.capture()["cwd"], str(self.repository))

    def test_artifacts_never_land_inside_the_repository_being_read(self) -> None:
        code, result, _ = self.consult(
            "--question",
            "Why?",
            "--read",
            str(self.repository),
            "--model",
            "gpt-5.6-luna",
            "--out-dir",
            str(self.repository / "consult-out"),
        )
        self.assertEqual(code, 4)
        self.assertIn("--out-dir must be outside the --read repository", result["error"])
        self.assertFalse((self.repository / "consult-out").exists())

    def test_follow_up_refuses_a_session_whose_workspace_is_gone(self) -> None:
        code, first, _ = self.consult("--question", "Which lock?", "--model", "glm:flash")
        self.assertEqual(code, 0, first.get("error"))
        shutil.rmtree(first["workspace"])
        code, result, _ = self.consult("--question", "Why?", "--follow-up", first["result_path"])
        self.assertEqual(code, 4)
        self.assertIn("no longer exists", result["error"])

    def test_detached_reading_consultation_belongs_to_its_repository(self) -> None:
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        code, _result, summary = self.consult(
            "--question",
            "Why?",
            "--read",
            str(self.repository),
            "--model",
            "gpt-5.6-luna",
            "--detach",
            "--notify",
            "none",
            env=env,
        )
        self.assertEqual(code, 0)
        lines = dict(line.split("=", 1) for line in summary.splitlines() if "=" in line)
        self.assertEqual(
            json.loads(Path(lines["AGENT_JOB"]).read_text(encoding="utf-8"))["repository"], str(self.repository)
        )
        output = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(output):
            self.assertEqual(
                RUNNER.events_main(["wait", lines["AGENT_EVENT"], "--cwd", str(self.repository), "--timeout", "30s"]), 0
            )
        signal = json.loads(output.getvalue())
        self.assertEqual((signal["status"], signal["repository"]), ("answered", str(self.repository)))

    def test_only_codex_is_offered_read_only_commands(self) -> None:
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        self.consult("--question", "Why?", "--read", str(self.repository), "--model", "gpt-5.6-luna", env=env)
        self.assertIn("run read-only commands there", self.capture()["stdin"])
        env = self.environment(FAKE_RESPONSE=FINDINGS)
        self.consult("--question", "Why?", "--read", str(self.repository), "--model", "opus", env=env)
        self.assertIn("You cannot run commands", self.capture()["stdin"])

    def test_parse_sections_tolerates_decoration_and_ignores_fenced_headings(self) -> None:
        sections, shape = CONSULT.parse_sections("preamble\n**ANSWER**\nyes\n\n## Uncertainty:\nnone\n", "question")
        self.assertEqual((sections, shape), ({"ANSWER": "yes", "UNCERTAINTY": "none"}, "structured"))
        sections, shape = CONSULT.parse_sections("### ANSWER\nyes, because of X\n", "question")
        self.assertEqual((sections, shape), ({"ANSWER": "yes, because of X"}, "partial"))
        quoted = (
            "### ANSWER\nThe contract says:\n```\n### ANSWER\n### UNCERTAINTY\n```\nso use it.\n### UNCERTAINTY\nnone\n"
        )
        sections, shape = CONSULT.parse_sections(quoted, "question")
        self.assertEqual(shape, "structured")
        self.assertTrue(
            sections["ANSWER"].startswith("The contract says:") and sections["ANSWER"].endswith("so use it.")
        )
        self.assertEqual(sections["UNCERTAINTY"], "none")
        sections, shape = CONSULT.parse_sections(FINDINGS, "review")
        self.assertEqual((shape, list(sections)), ("structured", ["FINDINGS", "EVIDENCE", "UNCERTAINTY"]))


if __name__ == "__main__":
    unittest.main()
