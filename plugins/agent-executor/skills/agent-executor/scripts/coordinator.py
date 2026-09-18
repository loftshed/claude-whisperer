#!/usr/bin/env python3
"""Record bounded conductor decisions and dispatch through the audited executor.

The host agent supplies decisions and reviews. This CLI does not invent a model
ranking, run a model to poll another model, or retry an uncertain dispatch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


def module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location("executor_" + name, Path(__file__).with_name(name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


RUNNER = module("run_agent")
SUPPORT = module("execution_support")
EVIDENCE = module("evidence")
POLICY = module("conductor_policy")
NATIVE = module("native_bridge")
SCHEMA = "agent-executor.task.v1"


class CoordinationError(ValueError):
    pass


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CoordinationError(f"expected JSON object: {path}")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise CoordinationError("timestamps must include a timezone")
    return result


def native(host: str, engine: str, model_id: str) -> bool:
    if host == engine and host in {"codex", "agy", "claude"}:
        return True
    leaf = model_id.rsplit("/", 1)[-1].lower()
    return any((
        host == "codex" and bool(re.match(r"(?:gpt-\d|codex)", leaf)),
        host == "agy" and leaf.startswith("gemini"),
        host == "claude" and leaf.startswith("claude"),
    ))


def choose_route(packet: dict[str, Any]) -> dict[str, str]:
    for field in ("host", "engine", "model"):
        if not isinstance(packet.get(field), str) or not packet[field]:
            raise CoordinationError(f"route requires {field}")
    if packet.get("consultation") is True:
        if not packet.get("decision_needed") or not packet.get("question"):
            raise CoordinationError("consultation requires a question and decision needed")
        owner = "current"
        action = "consult"
    elif not all(packet.get(field) is True for field in ("context_portable", "independent_acceptance", "handoff_worthwhile")):
        return {"action": "continue", "owner": "current", "transport": "current_session"}
    else:
        owner, action = "fresh_worker", "delegate"
    return {"action": action, "owner": owner, "transport": "native" if native(packet["host"], packet["engine"], packet["model"]) else "executor"}


def check_evidence(path: Path, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Check artifacts, freshness, and version metadata; never semantic truth."""
    evidence = read(path)
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(evidence.get("conclusion"), str) or not evidence["conclusion"].strip():
        raise CoordinationError("evidence requires a conclusion")
    versions = evidence.get("versions")
    sources = evidence.get("sources")
    if not isinstance(versions, dict) or not isinstance(sources, list) or not sources:
        raise CoordinationError("evidence requires observed versions and at least one source")
    for source in sources:
        if not isinstance(source, dict):
            raise CoordinationError("source must be an object")
        for field in ("location", "component", "version", "retrieved_at", "artifact", "sha256"):
            if not isinstance(source.get(field), str) or not source[field]:
                raise CoordinationError(f"source requires {field}")
        if versions.get(source["component"]) != source["version"]:
            raise CoordinationError("source version differs from the observed version")
        age = (now - timestamp(source["retrieved_at"])).total_seconds()
        if not 0 <= age <= 24 * 60 * 60:
            raise CoordinationError("source is stale or dated in the future; retrieve it again")
        artifact = Path(source["artifact"]).expanduser()
        if not artifact.is_absolute() or not artifact.is_file() or digest(artifact) != source["sha256"]:
            raise CoordinationError("evidence artifact is missing or its content changed")
    return {"path": str(path.resolve()), "sha256": digest(path), "conclusion": evidence["conclusion"], "metadata_checked_at": now.isoformat(), "semantic_review": "conductor_responsibility"}


def save(directory: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = RUNNER.utc_now()
    RUNNER.write_json_atomic(directory / "task.json", state)


def load(directory: Path) -> dict[str, Any]:
    state = read(directory / "task.json")
    if state.get("schema") != SCHEMA:
        raise CoordinationError("unsupported task schema")
    return state


def import_result(run: dict[str, Any], state: dict[str, Any]) -> bool:
    path = Path(run["result_path"])
    if not path.is_file():
        return False
    result = read(path)
    if result.get("schema") != RUNNER.RESULT_SCHEMA or result.get("repository") != state["repository"]:
        raise CoordinationError("result identity does not match task")
    if result.get("engine") != run["engine"] or result.get("model") != run["model"]:
        raise CoordinationError("result executor does not match reserved dispatch")
    if result.get("status") == "dry_run" or not result.get("finished_at"):
        raise CoordinationError("result is not a terminal executor invocation")
    run.update(status="finished", runner_status=result["status"], result_sha256=digest(path), usage=result.get("usage"), session_id=result.get("session_id"))
    if result.get("native_event_id"):
        run["native_event_id"] = result["native_event_id"]
    POLICY.settle(state, run, result)
    if run.get("event_path") and not Path(run["event_path"]).exists():
        RUNNER.publish_completion_event(result=result, result_path=path, event_path=Path(run["event_path"]), notification_mode="none", completion_hook=None)
    return True


def recover(directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    for run in state["runs"]:
        if run["status"] in {"dispatching", "uncertain", "active", "reserved"}:
            if not import_result(run, state):
                if run.get("transport") == "native" and not run.get("process_identity"):
                    ready = directory / "runs" / run["run_id"] / "ready.json"
                    if ready.is_file():
                        checkpoint = read(ready)
                        if checkpoint.get("run_id") == run["run_id"]:
                            run.update(pid=checkpoint["pid"], process_identity=checkpoint["process_identity"])
                run["process_state"] = NATIVE.process_state(run)
                run["status"] = "active" if run["process_state"] == "active" else "uncertain"
                run["recovery"] = "Use the recorded native handle or process; no terminal result yet. Never relaunch this request ID."
    save(directory, state)
    return state


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    renderer = RUNNER.brief_renderer_module()
    spec = renderer.validate_spec(read(args.task_spec))
    repository = RUNNER.git_root(Path(spec["repository_root"]).resolve())
    if repository != Path(spec["repository_root"]).resolve():
        raise CoordinationError("task spec must name the repository root")
    spec["repository_root"] = str(repository)
    directory = args.task_dir.expanduser().resolve()
    if directory == repository or repository in directory.parents:
        raise CoordinationError("task directory must be outside the repository")
    duration = RUNNER.parse_duration(args.time_budget)
    now = dt.datetime.now(dt.timezone.utc)
    state = {
        "schema": SCHEMA, "task_id": uuid.uuid4().hex, "repository": str(repository),
        "host": args.host, "status": "active", "created_at": now.isoformat(),
        "deadline": (now + dt.timedelta(seconds=duration)).isoformat(),
        "limits": {"consultations": 3, "challenges": 1, "corrections": 1},
        "task_spec": spec, "runs": [], "decisions": [],
        "budget": POLICY.invocation_budget(args.invocation_allowance), "cost_ledger": {},
        "evidence": {"sources": {}, "reviews": {}, "resources": {}}, "knowledge": [],
        "evidence_policy": "host_receipts_required",
        "cost_policy": "Measured invocation quota and deadline are enforced. Provider counters/estimates remain separate; no invoice or provider subscription-quota cap is claimed.",
    }
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    save(directory, state)
    return state


def latest_work(state: dict[str, Any]) -> dict[str, Any] | None:
    return next((run for run in reversed(state["runs"]) if run["kind"] in {"implementation", "correction", "verification"}), None)


def reverify(args: argparse.Namespace, directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    """A new host check record never rewrites a failed executor invocation."""
    if not args.reason.strip():
        raise CoordinationError("repeat verification requires a concrete reason")
    prior = next((run for run in state["runs"] if run["request_id"] == args.request_id), None)
    if prior:
        recover(directory, state)
        return prior
    work = next((run for run in state["runs"] if run["run_id"] == args.run_id), None)
    later = state["runs"][state["runs"].index(work) + 1:] if work else []
    if state["status"] != "active" or not work or work["kind"] not in {"implementation", "correction", "verification"} or any(run["kind"] != "verification" for run in later) or any(run["status"] != "finished" for run in state["runs"]):
        raise CoordinationError("reverify requires the latest finished work and no active invocation")
    path = Path(work["result_path"])
    if digest(path) != work["result_sha256"]:
        raise CoordinationError("previous result changed")
    previous = read(path)
    if previous["status"] not in {"completed", "verification_failed"} or previous.get("scope_violations") or previous.get("history_violations"):
        raise CoordinationError("reverification cannot repair a failed executor report or safety violation")
    commands = args.verify_command or previous.get("verification_commands", [])
    if not commands or RUNNER.deadline_remaining(state["deadline"]) <= 0:
        raise CoordinationError("reverification requires checks and remaining task time")
    run_id = uuid.uuid4().hex
    folder = directory / "runs" / run_id / "artifacts"
    folder.mkdir(parents=True, mode=0o700)
    event_path = RUNNER.prepare_completion_event_path()
    run = {"run_id": run_id, "request_id": args.request_id, "kind": "verification", "status": "dispatching",
        "engine": previous["engine"], "model": previous["model"], "result_path": str(folder / "result.json"),
        "reserved_at": RUNNER.utc_now(), "event_id": event_path.stem, "event_path": str(event_path),
        "completion_owner": "coordinator", "transport": "host_checks", "reason": args.reason,
        "verification_of": work["run_id"], "pid": os.getpid(), "process_identity": NATIVE.process_identity(os.getpid())}
    run["intervening_checks"] = [value["run_id"] for value in later]
    repository = Path(state["repository"])
    with SUPPORT.workspace_lease(repository, RUNNER.default_agent_cache_dir()) as lease:
        before, identity = RUNNER.git_state(repository, previous["tracked_paths"]), RUNNER.git_identity(repository)
        if before["fingerprint"] != previous["git_after"]["fingerprint"] or identity != previous["git_identity_after"]:
            raise CoordinationError("work changed since the prior audit; investigate before rechecking")
        state["runs"].append(run)
        save(directory, state)
        started = time.monotonic()
        verification = RUNNER.run_verification_commands(commands, repo=repository, out_dir=folder, timeout_seconds=600, deadline=state["deadline"], lease=lease)
        after, after_identity = RUNNER.git_state(repository, previous["tracked_paths"]), RUNNER.git_identity(repository)
        history = RUNNER.find_history_violations(identity, after_identity)
        changed = before["fingerprint"] != after["fingerprint"]
        delta = RUNNER.state_delta(previous["git_before"], after)
        scope = RUNNER.find_scope_violations(delta, previous["allowed_paths"])
        status = "history_violation" if history else "scope_violation" if scope else "verification_mutation" if changed else "completed" if verification["status"] == "passed" else "verification_failed"
        usage = RUNNER.bundled_module("usage").empty()
        usage.update(status="known", totals={name: 0 for name in usage["totals"]}, cumulative=previous.get("usage", {}).get("cumulative"), coverage="host_checks_only; no_model_invocation", native_cost_usd=0)
        result = {**previous, "status": status, "task_outcome": RUNNER.task_outcome_for_status(status),
            "exit_code": {"completed": 0, "history_violation": 21, "scope_violation": 22, "verification_mutation": 26}.get(status, 25), "started_at": run["reserved_at"], "finished_at": RUNNER.utc_now(),
            "duration_seconds": time.monotonic() - started, "verification": verification, "verification_commands": commands,
            "git_after": after, "git_identity_after": after_identity, "history_violations": history, "scope_violations": scope, "run_delta": delta,
            "verification_changed_workspace": changed, "verification_delta": RUNNER.state_delta(before, after),
            "usage": usage, "tariff_estimate": None, "executor_attempts": [], "transport": "host_checks",
            "verification_of": {"path": str(path), "sha256": work["result_sha256"], "reason": args.reason},
            "model_invoked": False}
        RUNNER.write_json_atomic(Path(run["result_path"]), result)
    import_result(run, state)
    save(directory, state)
    return run


def dispatch(args: argparse.Namespace, directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.request_id):
        raise CoordinationError("request ID must be 1-80 letters, digits, underscores, or hyphens")
    prior = next((run for run in state["runs"] if run["request_id"] == args.request_id), None)
    if prior is not None:
        # A repeated ID never launches another process, including after a crash.
        recover(directory, state)
        return prior
    if state["status"] != "active":
        raise CoordinationError("task has been accepted or returned to the conductor")
    if any(run["status"] not in {"finished", "cancelled"} for run in state["runs"]):
        raise CoordinationError("recover the unresolved dispatch before starting new work")
    if RUNNER.deadline_remaining(state["deadline"]) <= 0:
        raise CoordinationError("task deadline exhausted; conductor must investigate or replan")
    work = latest_work(state)
    kind = args.kind
    if kind == "implementation" and work:
        raise CoordinationError("initial implementation already dispatched; use a bounded correction")
    if kind in {"consultation", "challenge"}:
        if sum(run["kind"] in {"consultation", "challenge"} for run in state["runs"]) >= state["limits"]["consultations"]:
            raise CoordinationError("consultation budget exhausted; conductor takes over")
        if kind == "challenge" and (not any(run["kind"] == "consultation" for run in state["runs"]) or any(run["kind"] == "challenge" for run in state["runs"])):
            raise CoordinationError("one challenge is allowed after a consultation")
        if args.packet is None:
            raise CoordinationError("consultation requires --packet")
        packet = SUPPORT.validate_consultation(read(args.packet))
        spec = {
            **state["task_spec"], "objective": packet["question"],
            "current_state": "Decision needed: " + packet["decision_needed"],
            "plan": ["Inspect the evidence and applicable versioned official documentation. Do not edit.", "Return a cited conclusion, limitations, and one discriminating experiment if unresolved."],
            "allow_paths": [], "track_paths": [], "development_checks": [],
            "acceptance_criteria": ["Answer the question with applicable sources and identify remaining uncertainty."],
            "context_packet": {"decisions": packet["hypotheses"], "failed_attempts": packet["attempts"], "evidence": packet["observations"], "versions": packet["versions"]},
        }
    else:
        spec = dict(state["task_spec"])
    engine, model_id, effort, variant = args.engine, args.model, args.effort, args.variant
    verification = args.verify_command
    billing = args.billing_mode
    previous_result = None
    evidence_record = None
    if kind == "correction":
        if not work or work["status"] != "finished" or any(run["kind"] == "correction" for run in state["runs"]):
            raise CoordinationError("one correction requires a finished implementation")
        if args.evidence is None and not args.review_id:
            raise CoordinationError("correction requires conductor-reviewed --evidence")
        previous_result = read(Path(work["result_path"]))
        if digest(Path(work["result_path"])) != work["result_sha256"] or not previous_result.get("session_id"):
            raise CoordinationError("previous result changed or has no exact session")
        SUPPORT.validate_resume(previous_result, {"repository": state["repository"]})
        engine, model_id = previous_result["engine"], previous_result["model"]
        effort, variant = previous_result.get("effort", {}).get("bound"), previous_result.get("variant")
        verification = previous_result.get("verification_commands", [])
        billing = previous_result.get("billing_mode", "unknown")
        if any(value is not None for value in (args.engine, args.model, args.effort, args.variant)) or args.verify_command:
            raise CoordinationError("correction inherits engine, model, effort, variant, and verification; omit overrides")
        if state.get("evidence_policy") == "host_receipts_required":
            proof = EVIDENCE.receipt(state, args.review_id, RUNNER)
            evidence_record = {"review_id": proof["review_id"]}
        else:
            evidence_record = check_evidence(args.evidence)
            proof = read(args.evidence)
        spec["plan"] = ["Continue the original objective in this exact session.", "Apply the conductor's reviewed evidence: " + proof["conclusion"]]
        spec["context_packet"] = {**spec.get("context_packet", {}), "evidence": ["Conductor evidence: " + json.dumps(proof, ensure_ascii=True)]}
    if not engine or not model_id:
        raise CoordinationError("dispatch requires an exact --engine and --model")
    if engine == "codex" and effort is None and args.transport != "native":
        raise CoordinationError("recorded Codex dispatch requires explicit --effort for reproducible correction")
    is_native = native(state["host"], engine, model_id)
    if is_native and args.transport != "native":
        raise CoordinationError("this executor is native to the host; use native tools and retain ownership there")
    if not is_native and args.transport == "native":
        raise CoordinationError("native transport requires the current host's native model family")
    if engine == "claude" and args.transport != "native":
        raise CoordinationError("Claude uses the native host bridge; no Claude CLI adapter is installed")
    if previous_result and previous_result.get("transport") == "native" and args.transport != "native":
        raise CoordinationError("native correction must use the recorded native host session")
    run_id = uuid.uuid4().hex
    run_dir = directory / "runs" / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    spec_path = run_dir / "task-spec.json"
    RUNNER.write_json_atomic(spec_path, spec)
    (run_dir / "brief.md").write_text(RUNNER.brief_renderer_module().render_brief(spec), encoding="utf-8")
    result_path = run_dir / "artifacts" / "result.json"
    event_path = RUNNER.prepare_completion_event_path()
    command = [sys.executable, str(Path(__file__).with_name("run_agent.py")), "--cwd", state["repository"], "--task-spec", str(spec_path), "--engine", engine, "--model", model_id, "--out-dir", str(result_path.parent), "--deadline", state["deadline"], "--billing-mode", billing, "--retry-read-only", "0", "--completion-event", str(event_path)]
    if effort:
        command += ["--effort", effort]
    if variant:
        command += ["--variant", variant]
    if kind in {"implementation", "correction"}:
        for check in verification:
            command += ["--verify-command", check]
        if spec["allow_paths"] or spec["track_paths"]:
            command += ["--expect-changes"]
    if previous_result:
        command += ["--session", previous_result["session_id"], "--resume-result", work["result_path"]]
    run = {
        "run_id": run_id, "request_id": args.request_id, "kind": kind,
        "status": "dispatching", "engine": engine, "model": model_id,
        "result_path": str(result_path), "command": command,
        "reserved_at": RUNNER.utc_now(), "evidence": evidence_record,
        "transport": args.transport, "effort": effort, "variant": variant, "billing_mode": billing,
        "verification_commands": verification, "event_id": event_path.stem, "event_path": str(event_path),
        "completion_owner": "coordinator", "resume_result": work["result_path"] if previous_result else None,
        "required_native_handle": previous_result["session_id"] if previous_result and args.transport == "native" else None,
    }
    if "budget" not in state:
        raise CoordinationError("older task has no enforceable allowance; finish/review it or create a new bounded task")
    POLICY.reserve(state, args.request_id)
    state["runs"].append(run)
    save(directory, state)  # Durable intent precedes the external side effect.
    if args.transport == "native":
        NATIVE.start(directory, run, RUNNER)
        save(directory, state)
        return run
    with RUNNER.open_private_binary(run_dir / "runner.stdout") as stdout, RUNNER.open_private_binary(run_dir / "runner.stderr") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL)
        run["pid"] = process.pid
        run["process_identity"] = NATIVE.process_identity(process.pid)
        save(directory, state)
        run["process_exit_code"] = process.wait()
    if not import_result(run, state):
        run["status"] = "uncertain"
        run["recovery"] = "Runner exited without a terminal result; inspect artifacts before taking over. No automatic retry."
    save(directory, state)
    return run


def review(args: argparse.Namespace, directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    run = next((value for value in state["runs"] if value["run_id"] == args.run_id), None)
    if run is None or run["status"] != "finished":
        raise CoordinationError("review requires an exact finished run")
    result_path = Path(run["result_path"])
    if digest(result_path) != run["result_sha256"]:
        raise CoordinationError("result changed since collection")
    result = read(result_path)
    record = {"run_id": args.run_id, "decision": args.decision, "at": RUNNER.utc_now()}
    if args.decision in {"accept", "supported"}:
        if result["status"] != "completed" or result.get("scope_violations") or result.get("history_violations"):
            raise CoordinationError("only a completed, audited run can be accepted or supported")
        if args.evidence is None and not args.review_id:
            raise CoordinationError("review requires conductor evidence")
        if state.get("evidence_policy") == "host_receipts_required":
            evidence = EVIDENCE.receipt(state, args.review_id, RUNNER, acceptance=args.decision == "accept")
            record["evidence"] = {"review_id": evidence["review_id"], "owner": evidence["owner"]}
        else:
            evidence = read(args.evidence)
            record["evidence"] = check_evidence(args.evidence)
        if args.decision == "accept":
            if run is not latest_work(state):
                raise CoordinationError("accept only the latest implementation or correction")
            if state.get("evidence_policy") != "host_receipts_required" and evidence.get("criteria") != state["task_spec"]["acceptance_criteria"]:
                raise CoordinationError("evidence must list every original acceptance criterion in order")
            repository = Path(state["repository"])
            with SUPPORT.workspace_lease(repository, RUNNER.default_agent_cache_dir()):
                if RUNNER.git_state(repository, result["tracked_paths"])["fingerprint"] != result["git_after"]["fingerprint"] or RUNNER.git_identity(repository) != result["git_identity_after"]:
                    raise CoordinationError("workspace changed since verification")
            state["status"] = "accepted"
        elif run["kind"] not in {"consultation", "challenge"}:
            raise CoordinationError("supported is a consultation decision")
    elif args.decision == "takeover":
        state["status"] = "conductor_takeover"
    state["decisions"].append(record)
    save(directory, state)
    return record


def host_action(args: argparse.Namespace, directory: Path, state: dict[str, Any]) -> Any:
    action = args.action
    if action in {"verify-evidence", "record-decision"} and any(run["status"] not in {"finished", "cancelled"} for run in state["runs"]):
        raise CoordinationError("finish/recover the active audit before evidence review")
    if action == "next":
        observation = read(args.packet) if args.packet else {}
        if observation.get("review_id"):
            EVIDENCE.receipt(state, observation["review_id"], RUNNER)
        return POLICY.next_action(state, observation, RUNNER.deadline_remaining(state["deadline"]))
    if action == "knowledge":
        for entry in state.get("knowledge", []):
            if entry.get("evidence_review_id"):
                try:
                    EVIDENCE.receipt(state, entry["evidence_review_id"], RUNNER)
                except ValueError as error:
                    entry["invalidated"] = str(error)
        save(directory, state)
        return state.get("knowledge", [])
    if action == "capture":
        result = EVIDENCE.capture(state, directory, args, RUNNER, SUPPORT)
    elif action == "verify-evidence":
        result = EVIDENCE.verify(state, read(args.packet), RUNNER)
    elif action == "register":
        result = EVIDENCE.register(state, read(args.packet))
    elif action == "record-decision":
        proof = EVIDENCE.receipt(state, args.review_id, RUNNER) if args.review_id else None
        result = POLICY.decision(state, read(args.packet), proof)
    else:
        run = next((value for value in state["runs"] if value["run_id"] == args.run_id), None)
        if run is None:
            raise CoordinationError("unknown run ID")
        if action == "capture-checks":
            result = EVIDENCE.capture_checks(state, directory, run, args.component, args.version)
        elif action == "ack":
            if run["status"] != "finished" or not any(value["run_id"] == run["run_id"] for value in state["decisions"]):
                raise CoordinationError("persist a review decision before acknowledging its exact event")
            if args.event_id != run.get("event_id"):
                raise CoordinationError("ack requires the exact recorded event ID")
            event = RUNNER.read_completion_event(Path(run["event_path"]))
            if event is None or event["result_path"] != run["result_path"] or digest(Path(run["result_path"])) != run["result_sha256"]:
                raise CoordinationError("completion event/result no longer matches the reviewed run")
            # Review already durable. Save ack intent before the side effect;
            # retrying this exact event is idempotent after a crash.
            run["ack_intent_at"] = RUNNER.utc_now()
            save(directory, state)
            RUNNER.acknowledge_completion_event(event)
            run["acknowledged_at"] = RUNNER.utc_now()
            result = run
        elif action in {"native-attach", "native-complete"}:
            if run.get("transport") != "native":
                raise CoordinationError("this is not a native reservation")
            packet = read(args.packet)
            if action == "native-attach":
                handle = packet.get("native_handle")
                if not isinstance(handle, str) or not handle:
                    raise CoordinationError("native-attach requires the observed tool handle")
                if run.get("native_handle") not in (None, handle) or run.get("required_native_handle") not in (None, handle):
                    raise CoordinationError("native handle differs from the reserved original session")
                if run["status"] not in {"reserved", "active"}:
                    raise CoordinationError("recover reservation readiness before attaching")
                run.update(native_handle=handle, status="active", attached_at=RUNNER.utc_now())
            else:
                NATIVE.complete(directory, state, run, packet, RUNNER)
                import_result(run, state)
            result = run
        else:
            raise CoordinationError("unsupported host action")
    save(directory, state)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    route = commands.add_parser("route")
    route.add_argument("--packet", type=Path, required=True)
    recommend = commands.add_parser("recommend")
    recommend.add_argument("--packet", type=Path, required=True)
    init = commands.add_parser("init")
    init.add_argument("--task-spec", type=Path, required=True)
    init.add_argument("--host", choices=("codex", "agy", "claude", "other"), required=True)
    init.add_argument("--time-budget", default="1h")
    init.add_argument("--invocation-allowance", type=int, default=5)
    for action in ("init", "dispatch", "recover", "review", "reverify", "show", "next", "knowledge", "capture", "capture-checks", "verify-evidence", "register", "record-decision", "ack", "native-attach", "native-complete"):
        sub = init if action == "init" else commands.add_parser(action)
        sub.add_argument("--task-dir", type=Path, required=True)
        if action == "dispatch":
            sub.add_argument("--kind", choices=("implementation", "consultation", "challenge", "correction"), required=True)
            sub.add_argument("--request-id", required=True)
            sub.add_argument("--engine", choices=(*RUNNER.EXECUTION_ENGINES, "claude"))
            sub.add_argument("--transport", choices=("executor", "native"), default="executor")
            sub.add_argument("--model")
            sub.add_argument("--effort")
            sub.add_argument("--variant")
            sub.add_argument("--billing-mode", choices=("unknown", "subscription", "api"), default="unknown")
            sub.add_argument("--verify-command", action="append", default=[])
            sub.add_argument("--packet", type=Path)
            sub.add_argument("--evidence", type=Path)
            sub.add_argument("--review-id")
        elif action == "review":
            sub.add_argument("--run-id", required=True)
            sub.add_argument("--decision", choices=("accept", "supported", "reject", "takeover"), required=True)
            sub.add_argument("--evidence", type=Path)
            sub.add_argument("--review-id")
        elif action == "capture":
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument("--url")
            source.add_argument("--path", type=Path)
            source.add_argument("--command")
            sub.add_argument("--source-kind", choices=("documentation", "version", "config", "check", "observation"), required=True)
            sub.add_argument("--component", required=True)
            sub.add_argument("--version", required=True)
            sub.add_argument("--authority", choices=("official", "local", "secondary"), default="local")
            sub.add_argument("--updated-at")
        elif action in {"next", "verify-evidence", "register", "record-decision", "native-attach", "native-complete"}:
            sub.add_argument("--packet", type=Path, required=action != "next")
            if action.startswith("native-"):
                sub.add_argument("--run-id", required=True)
            elif action == "record-decision":
                sub.add_argument("--review-id")
        elif action == "ack":
            sub.add_argument("--run-id", required=True)
            sub.add_argument("--event-id", required=True)
        elif action == "capture-checks":
            sub.add_argument("--run-id", required=True)
            sub.add_argument("--component", required=True)
            sub.add_argument("--version", required=True)
        elif action == "reverify":
            sub.add_argument("--run-id", required=True)
            sub.add_argument("--request-id", required=True)
            sub.add_argument("--reason", required=True)
            sub.add_argument("--verify-command", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.action == "route":
            output = choose_route(read(args.packet))
        elif args.action == "recommend":
            output = POLICY.recommend(read(args.packet), read(Path(__file__).parent.parent / "references/profiles.json"))
        elif args.action == "init":
            output = initialize(args)
        else:
            directory = args.task_dir.expanduser().resolve()
            # Separate from the worktree lease held by the child runner. A task
            # lease prevents duplicate decisions while its dispatch is running.
            with SUPPORT.workspace_lease(directory, RUNNER.default_agent_cache_dir() / "coordinator-locks"):
                state = load(directory)
                if args.action == "dispatch":
                    output = dispatch(args, directory, state)
                elif args.action == "recover":
                    output = recover(directory, state)
                elif args.action == "review":
                    output = review(args, directory, state)
                elif args.action == "reverify":
                    output = reverify(args, directory, state)
                elif args.action == "show":
                    output = state
                else:
                    output = host_action(args, directory, state)
        print(json.dumps(output, indent=2, ensure_ascii=True))
        return 0
    except (ValueError, OSError, RUNNER.RunnerError) as exc:
        print(f"coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
