from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_run_agent as fixtures

RUNNER = fixtures.RUNNER
SUPPORT = RUNNER.bundled_module("execution_support")
USAGE = RUNNER.bundled_module("usage")
COORDINATOR = RUNNER.bundled_module("coordinator")


def sample_usage(input_tokens=1000, cached=400, writes=100, output=200):
    return {"input_tokens": input_tokens, "cached_input_tokens": cached, "cache_write_input_tokens": writes, "output_tokens": output, "reasoning_output_tokens": 80}


def usage_stream(*snapshots):
    return "\n".join(json.dumps({"type": "turn.completed", "usage": value}) for value in snapshots)


class UsageTests(unittest.TestCase):
    def test_captured_agy_envelope_yields_report_usage_and_exact_session(self):
        text = (Path(__file__).parent / "fixtures/agy-1.2.6-stream.jsonl").read_text()
        report, session, status = RUNNER.parse_agy_output(text)
        self.assertEqual((session, status), ("00000000-0000-4000-8000-000000000001", "success"))
        raw, extracted = RUNNER.extract_raw_final_report(report)
        self.assertEqual(RUNNER.parse_reported_outcome(raw, extracted), "blocked")
        usage = USAGE.normalize_usage("agy", text)
        self.assertEqual(usage["cumulative"]["input_tokens"], 1000)
        self.assertEqual(usage["cumulative"]["thinking_tokens"], 60)
        self.assertEqual(usage["status"], "unknown")

    def test_agy_keeps_cumulative_and_step_snapshots_separate_without_guessing_billing(self):
        events = [
            {"type": "init", "model": "gemini-3.8-flash-medium"},
            {"type": "step_update", "conversation_id": "s", "step_index": 1, "usage": {"input_tokens": 20}},
            {"type": "step_update", "conversation_id": "s", "step_index": 1, "usage": {"input_tokens": 278}},
            {"type": "result", "conversation_id": "s", "usage": {"input_tokens": 278, "cache_read_tokens": 30214, "output_tokens": 4, "total_tokens": 282}},
        ]
        text = "\n".join(json.dumps(event) for event in events)
        usage = USAGE.normalize_usage("agy", text)
        self.assertEqual(usage["status"], "unknown")
        self.assertEqual(usage["cumulative"]["input_tokens"], 278)
        self.assertEqual(len(usage["step_snapshots"]), 1)
        self.assertEqual(usage["step_snapshots"][0]["usage"]["input_tokens"], 278)
        self.assertEqual(usage["model_observations"][0]["meaning"], "configured_not_backend_resolved")
        self.assertIn("separate_process_resume_counter_scope_unverified", USAGE.normalize_usage("agy", text, resumed=True, baseline=usage["cumulative"])["gaps"])

    def test_tariff_estimates_expire_and_do_not_guess_long_context_tiers(self):
        registry = json.loads((Path(__file__).parents[1] / "references/model-registry.json").read_text())
        now = dt.datetime(2026, 9, 18, 16, tzinfo=dt.timezone.utc)
        usage = USAGE.normalize_usage("codex", usage_stream(sample_usage()))
        estimate = USAGE.estimate_tariff(usage, "gpt-5.6-luna", registry, billing_mode="subscription", now=now)
        self.assertEqual((estimate["estimated_usd"], estimate["actual_charge_usd"]), (.000373, None))
        week_old = USAGE.estimate_tariff(usage, "gpt-5.6-luna", registry, billing_mode="api", now=now + dt.timedelta(days=7))
        self.assertEqual(week_old["estimated_usd"], .000373)
        expired = USAGE.estimate_tariff(usage, "gpt-5.6-luna", registry, billing_mode="api", now=now + dt.timedelta(days=31))
        self.assertEqual(expired["gaps"], ["tariff_requires_live_refresh"])
        slug = USAGE.estimate_tariff(usage, "gemini-3.8-flash-high", registry, billing_mode="api", now=now)
        self.assertNotIn("model_has_no_verified_tariff", slug["gaps"])
        long = USAGE.normalize_usage("codex", usage_stream(sample_usage(300000)))
        unresolved = USAGE.estimate_tariff(long, "gpt-5.6-luna", registry, billing_mode="api", now=now)
        self.assertEqual((unresolved["estimated_usd"], unresolved["gaps"]), (None, ["per_request_tariff_tier_not_resolved"]))

    def test_codex_uses_last_snapshot_and_counts_reasoning_once(self):
        result = USAGE.normalize_usage("codex", usage_stream(sample_usage(800), sample_usage()))
        self.assertEqual(result["totals"], {"fresh_input": 500, "cache_read": 400, "cache_write": 100, "output": 200})
        self.assertEqual(result["cumulative"]["input_tokens"], 1000)

    def test_resume_requires_a_baseline_and_reports_only_new_usage(self):
        text = usage_stream(sample_usage(1600, 600, 200, 300))
        unknown = USAGE.normalize_usage("codex", text, resumed=True)
        self.assertEqual(unknown["status"], "unknown")
        delta = USAGE.normalize_usage("codex", text, resumed=True, baseline=sample_usage())
        self.assertEqual(delta["totals"], {"fresh_input": 300, "cache_read": 200, "cache_write": 100, "output": 100})

    def test_unknown_writes_impossible_inputs_and_reset_counters_remain_unknown(self):
        raw = sample_usage()
        del raw["cache_write_input_tokens"]
        result = USAGE.normalize_usage("codex", usage_stream(raw))
        self.assertEqual(result["totals"], {"fresh_input": None, "cache_read": 400, "cache_write": None, "output": 200})
        impossible = USAGE.normalize_usage("codex", usage_stream(sample_usage(10)))
        self.assertEqual(impossible["status"], "partial")
        self.assertIsNone(impossible["totals"]["fresh_input"])
        reset = USAGE.normalize_usage("codex", usage_stream(sample_usage(10, 1, 1, 1)), resumed=True, baseline=sample_usage())
        self.assertEqual(reset["status"], "unknown")

    def test_raw_counter_reset_cannot_look_like_positive_fresh_usage(self):
        result = USAGE.normalize_usage("codex", usage_stream(sample_usage(9000, 1000, 0, 300)), resumed=True, baseline=sample_usage(10000, 9000, 500, 200))
        self.assertEqual(result["totals"], {"fresh_input": None, "cache_read": None, "cache_write": None, "output": None})
        self.assertIn("cumulative_counters_reset", result["gaps"])

    def test_opencode_deduplicates_steps_and_adds_separate_reasoning(self):
        event = {"type": "step_finish", "sessionID": "s", "part": {"id": "a", "messageID": "m", "cost": .02, "tokens": {"input": 500, "cache": {"read": 400, "write": 100}, "output": 120, "reasoning": 80}}}
        result = USAGE.normalize_usage("opencode", json.dumps(event) + "\n" + json.dumps(event))
        self.assertEqual(result["totals"], {"fresh_input": 500, "cache_read": 400, "cache_write": 100, "output": 200})
        self.assertEqual(result["native_cost_usd"], .02)
        del event["part"]["id"]
        self.assertEqual(USAGE.normalize_usage("opencode", json.dumps(event))["status"], "unknown")

    def test_missing_attempt_usage_never_becomes_free_work(self):
        known = USAGE.normalize_usage("codex", usage_stream(sample_usage()))
        unknown = USAGE.normalize_usage("agy", "Plain report")
        result = USAGE.combine_attempts([known, unknown])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["attempts"][0]["totals"]["output"], 200)
        self.assertEqual(unknown["totals"], {"fresh_input": None, "cache_read": None, "cache_write": None, "output": None})


class ExecutionExtensionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RunnerIntegrationTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_catalog_effort_binds_to_cli_and_invalid_value_stops_before_execution(self):
        code, result, _, _, capture = self.fixture.run_main(extra_args=["--effort", "low"])
        self.assertEqual((code, result["effort"]["bound"]), (0, "low"))
        self.assertIn('model_reasoning_effort="low"', capture["argv"])
        self.assertIsNone(result["effort"]["observed"])
        code, result, _, _, capture = self.fixture.run_main(extra_args=["--effort", "made-up"])
        self.assertEqual((code, result["status"]), (4, "preflight_failed"))
        self.assertEqual(capture, {})

    def test_tabbed_agy_catalog_uses_exact_id_and_effort_conflicts_fail(self):
        code, result, _, _, _ = self.fixture.run_main(engine="agy", extra_args=["--effort", "medium"])
        self.assertEqual((code, result["model"], result["effort"]["bound"]), (0, "gemini-3.8-flash-medium", "medium"))
        code, result, _, _, capture = self.fixture.run_main(engine="agy", extra_args=["--effort", "high"])
        self.assertEqual((code, result["status"], capture), (4, "preflight_failed", {}))

    def test_agy_terminal_failure_cannot_be_overridden_by_complete_report(self):
        with mock.patch.dict(os.environ, {"FAKE_AGY_STATUS": "error"}):
            code, result, _, _, capture = self.fixture.run_main(engine="agy")
        self.assertEqual((result["status"], result["provider_terminal_status"]), ("failed", "error"))
        self.assertIn("stream-json", capture["argv"])

    def test_resume_preserves_identity_and_derives_a_usage_delta(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_USAGE": json.dumps([sample_usage()])}):
            code, first, directory, _, _ = self.fixture.run_main(no_change=True, expect_changes=False, allow_path=None, extra_args=["--effort", "low"])
        with mock.patch.dict(os.environ, {"FAKE_CODEX_USAGE": json.dumps([sample_usage(1600, 600, 200, 300)])}):
            code, result, _, _, _ = self.fixture.run_main(no_change=True, expect_changes=False, allow_path=None, session=first["session_id"], extra_args=["--effort", "low", "--resume-result", str(directory / "result.json")])
        self.assertEqual(code, 0)
        self.assertEqual(result["usage"]["totals"], {"fresh_input": 300, "cache_read": 200, "cache_write": 100, "output": 100})
        code, result, _, _, capture = self.fixture.run_main(no_change=True, expect_changes=False, allow_path=None, session=first["session_id"], extra_args=["--effort", "high", "--resume-result", str(directory / "result.json")])
        self.assertEqual((code, result["status"], capture), (4, "preflight_failed", {}))

    def test_workspace_lease_survives_parent_release_until_child_exits(self):
        cache = self.fixture.external_path / "locks"
        repository = self.fixture.repository.path
        with SUPPORT.workspace_lease(repository, cache) as descriptor:
            child = subprocess.Popen([sys.executable, "-c", "import sys,time; print('ready', flush=True); time.sleep(10)"], stdout=subprocess.PIPE, text=True, pass_fds=(descriptor,))
            self.assertEqual(child.stdout.readline().strip(), "ready")
        try:
            with self.assertRaisesRegex(SUPPORT.ContractError, "another executor"):
                with SUPPORT.workspace_lease(repository, cache):
                    self.fail("concurrent lease was granted")
        finally:
            child.terminate()
            child.wait()
            child.stdout.close()
        with SUPPORT.workspace_lease(repository, cache):
            code, result, _, _, _ = self.fixture.run_main()
        self.assertEqual((code, result["status"]), (0, "completed"))

    def test_expired_deadline_does_not_launch_verification(self):
        marker = self.fixture.repository.path / "should-not-exist"
        result = RUNNER.run_verification_commands([f"touch '{marker}'"], repo=self.fixture.repository.path, out_dir=self.fixture.external_path, timeout_seconds=10, deadline="2000-01-01T00:00:00+00:00")
        self.assertEqual((result["status"], result["completed_count"], marker.exists()), ("timed_out", 0, False))

    def test_python_verification_subprocess_imports_do_not_write_bytecode(self):
        (self.fixture.repository.path / "helper.py").write_text("VALUE = 1\n")
        code, result, _, _, _ = self.fixture.run_main(extra_args=["--verify-command", 'python3 -c "import helper; assert helper.VALUE == 1"'])
        self.assertEqual((code, result["status"], result["verification"]["status"]), (0, "completed", "passed"))
        self.assertEqual(result["run_delta"]["changed_paths"], ["allowed/output.txt"])


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RunnerIntegrationTests()
        self.fixture.setUp()
        self.directory = self.fixture.external_path / "task"
        self.spec = {
            "objective": "Write the approved output", "repository_root": str(self.fixture.repository.path),
            "current_state": "Output is missing", "pre_existing_changes": [], "plan": ["Write allowed/output.txt"],
            "allow_paths": ["allowed"], "track_paths": [], "development_checks": [],
            "acceptance_criteria": ["Output contains the approved content"],
        }
        self.spec_path = self.fixture.external_path / "spec.json"
        self.spec_path.write_text(json.dumps(self.spec))
        self.packet = self.fixture.external_path / "packet.json"
        self.packet.write_text(json.dumps({"question": "Does cancellation prevent the callback?", "decision_needed": "Choose a race guard", "observations": ["Callback ran after cancellation"], "attempts": ["Cancellation alone failed"], "versions": ["client 2"], "hypotheses": ["Queued callbacks survive cancellation", "Another request emitted the callback"]}))
        self.environment = mock.patch.dict(os.environ, {
            "PATH": f"{self.fixture.external_path}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(self.fixture.external_path / "cache"),
            "FAKE_CAPTURE": str(self.fixture.external_path / "capture.json"),
        })
        self.environment.start()
        self.assertEqual(self.call("init", "--task-spec", str(self.spec_path), "--host", "claude")[0], 0)

    def tearDown(self):
        self.environment.stop()
        self.fixture.tearDown()

    def call(self, action, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = COORDINATOR.main([action, "--task-dir", str(self.directory), *args])
        return code, json.loads(stdout.getvalue()) if code == 0 else stderr.getvalue()

    def start(self, request="implement", kind="implementation", *extra):
        return self.call("dispatch", "--kind", kind, "--request-id", request, "--engine", "codex", "--model", "gpt-5.6-luna", "--effort", "low", *extra)

    def evidence(self):
        artifact = self.fixture.external_path / "evidence.txt"
        artifact.write_text("Observed trace and focused check output\n")
        path = self.fixture.external_path / "evidence.json"
        path.write_text(json.dumps({
            "conclusion": "Use a request identity guard based on the observed callback order",
            "versions": {"client": "2"}, "criteria": self.spec["acceptance_criteria"],
            "sources": [{"location": "local test trace", "component": "client", "version": "2", "retrieved_at": RUNNER.utc_now(), "artifact": str(artifact), "sha256": COORDINATOR.digest(artifact)}],
        }))
        return path

    def reviewed_evidence(self):
        self.evidence()
        version_file = self.fixture.external_path / "version.txt"
        version_file.write_text("client 2\n")
        _, version = self.call("capture", "--path", str(version_file), "--source-kind", "version", "--component", "client", "--version", "2")
        _, source = self.call("capture", "--path", str(self.fixture.external_path / "evidence.txt"), "--source-kind", "observation", "--component", "client", "--version", "2")
        packet = self.fixture.external_path / "review-packet.json"
        packet.write_text(json.dumps({
            "question": "Does the fixture trace support the correction?", "conclusion": "Use the observed trace",
            "owner": "test conductor", "invalidation_trigger": "client version or source changes",
            "versions": {"client": "2"}, "version_sources": {"client": version["source_id"]},
            "claims": [{"kind": "observation", "statement": "The fixture produced a trace", "citations": [{"source_id": source["source_id"], "excerpt": "Observed trace and focused check output"}]}],
            "criteria": [{"criterion": self.spec["acceptance_criteria"][0], "claim_indexes": [0]}], "gaps": [],
        }))
        code, receipt = self.call("verify-evidence", "--packet", str(packet))
        self.assertEqual(code, 0, receipt)
        return receipt["review_id"]

    def test_duplicate_dispatch_and_recovery_do_not_relaunch_work(self):
        code, run = self.start()
        self.assertEqual((code, run["status"], run["runner_status"]), (0, "finished", "completed"))
        marker = self.fixture.external_path / "capture.json"
        original_time = marker.stat().st_mtime_ns
        state = COORDINATOR.load(self.directory)
        state["runs"][0]["status"] = "dispatching"
        COORDINATOR.save(self.directory, state)
        code, recovered = self.call("recover")
        self.assertEqual(recovered["runs"][0]["status"], "finished")
        code, duplicate = self.start()
        self.assertEqual((code, duplicate["run_id"], marker.stat().st_mtime_ns), (0, run["run_id"], original_time))

    def test_crash_without_result_is_uncertain_and_never_retried(self):
        state = COORDINATOR.load(self.directory)
        state["runs"].append({"run_id": "reserved", "request_id": "implement", "kind": "implementation", "status": "dispatching", "result_path": str(self.directory / "missing-result.json")})
        COORDINATOR.save(self.directory, state)
        code, run = self.start()
        self.assertEqual((code, run["status"]), (0, "uncertain"))
        code, error = self.start("another")
        self.assertEqual(code, 2)
        self.assertIn("unresolved dispatch", error)

    def test_blocked_consulted_and_corrected_runs_keep_separate_outcomes(self):
        with mock.patch.dict(os.environ, {"FAKE_REPORT_STATUS": "BLOCKED"}):
            code, blocked = self.start()
        self.assertEqual(blocked["runner_status"], "blocked")
        with mock.patch.dict(os.environ, {"FAKE_NO_CHANGE": "1"}):
            code, consultation = self.start("consult", "consultation", "--packet", str(self.packet))
        self.assertEqual(consultation["runner_status"], "completed")
        proof = self.reviewed_evidence()
        self.assertEqual(self.call("review", "--run-id", consultation["run_id"], "--decision", "supported", "--review-id", proof)[0], 0)
        with mock.patch.dict(os.environ, {"FAKE_CONTENT": "corrected output\n"}):
            code, corrected = self.call("dispatch", "--kind", "correction", "--request-id", "repair", "--review-id", proof)
        self.assertEqual((code, corrected["runner_status"]), (0, "completed"))
        result = COORDINATOR.read(Path(corrected["result_path"]))
        self.assertEqual((result["session_id"], result["effort"]["bound"], result["resumed_from"]), ("codex-thread-1", "low", blocked["result_path"]))
        self.assertEqual(COORDINATOR.read(Path(blocked["result_path"]))["status"], "blocked")
        code, error = self.call("dispatch", "--kind", "correction", "--request-id", "repair-again", "--review-id", proof)
        self.assertEqual(code, 2)
        self.assertIn("one correction", error)

    def test_acceptance_requires_current_untampered_evidence_and_workspace(self):
        _, run = self.start()
        proof = self.reviewed_evidence()
        artifact = self.fixture.external_path / "evidence.txt"
        artifact.write_text("Changed after review")
        code, error = self.call("review", "--run-id", run["run_id"], "--decision", "accept", "--review-id", proof)
        self.assertEqual(code, 2)
        self.assertIn("changed", error)
        proof = self.reviewed_evidence()
        code, decision = self.call("review", "--run-id", run["run_id"], "--decision", "accept", "--review-id", proof)
        self.assertEqual((code, decision["decision"], COORDINATOR.load(self.directory)["status"]), (0, "accept", "accepted"))

    def test_allowance_counts_all_attempts_and_ack_requires_review(self):
        state = COORDINATOR.load(self.directory)
        state["budget"]["limit"] = 1
        COORDINATOR.save(self.directory, state)
        _, run = self.start()
        self.assertEqual(self.call("ack", "--run-id", run["run_id"], "--event-id", run["event_id"])[0], 2)
        self.assertEqual(self.call("review", "--run-id", run["run_id"], "--decision", "reject")[0], 0)
        self.assertEqual(self.call("ack", "--run-id", run["run_id"], "--event-id", "wrong")[0], 2)
        self.assertEqual(self.call("ack", "--run-id", run["run_id"], "--event-id", run["event_id"])[0], 0)
        code, error = self.start("consult", "consultation", "--packet", str(self.packet))
        self.assertEqual(code, 2)
        self.assertIn("allowance exhausted", error)
        self.assertEqual(COORDINATOR.load(self.directory)["budget"]["used"], 1)

    def test_live_process_recovery_does_not_mark_active_work_stopped(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            state = COORDINATOR.load(self.directory)
            state["runs"].append({"run_id": "running", "request_id": "running", "kind": "implementation", "status": "dispatching", "result_path": str(self.directory / "missing-result.json"), "pid": process.pid, "process_identity": COORDINATOR.NATIVE.process_identity(process.pid)})
            COORDINATOR.save(self.directory, state)
            code, state = self.call("recover")
            self.assertEqual((code, state["runs"][0]["status"]), (0, "active"))
            self.assertEqual(self.call("next")[1]["action"], "wait")
            state["deadline"] = "2000-01-01T00:00:00+00:00"
            COORDINATOR.save(self.directory, state)
            self.assertEqual(self.call("next")[1]["action"], "interrupt")
        finally:
            process.terminate()
            process.wait()

    def test_reverification_preserves_failed_result_and_does_not_spend_model_allowance(self):
        code, failed = self.start("work", "implementation", "--verify-command", "exit 1")
        self.assertEqual((code, failed["runner_status"]), (0, "verification_failed"))
        original = Path(failed["result_path"]).read_bytes()
        code, checked = self.call("reverify", "--run-id", failed["run_id"], "--request-id", "check-1", "--reason", "The supplied gate exited unconditionally; check the requested artifact", "--verify-command", "test -f allowed/output.txt")
        self.assertEqual((code, checked["runner_status"]), (0, "completed"))
        self.assertEqual(Path(failed["result_path"]).read_bytes(), original)
        self.assertEqual(COORDINATOR.load(self.directory)["budget"]["used"], 1)
        result = COORDINATOR.read(Path(checked["result_path"]))
        self.assertEqual(result["usage"]["totals"], {"fresh_input": 0, "cache_read": 0, "cache_write": 0, "output": 0})
        self.assertFalse(result["model_invoked"])
        code, duplicate = self.call("reverify", "--run-id", failed["run_id"], "--request-id", "check-1", "--reason", "same check")
        self.assertEqual((code, duplicate["run_id"]), (0, checked["run_id"]))

    def test_reverification_cannot_hide_a_mutating_check(self):
        _, work = self.start()
        code, checked = self.call("reverify", "--run-id", work["run_id"], "--request-id", "bad-check", "--reason", "Exercise mutation rejection", "--verify-command", "printf changed > allowed/output.txt")
        self.assertEqual((code, checked["runner_status"]), (0, "verification_mutation"))

    def test_native_host_handoff_holds_lease_and_audits_before_review(self):
        code, run = self.call("dispatch", "--kind", "implementation", "--request-id", "native-1", "--transport", "native", "--engine", "claude", "--model", "claude-sonnet-5", "--effort", "high", "--verify-command", "test -f allowed/output.txt")
        self.assertEqual(code, 0, run)
        packet = self.fixture.external_path / "native.json"
        try:
            with self.assertRaisesRegex(SUPPORT.ContractError, "another executor"):
                with SUPPORT.workspace_lease(self.fixture.repository.path, RUNNER.default_agent_cache_dir()):
                    self.fail("lease should belong to the native guard")
            packet.write_text(json.dumps({"native_handle": "native-test-session"}))
            self.assertEqual(self.call("native-attach", "--run-id", run["run_id"], "--packet", str(packet))[0], 0)
            (self.fixture.repository.path / "allowed").mkdir(exist_ok=True)
            (self.fixture.repository.path / "allowed/output.txt").write_text("native output\n")
            report = self.fixture.external_path / "native-report.txt"
            report.write_text("### STATUS\nCOMPLETE\n### FILES CHANGED\nallowed/output.txt\n### COMMANDS RUN\nnone\n### VERIFICATION\nHost will check output.\n### RISKS OR BLOCKERS\nnone\n")
            packet.write_text(json.dumps({"native_handle": "native-test-session", "event_id": "native-completion-1", "terminal": True, "tool_status": "completed", "report_path": str(report)}))
            code, finished = self.call("native-complete", "--run-id", run["run_id"], "--packet", str(packet))
            self.assertEqual(code, 0, finished)
            self.assertEqual((code, finished["runner_status"]), (0, "completed"), finished)
            result = COORDINATOR.read(Path(finished["result_path"]))
            self.assertEqual(result["verification"]["status"], "passed")
            self.assertIsNone(result["resolved_model"])
            self.assertIsNone(result["effort"]["observed"])
            self.assertEqual(self.call("dispatch", "--kind", "implementation", "--request-id", "native-1", "--transport", "native")[1]["run_id"], run["run_id"])
        finally:
            if COORDINATOR.NATIVE.process_state(run) == "active":
                os.kill(run["pid"], 15)

    def test_claim_cannot_verify_an_excerpt_absent_from_the_source(self):
        self.reviewed_evidence()
        path = self.fixture.external_path / "review-packet.json"
        packet = COORDINATOR.read(path)
        packet["claims"][0]["citations"][0]["excerpt"] = "invented vendor assertion"
        path.write_text(json.dumps(packet))
        code, error = self.call("verify-evidence", "--packet", str(path))
        self.assertEqual(code, 2)
        self.assertIn("absent", error)

    def test_raw_audit_capture_does_not_invalidate_check_projection_of_same_result(self):
        _, run = self.start("check", "implementation", "--verify-command", "printf checked")
        code, check = self.call("capture-checks", "--run-id", run["run_id"], "--component", "client", "--version", "2")
        self.assertEqual(code, 0, check)
        code, audit = self.call("capture", "--path", run["result_path"], "--source-kind", "observation", "--component", "client", "--version", "2")
        self.assertEqual(code, 0, audit)
        state = COORDINATOR.load(self.directory)
        source = state["evidence"]["sources"][check["source_id"]]
        self.assertIn("checked", COORDINATOR.EVIDENCE.validate_source(source, state, RUNNER))
        self.assertIsNone(source["invalidated"])

    def test_consultation_budget_deadline_and_native_boundary_are_enforced(self):
        with mock.patch.dict(os.environ, {"FAKE_NO_CHANGE": "1"}):
            for index in range(3):
                code, run = self.start("consult" + str(index), "consultation", "--packet", str(self.packet))
                self.assertEqual((code, run["runner_status"]), (0, "completed"))
        code, error = self.start("fourth", "consultation", "--packet", str(self.packet))
        self.assertEqual(code, 2)
        self.assertIn("budget exhausted", error)
        state = COORDINATOR.load(self.directory)
        state["deadline"] = "2000-01-01T00:00:00+00:00"
        COORDINATOR.save(self.directory, state)
        code, error = self.start()
        self.assertEqual(code, 2)
        self.assertIn("deadline exhausted", error)
        route = COORDINATOR.choose_route({"host": "codex", "engine": "codex", "model": "gpt-6-astra", "context_portable": True, "independent_acceptance": True, "handoff_worthwhile": True})
        self.assertEqual(route, {"action": "delegate", "owner": "fresh_worker", "transport": "native"})

    def test_stale_or_wrong_version_sources_are_not_verified(self):
        proof = self.evidence()
        value = COORDINATOR.read(proof)
        value["sources"][0]["version"] = "3"
        proof.write_text(json.dumps(value))
        with self.assertRaisesRegex(COORDINATOR.CoordinationError, "observed version"):
            COORDINATOR.check_evidence(proof)
        value["sources"][0]["version"] = "2"
        value["sources"][0]["retrieved_at"] = "2000-01-01T00:00:00+00:00"
        proof.write_text(json.dumps(value))
        with self.assertRaisesRegex(COORDINATOR.CoordinationError, "stale"):
            COORDINATOR.check_evidence(proof)

    def test_consultation_request_is_advice_only_and_rejects_approval_fields(self):
        packet = self.packet.read_text()
        report = "### RISKS OR BLOCKERS\n```consultation-request\n" + packet + "\n```\n"
        self.assertEqual(SUPPORT.consultation_from_report(report, "blocked")["status"], "requested")
        self.assertEqual(SUPPORT.consultation_from_report(report, "complete")["status"], "invalid")
        value = json.loads(packet)
        value["verified"] = True
        with self.assertRaisesRegex(SUPPORT.ContractError, "unknown consultation fields"):
            SUPPORT.validate_consultation(value)

    def test_long_consultation_is_validated_before_display_truncation(self):
        packet = COORDINATOR.read(self.packet)
        packet["observations"] = ["trace " * 1200]
        report = "### STATUS\nBLOCKED\n### FILES CHANGED\nnone\n### COMMANDS RUN\nnone\n### VERIFICATION\nnone\n### RISKS OR BLOCKERS\n```consultation-request\n" + json.dumps(packet) + "\n```\n"
        raw, extracted = RUNNER.extract_raw_final_report(report)
        self.assertEqual(RUNNER.report_contract_violations(raw, extracted), [])
        self.assertEqual(SUPPORT.consultation_from_report(raw, "blocked")["request"]["question"], "Does cancellation prevent the callback?")
        self.assertLessEqual(len(RUNNER.bound_final_message(raw)), 4000)


if __name__ == "__main__":
    unittest.main()
