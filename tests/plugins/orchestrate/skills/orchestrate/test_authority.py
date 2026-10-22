"""Lead authority, fencing and verified handoffs, driven through the coordinator with fake executors."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from unittest import mock

import test_run_agent as fixtures

RUNNER = fixtures.RUNNER
COORDINATOR = RUNNER.bundled_module("coordinator")
AUTHORITY = COORDINATOR.AUTHORITY
SUPPORT = COORDINATOR.SUPPORT
MAILBOX = COORDINATOR.MAILBOX

LEAD, DEPUTY = "claude-lead-1", "codex-deputy-1"


class AuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.RunnerIntegrationTests()
        self.fixture.setUp()
        self.repository = self.fixture.repository.path
        self.directory = self.fixture.external_path / "task"
        spec = {
            "objective": "Write the approved output",
            "repository_root": str(self.repository),
            "current_state": "Output is missing",
            "pre_existing_changes": [],
            "plan": ["Write allowed/output.txt"],
            "allow_paths": ["allowed"],
            "track_paths": [],
            "development_checks": [],
            "acceptance_criteria": ["Output contains the approved content"],
        }
        self.spec_path = self.fixture.external_path / "spec.json"
        self.spec_path.write_text(json.dumps(spec))
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{self.fixture.external_path}{os.pathsep}{os.environ['PATH']}",
                "XDG_CACHE_HOME": str(self.fixture.external_path / "cache"),
                "FAKE_CAPTURE": str(self.fixture.external_path / "capture.json"),
            },
        )
        self.environment.start()
        self.authority("appoint", "--lead-session", LEAD, "--lead-harness", "claude", "--deputy-harness", "codex")
        self.assertEqual(
            self.task("init", "--task-spec", str(self.spec_path), "--authority", "team", *self.as_lead(1))[0], 0
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.fixture.tearDown()

    def authority(self, action: str, *args: str, ok: bool = True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = AUTHORITY.main([action, "--team", "team", *args])
        if ok:
            self.assertEqual(code, 0, stderr.getvalue())
            return json.loads(stdout.getvalue())
        self.assertNotEqual(code, 0)
        return stderr.getvalue()

    def task(self, action: str, *args: str):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = COORDINATOR.main([action, "--task-dir", str(self.directory), *args])
        return code, json.loads(stdout.getvalue()) if code == 0 else stderr.getvalue()

    @staticmethod
    def as_lead(generation: int, session: str = LEAD) -> list[str]:
        return ["--lead-session", session, "--lead-generation", str(generation)]

    def investigate(self, request: str, *lead: str, engine: str = "codex"):
        model = {"codex": ["--model", "gpt-5.6-luna", "--effort", "low"], "claude": ["--model", "claude-sonnet-5"]}
        return self.task(
            "dispatch", "--kind", "investigation", "--request-id", request, "--engine", engine, *model[engine], *lead
        )

    def digest(self, harness: str = "codex") -> str:
        return self.authority("show", "--harness", harness)["state"]["digest"]

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *args], capture_output=True, text=True, check=True
        ).stdout

    def take_over(self, expect: int = 1) -> dict:
        return self.authority(
            "takeover", "--session", DEPUTY, "--harness", "codex", "--expect-generation", str(expect), "--user-approved"
        )

    def test_a_team_task_refuses_mutations_without_the_current_generation(self) -> None:
        code, error = self.investigate("unfenced")
        self.assertEqual(code, 2)
        self.assertIn("pass --lead-session and --lead-generation", error)
        code, error = self.investigate("wrong", *self.as_lead(1, "someone-else"))
        self.assertIn("was superseded", error)
        self.assertEqual(self.investigate("ok", *self.as_lead(1))[0], 0)
        # Reading never needs authority.
        self.assertEqual(self.task("show")[0], 0)
        # Credits need the user's say-so in the appointment, not just a flag on one dispatch.
        self.assertIn(
            "did not allow this team to spend credits", self.investigate("paid", *self.as_lead(1), "--allow-credits")[1]
        )

    def test_a_planned_handover_keeps_the_lead_in_charge_until_the_successor_accepts(self) -> None:
        self.authority(
            "offer", "--session", LEAD, "--expect-generation", "1", "--to-session", DEPUTY, "--to-harness", "codex"
        )
        # The offer does not fence the lead: it keeps working while the successor is away.
        self.assertEqual(self.investigate("still-lead", *self.as_lead(1))[0], 0)
        self.assertEqual(self.task("recover", *self.as_lead(1))[0], 0)
        record = self.authority(
            "accept", "--session", DEPUTY, "--harness", "codex", "--expect-generation", "1", "--digest", self.digest()
        )
        self.assertEqual((record["generation"], record["status"], record["lead"]["session"]), (2, "active", DEPUTY))
        self.assertEqual(record["handoff"]["reason"], "handover")
        self.assertIn("superseded", self.task("recover", *self.as_lead(1))[1])
        self.assertEqual(self.task("recover", *self.as_lead(2, DEPUTY))[0], 0)

    def test_a_takeover_fences_at_once_and_a_pending_successor_may_only_recover_or_abandon(self) -> None:
        refused = self.authority(
            "takeover", "--session", DEPUTY, "--harness", "codex", "--expect-generation", "1", ok=False
        )
        self.assertIn("needs the user's approval", refused)
        wrong = self.authority(
            "takeover", "--session", "x", "--harness", "agy", "--expect-generation", "1", "--user-approved", ok=False
        )
        self.assertIn("deputy runs on codex", wrong)
        self.assertEqual(self.take_over()["status"], "pending")
        self.assertIn("superseded", self.investigate("old-lead", *self.as_lead(1))[1])
        self.assertIn("is pending", self.investigate("too-early", *self.as_lead(2, DEPUTY))[1])
        self.assertEqual(self.task("recover", *self.as_lead(2, DEPUTY))[0], 0)
        # A returning predecessor racing the deputy on the same generation loses: exactly one change wins.
        raced = self.authority(
            "takeover",
            "--session",
            DEPUTY,
            "--harness",
            "codex",
            "--expect-generation",
            "1",
            "--user-approved",
            ok=False,
        )
        self.assertIn("generation is 2", raced)
        self.authority(
            "accept", "--session", DEPUTY, "--harness", "codex", "--expect-generation", "2", "--digest", self.digest()
        )
        # A Codex lead sends a Claude worker through the executor.
        self.assertEqual(self.investigate("new-lead", *self.as_lead(2, DEPUTY), engine="claude")[0], 0)

    def test_the_user_can_always_re_appoint_even_from_a_stuck_pending_generation(self) -> None:
        self.take_over()
        stale = self.authority("appoint", "--lead-session", LEAD, "--lead-harness", "claude", ok=False)
        self.assertIn("generation is 2", stale)
        record = self.authority(
            "appoint", "--lead-session", LEAD, "--lead-harness", "claude", "--expect-generation", "2"
        )
        self.assertEqual((record["generation"], record["status"]), (3, "active"))
        self.assertEqual(self.investigate("back", *self.as_lead(3))[0], 0)

    def test_accept_binds_to_the_state_shown_and_refuses_rewritten_history(self) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", "the commit the handoff records")
        self.take_over()
        shown = self.digest()
        (self.repository / "allowed").mkdir(exist_ok=True)
        (self.repository / "allowed" / "late.txt").write_text("written after the successor looked\n")
        changed = self.authority(
            "accept", "--session", DEPUTY, "--harness", "codex", "--expect-generation", "2", "--digest", shown, ok=False
        )
        self.assertIn("changed since it was shown", changed)
        (self.repository / "allowed" / "late.txt").unlink()
        handoff_head = self.authority("show")["authority"]["handoff"]["heads"][str(self.repository.resolve())]
        self.git("checkout", "-q", "--detach", handoff_head + "~1")
        self.git("commit", "-q", "--allow-empty", "-m", "replaced history")
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(self.repository), "merge-base", "--is-ancestor", handoff_head, "HEAD"], check=False
            ).returncode,
            0,
        )
        rewritten = self.authority(
            "accept",
            "--session",
            DEPUTY,
            "--harness",
            "codex",
            "--expect-generation",
            "2",
            "--digest",
            self.digest(),
            ok=False,
        )
        self.assertIn("history was rewritten", rewritten)

    def test_accept_succeeds_while_an_inherited_writer_is_still_editing_its_paths(self) -> None:
        state = COORDINATOR.load(self.directory)
        state["runs"].append(
            {"run_id": "inherited", "request_id": "inherited", "kind": "implementation", "status": "active"}
        )
        COORDINATOR.save(self.directory, state)
        self.take_over()
        (self.repository / "allowed").mkdir(exist_ok=True)
        (self.repository / "allowed" / "one.txt").write_text("the inherited run is writing\n")
        shown = self.digest()
        (self.repository / "allowed" / "two.txt").write_text("and is still writing\n")
        record = self.authority(
            "accept", "--session", DEPUTY, "--harness", "codex", "--expect-generation", "2", "--digest", shown
        )
        self.assertEqual(record["status"], "active")

    def test_a_cross_harness_successor_dispatches_with_its_own_host(self) -> None:
        self.assertEqual(self.investigate("as-claude", *self.as_lead(1))[0], 0)
        self.take_over()
        self.authority(
            "accept", "--session", DEPUTY, "--harness", "codex", "--expect-generation", "2", "--digest", self.digest()
        )
        # Codex is native to the new lead: it must use its own tools, not an executor run.
        self.assertIn("native to the host", self.investigate("as-codex", *self.as_lead(2, DEPUTY))[1])

    def test_abandon_ends_an_orphaned_native_guard_and_frees_the_checkout(self) -> None:
        code, run = self.task(
            "dispatch",
            "--kind",
            "implementation",
            "--request-id",
            "native-1",
            "--transport",
            "native",
            "--engine",
            "claude",
            "--model",
            "claude-sonnet-5",
            "--effort",
            "high",
            *self.as_lead(1),
        )
        self.assertEqual(code, 0, run)
        handle = self.fixture.external_path / "handle.json"
        handle.write_text(json.dumps({"native_handle": "native-worker-1"}))
        self.assertEqual(
            self.task("native-attach", "--run-id", run["run_id"], "--packet", str(handle), *self.as_lead(1))[0], 0
        )
        try:
            with self.assertRaisesRegex(SUPPORT.ContractError, "another executor"):
                with SUPPORT.workspace_lease(self.repository, RUNNER.default_agent_cache_dir()):
                    pass
            self.take_over()
            [orphan] = self.authority("show", "--harness", "codex")["state"]["runs"]
            self.assertEqual((orphan["guard"], orphan["worker"]), ("active", "unknown"))
            self.assertIn("correction", orphan)
            code, abandoned = self.task(
                "abandon", "--run-id", run["run_id"], "--reason", "lead ran out of quota", *self.as_lead(2, DEPUTY)
            )
            self.assertEqual(code, 0, abandoned)
            self.assertEqual((abandoned["status"], abandoned["runner_status"]), ("finished", "abandoned"))
            with SUPPORT.workspace_lease(self.repository, RUNNER.default_agent_cache_dir()):
                pass
            # The predecessor's genuine late result, for the attached handle, is not a second terminal packet.
            packet = self.fixture.external_path / "late.json"
            packet.write_text(json.dumps({"native_handle": "native-worker-1", "event_id": "late", "terminal": True}))
            code, late = self.task("native-complete", "--run-id", run["run_id"], "--packet", str(packet))
            self.assertNotEqual(code, 0)
            self.assertIn("differs from the persisted result", late)
            self.authority(
                "accept",
                "--session",
                DEPUTY,
                "--harness",
                "codex",
                "--expect-generation",
                "2",
                "--digest",
                self.digest(),
            )
            accepted = self.task("review", "--run-id", run["run_id"], "--decision", "accept", *self.as_lead(2, DEPUTY))
            self.assertIn("abandoned run is never accepted", accepted[1])
            code, decision = self.task(
                "review", "--run-id", run["run_id"], "--decision", "takeover", *self.as_lead(2, DEPUTY)
            )
            self.assertEqual((code, decision["generation"]), (0, 2), decision)
        finally:
            if COORDINATOR.NATIVE.process_state(run) == "active":
                os.kill(run["pid"], 15)

    def test_abandon_interrupts_an_inherited_executor_run_and_records_its_audit(self) -> None:
        started, release = self.fixture.external_path / "started", self.fixture.external_path / "release"
        with mock.patch.dict(
            os.environ, {"FAKE_BLOCK": "1", "FAKE_STARTED": str(started), "FAKE_RELEASE": str(release)}
        ):
            code, run = self.investigate("blocked", *self.as_lead(1), "--background")
            self.assertEqual(code, 0, run)
            try:
                deadline = time.monotonic() + 30
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists())
                self.take_over()
                code, abandoned = self.task(
                    "abandon", "--run-id", run["run_id"], "--reason", "lead left", *self.as_lead(2, DEPUTY)
                )
                self.assertEqual(code, 0, abandoned)
                self.assertEqual(abandoned["abandoned"]["signals"][0], "SIGINT")
                self.assertEqual(abandoned["status"], "cancelled", abandoned)
                self.assertIn("without an audit", abandoned["recovery"])
                # Its executor is stopped too, so the checkout's write lease is free again.
                with SUPPORT.workspace_lease(self.repository, RUNNER.default_agent_cache_dir()):
                    pass
            finally:
                release.write_text("go")

    def test_a_worker_question_after_a_takeover_reaches_the_successor(self) -> None:
        opened = MAILBOX.open_run(
            MAILBOX.root(),
            "authority-run",
            worker_harness="codex",
            repository=self.repository,
            result_path=self.fixture.external_path / "result.json",
            deadline="2099-01-01T00:00:00+00:00",
            worker_pid=None,
        )
        state = COORDINATOR.load(self.directory)
        state["runs"].append(
            {"run_id": "live", "request_id": "live", "kind": "investigation", "status": "active", "mailbox": opened}
        )
        COORDINATOR.save(self.directory, state)
        self.take_over()
        self.authority(
            "accept", "--session", DEPUTY, "--harness", "codex", "--expect-generation", "2", "--digest", self.digest()
        )
        [live] = self.authority("show", "--harness", "codex")["state"]["runs"]
        self.assertEqual(live["conductor_mailbox"], opened["conductor"])
        worker = MAILBOX.load(MAILBOX.peer_path(MAILBOX.root(), opened["worker"]))
        MAILBOX.send(
            MAILBOX.root(), sender=worker, to=opened["conductor"], text="Which version?", kind="question", client_id="q"
        )
        code, action = self.task("next")
        self.assertEqual((code, action["action"], action["messages"][0]["text"]), (0, "respond", "Which version?"))

    def test_the_fence_refuses_an_unregistered_task_or_another_store(self) -> None:
        record_path = AUTHORITY.team_dir("team") / "authority.json"
        record = json.loads(record_path.read_text())
        record["tasks"] = []
        record_path.write_text(json.dumps(record))
        self.assertIn("not registered with its team", self.task("recover", *self.as_lead(1))[1])
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.fixture.external_path / "elsewhere")}):
            self.assertIn("this process resolves", self.task("recover", *self.as_lead(1))[1])
        # The same store reached through a symlink is the same store.
        alias = self.fixture.external_path / "cache-link"
        alias.symlink_to(self.fixture.external_path / "cache")
        record["tasks"] = [str(self.directory.resolve())]
        record_path.write_text(json.dumps(record))
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(alias)}):
            self.assertEqual(self.task("recover", *self.as_lead(1))[0], 0)

    def test_an_authority_change_waits_briefly_then_says_why(self) -> None:
        with (
            AUTHORITY.locked(AUTHORITY.team_dir("team"), exclusive=False),
            mock.patch.object(AUTHORITY, "LOCK_WAIT_SECONDS", 0.2),
        ):
            with self.assertRaisesRegex(AUTHORITY.AuthorityError, "a coordinator action holds the team lock"):
                AUTHORITY.locked(AUTHORITY.team_dir("team"), exclusive=True, wait_seconds=0.2).__enter__()

    def test_git_status_parsing_and_unreadable_checkouts(self) -> None:
        self.assertEqual(
            AUTHORITY.porcelain_paths("R  allowed/new.txt\0allowed/old.txt\0?? allowed/x.txt\0 M a b.txt\0"),
            ["a b.txt", "allowed/new.txt", "allowed/old.txt", "allowed/x.txt"],
        )
        self.take_over()
        with mock.patch.object(AUTHORITY, "git", return_value=None):
            state = AUTHORITY.show("team", "codex")["state"]
            [checkout] = state["checkouts"].values()
            self.assertEqual(checkout["error"], "git failed")
            refused = self.authority(
                "accept",
                "--session",
                DEPUTY,
                "--harness",
                "codex",
                "--expect-generation",
                "2",
                "--digest",
                state["digest"],
                ok=False,
            )
        self.assertIn("cannot read the checkout state", refused)


if __name__ == "__main__":
    unittest.main()
