"""Durable bridge between conductor-owned native tools and the shared audit.

The native host starts/waits/cancels its own tool. This process owns the worktree
lease and captures the audit while that tool is active; it never launches a peer
CLI. Its file wait consumes no model tokens.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def process_identity(pid: int) -> str | None:
    # The command can change across exec() immediately after Popen. Birth time
    # remains stable and avoids treating that startup transition as PID reuse.
    proc = Path(f"/proc/{pid}/stat")
    if proc.is_file():
        try:
            fields = proc.read_text().rsplit(")", 1)[1].split()
            return "proc-start-ticks:" + fields[19]
        except (OSError, IndexError):
            return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def process_state(run: dict[str, Any]) -> str:
    if not run.get("pid") or not run.get("process_identity"):
        return "unknown"
    current = process_identity(run["pid"])
    if current is None:
        return "exited"
    return "active" if current == run["process_identity"] else "pid_reused"


def start(directory: Path, run: dict[str, Any], runner: Any) -> None:
    folder = directory / "runs" / run["run_id"]
    with runner.open_private_binary(folder / "guard.stdout") as stdout, runner.open_private_binary(folder / "guard.stderr") as stderr:
        child = subprocess.Popen([sys.executable, str(Path(__file__)), str(directory), run["run_id"]],
                                 stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
    run.update(pid=child.pid, process_identity=process_identity(child.pid), transport="native")
    until = time.monotonic() + 15
    while time.monotonic() < until:
        if (folder / "ready.json").exists():
            run["status"] = "reserved"
            run["native_instructions"] = {
                "dispatch": "Use the current host's native tool exactly once. Record its handle with native-attach before doing other work.",
                "deadline": "Host must stop its tool at the task deadline using native interruption; guard retains lease until terminal receipt.",
                "recovery": "Recover the native tool handle/event once; never spawn a replacement for an uncertain reservation.",
                "report": "Use the rendered brief and five report headings; save actual tool output for native-complete.",
                "completion_owner": "current_host", "brief_path": str(folder / "brief.md"),
            }
            return
        if child.poll() is not None:
            raise ValueError("native audit guard exited before readiness; inspect guard.stderr")
        time.sleep(.05)
    raise ValueError("native audit guard did not become ready; recover reservation before native dispatch")


def complete(directory: Path, state: dict[str, Any], run: dict[str, Any], packet: dict[str, Any], runner: Any) -> None:
    if not run.get("native_handle") or packet.get("native_handle") != run["native_handle"]:
        raise ValueError("completion must identify the exact attached native handle")
    if not packet.get("event_id") or not isinstance(packet["event_id"], str):
        raise ValueError("native completion requires the exact observed host event ID")
    if packet.get("terminal") is not True:
        raise ValueError("host must observe a terminal tool result before releasing the audit lease")
    folder = directory / "runs" / run["run_id"]
    result_path = Path(run["result_path"])
    if result_path.is_file():
        existing = json.loads(result_path.read_text())
        if existing.get("native_event_id") != packet["event_id"]:
            raise ValueError("native completion event differs from the persisted result")
        return
    if process_state(run) != "active":
        raise ValueError("native guard is not active; preserve uncertainty and investigate, do not forge a completed audit")
    request = folder / "completion.json"
    if request.exists() and json.loads(request.read_text()) != packet:
        raise ValueError("completion already reserved with different content")
    runner.write_json_atomic(request, packet)
    until = time.monotonic() + 60
    while time.monotonic() < until:
        if result_path.exists():
            return
        if process_state(run) != "active":
            raise ValueError("native guard exited without result; inspect artifacts and recover")
        time.sleep(.1)
    raise ValueError("native verification still active; recover the same run, do not repeat native work")


def audit(state: dict[str, Any], run: dict[str, Any], ready: dict[str, Any], packet: dict[str, Any], runner: Any, support: Any, lease: int) -> dict[str, Any]:
    repository = Path(state["repository"])
    folder = Path(run["result_path"]).parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    spec = json.loads((folder.parent / "task-spec.json").read_text())
    before, identity = ready["git_before"], ready["git_identity_before"]
    after_executor = runner.git_state(repository, spec["track_paths"])
    identity_executor = runner.git_identity(repository)
    delta = runner.state_delta(before, after_executor)
    report_path = Path(packet["report_path"]).expanduser().resolve(strict=True)
    report = report_path.read_text(encoding="utf-8")
    raw, extracted = runner.extract_raw_final_report(report)
    violations = runner.report_contract_violations(raw, extracted)
    outcome = runner.parse_reported_outcome(raw, extracted)
    consultation = support.consultation_from_report(raw, outcome)
    if consultation["status"] == "invalid":
        violations.append("invalid_consultation_request")
    allowed = list(dict.fromkeys(spec["allow_paths"] + spec["track_paths"]))
    status, code = runner.classify_result(history_violations=runner.find_history_violations(identity, identity_executor),
        scope_violations=runner.find_scope_violations(delta, allowed), timed_out=runner.deadline_remaining(state["deadline"]) <= 0,
        executor_exit_code=0 if packet.get("tool_status") == "completed" else 1,
        stdout_present=bool(report.strip()), report_extracted=extracted, expect_changes=bool(allowed),
        workspace_changed=before["fingerprint"] != after_executor["fingerprint"], reported_outcome=outcome,
        report_contract_valid=not violations)
    verification = runner.verification_summary(run["verification_commands"], status="skipped", skipped_reason=status)
    if status == "completed":
        verification = runner.run_verification_commands(run["verification_commands"], repo=repository, out_dir=folder,
                timeout_seconds=600, deadline=state["deadline"], lease=lease)
    after, after_identity = runner.git_state(repository, spec["track_paths"]), runner.git_identity(repository)
    history = runner.find_history_violations(identity, after_identity)
    delta = runner.state_delta(before, after)
    scope = runner.find_scope_violations(delta, allowed)
    if history:
        status, code = "history_violation", 21
    elif scope:
        status, code = "scope_violation", 22
    elif after["fingerprint"] != after_executor["fingerprint"]:
        status, code = "verification_mutation", 26
    elif verification["status"] in {"failed", "timed_out"}:
        status, code = "verification_failed", 25
    usage = runner.bundled_module("usage").empty()
    usage["gaps"].append("native_host_did_not_expose_verified_provider_usage")
    return {"schema": runner.RESULT_SCHEMA, "status": status, "exit_code": code,
        "repository": state["repository"], "engine": run["engine"], "model": run["model"],
        "requested_model": run["model"], "resolved_model": packet.get("observed_model"),
        "effort": {"requested": run.get("effort"), "bound": run.get("effort"), "observed": packet.get("observed_effort"), "binding": "host_native_tool"},
        "variant": run.get("variant"), "billing_mode": run["billing_mode"],
        "session_id": packet["native_handle"], "native_event_id": packet["event_id"], "transport": "native",
        "git_before": before, "git_identity_before": identity, "git_after_executor": after_executor,
        "git_after": after, "git_identity_after": after_identity, "run_delta": delta,
        "scope_violations": scope, "history_violations": history, "tracked_paths": spec["track_paths"],
        "allowed_paths": allowed, "verification_commands": run["verification_commands"],
        "verification": verification, "report_contract_violations": violations, "reported_outcome": outcome,
        "final_message": runner.bound_final_message(raw), "consultation": consultation, "usage": usage,
        "finished_at": runner.utc_now(), "started_at": run["reserved_at"],
        "duration_seconds": (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(run["reserved_at"].replace("Z", "+00:00"))).total_seconds(),
        "native_receipt": packet, "permission_basis": "existing_host_authority; unchanged_by_bridge"}


def main() -> None:
    directory, run_id = Path(sys.argv[1]), sys.argv[2]
    spec = importlib.util.spec_from_file_location("native_runner", Path(__file__).with_name("run_agent.py"))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    support = runner.bundled_module("execution_support")
    state = json.loads((directory / "task.json").read_text())
    run = next(value for value in state["runs"] if value["run_id"] == run_id)
    folder = directory / "runs" / run_id
    repository = Path(state["repository"])
    with support.workspace_lease(repository, runner.default_agent_cache_dir()) as lease:
        task = json.loads((folder / "task-spec.json").read_text())
        before, identity = runner.git_state(repository, task["track_paths"]), runner.git_identity(repository)
        if run.get("resume_result"):
            previous = json.loads(Path(run["resume_result"]).read_text())
            if previous["git_after"]["fingerprint"] != before["fingerprint"] or previous["git_identity_after"] != identity:
                raise ValueError("native correction workspace differs from the prior audited result")
        ready = {"run_id": run_id, "git_before": before, "git_identity_before": identity, "pid": os.getpid(), "process_identity": process_identity(os.getpid()), "ready_at": runner.utc_now()}
        runner.write_json_atomic(folder / "ready.json", ready)
        while not (folder / "completion.json").exists():
            if runner.deadline_remaining(state["deadline"]) <= 0 and not (folder / "deadline.json").exists():
                runner.write_json_atomic(folder / "deadline.json", {"run_id": run_id, "action": "host_interrupt_required", "at": runner.utc_now()})
            time.sleep(.2)
        packet = json.loads((folder / "completion.json").read_text())
        result = audit(state, run, ready, packet, runner, support, lease)
        runner.write_json_atomic(Path(run["result_path"]), result)


if __name__ == "__main__":
    main()
