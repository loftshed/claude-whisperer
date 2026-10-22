"""Succession: the deputy proposer, standing takeovers and the lead/deputy hook notices, with a fake monitor."""

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
SUCCESSION = RUNNER.bundled_module("succession")
AUTHORITY = SUCCESSION.module("authority")
MAILBOX = SUCCESSION.module("peer_mailbox")
SCRIPTS = Path(MAILBOX.__file__).parent

LEAD, DEPUTY = "claude-lead-session", "codex-deputy-session"


def lane(account, pool, status="available", usable=80, surplus=10, sub=None, stale=False, until=None):
    return {
        "accountId": account,
        "poolId": pool,
        "label": f"{account} · {pool}",
        "status": status,
        "availableNowPct": usable,
        "surplusPts": surplus,
        "subPoolOf": sub,
        "stale": stale,
        "blockedUntil": until,
    }


class SuccessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.claude_home = self.root / "claude-personal"
        self.claude_home.mkdir()
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.git_dir = str((self.repo / ".git").resolve())
        self.config = {
            "maxAgeSeconds": 180,
            "accounts": [
                {
                    "id": "claude-personal",
                    "provider": "claude",
                    "billing": "personal",
                    "configDir": str(self.claude_home),
                },
                {"id": "claude-work", "provider": "claude", "billing": "work", "configDir": str(self.root / "w")},
                {"id": "codex", "provider": "codex", "billing": "personal"},
                {"id": "antigravity", "provider": "antigravity", "billing": None},
            ],
        }
        self.lanes = [
            lane("claude-personal", "all"),
            lane("claude-personal", "fable", sub="all"),
            lane("claude-work", "all", usable=95, surplus=40),
            lane("codex", "codex", usable=70, surplus=5),
            lane("antigravity", "gemini", usable=90, surplus=20),
            lane("antigravity", "claude-gpt"),
        ]
        cache = self.root / "monitor-cache"
        cache.mkdir()
        (cache / "snapshot.json").write_text("{}")
        monitor = self.root / "monitor"
        monitor.write_text(f"#!/bin/sh\ncat {self.root / 'view.json'}\n")
        monitor.chmod(0o755)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "AGENT_EXECUTOR_HOME": str(self.root / "executor"),
                "MONITOR_CONFIG": str(self.root / "monitor-config.json"),
                "MONITOR_CACHE_DIR": str(cache),
                "MONITOR_BIN": str(monitor),
            },
        )
        self.environment.start()
        self.write()
        self.lead = MAILBOX.register(
            MAILBOX.root(),
            session=LEAD,
            harness="claude",
            cwd=str(self.repo),
            pid=os.getpid(),
            model="claude-opus-5-5",
            account=str(self.claude_home),
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def write(self) -> None:
        accounts = [
            {"id": a["id"], "provider": a["provider"], "billing": a.get("billing"), "label": a["id"]}
            for a in self.config["accounts"]
        ]
        view = {"schema": "monitor.snapshot.v1", "accounts": accounts, "lanes": self.lanes}
        (self.root / "view.json").write_text(json.dumps(view))
        (self.root / "monitor-config.json").write_text(json.dumps(self.config))

    def set_lane(self, account, pool, **changes) -> None:
        if "usable" in changes:
            changes["availableNowPct"] = changes.pop("usable")
        for index, entry in enumerate(self.lanes):
            if (entry["accountId"], entry["poolId"]) == (account, pool):
                self.lanes[index] = {**entry, **changes}
        self.write()

    def propose(self, routes=None):
        return SUCCESSION.propose(
            {"session": LEAD, "harness": "claude", "model": "claude-opus-5-5", "accountHome": str(self.claude_home)},
            view=SUCCESSION.fresh_view(),
            config=SUCCESSION.monitor_config(),
            routes=routes or {},
            peers=MAILBOX.peers(MAILBOX.root()),
        )

    def authority(self, *args, ok=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = AUTHORITY.main(list(args))
        if ok:
            self.assertEqual(code, 0, stderr.getvalue())
            return json.loads(stdout.getvalue())
        self.assertNotEqual(code, 0)
        return stderr.getvalue()

    def appoint(self, standing="blocked,credits:600"):
        return self.authority(
            "appoint",
            "--team",
            "team",
            "--lead-session",
            LEAD,
            "--lead-harness",
            "claude",
            "--lead-pool",
            "claude-personal:all",
            "--deputy-harness",
            "codex",
            "--deputy-pool",
            "codex:codex",
            "--standing-takeover",
            standing,
            "--repository",
            self.git_dir,
        )

    def deputy_session(self, session=DEPUTY, cwd=None):
        return MAILBOX.register(
            MAILBOX.root(), session=session, harness="codex", cwd=str(cwd or self.repo), pid=os.getpid(), account="/x"
        )

    def quiet_lead(self, seconds=3600):
        old = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")
        MAILBOX.annotate(MAILBOX.root(), LEAD, lastSeenAt=old.replace("+00:00", "Z"))

    def standing_takeover(self, ok=False):
        return self.authority(
            "takeover",
            "--team",
            "team",
            "--session",
            DEPUTY,
            "--harness",
            "codex",
            "--expect-generation",
            "1",
            "--standing",
            ok=ok,
        )

    # The proposer

    def test_the_deputy_draws_on_another_account_in_the_same_context(self) -> None:
        proposal = self.propose()
        self.assertEqual(proposal["refusals"], [])
        self.assertEqual((proposal["lead"]["pool"], proposal["context"]), ("claude-personal:all", "personal"))
        # claude-work has more room but is work; Fable is a sub-pool of the lead's own account.
        self.assertEqual(proposal["deputy"]["pool"], "codex:codex")
        self.assertEqual(proposal["deputy"]["harness"], "codex")
        self.assertEqual(proposal["untagged_accounts"], ["antigravity"])
        self.assertIn("start one", proposal["deputy"]["note"])

    def test_unknown_facts_refuse_with_a_way_to_fix_them(self) -> None:
        self.config["accounts"][0]["billing"] = None
        self.write()
        self.assertIn(
            "set billing (work or personal) on monitor account 'claude-personal'", self.propose()["refusals"][0]
        )
        self.config["accounts"][0]["billing"] = "personal"
        self.set_lane("codex", "codex", stale=True)
        refusal = self.propose()["refusals"][0]
        self.assertIn("no pool on another personal account has room now", refusal)
        self.assertIn("antigravity", refusal)
        with mock.patch.dict(os.environ, {"MONITOR_BIN": "/nonexistent"}):
            self.assertIn("quota is unknown", self.propose()["refusals"][0])

    def test_workers_spare_the_lead_and_deputy_pools_while_another_fits(self) -> None:
        self.config["accounts"][3]["billing"] = "personal"
        self.write()
        routes = {
            "implementation": {
                "engine": "codex",
                "model": "gpt-6-luna",
                "alternates": [{"engine": "agy", "model": "gemini-3.8-flash-high"}],
            },
            "review": {"engine": "codex", "model": "gpt-6-luna"},
        }
        proposal = self.propose(routes)
        self.assertEqual(proposal["deputy"]["pool"], "antigravity:gemini")
        self.assertEqual([p["pool"] for p in proposal["workers"]["implementation"]["pools"]], ["codex:codex"])
        # With Antigravity out, Codex is the only independent pool left, so review can only share it.
        self.set_lane("antigravity", "gemini", status="blocked", usable=0)
        self.set_lane("antigravity", "claude-gpt", status="blocked", usable=0)
        proposal = self.propose(routes)
        self.assertEqual(proposal["deputy"]["pool"], "codex:codex")
        self.assertIn("only the lead's or deputy's pool", proposal["workers"]["review"]["note"])

    def test_set_billing_records_the_users_answer(self) -> None:
        self.authority("set-billing", "--account", "antigravity", "--billing", "personal")
        saved = json.loads((self.root / "monitor-config.json").read_text())
        self.assertEqual(saved["accounts"][3]["billing"], "personal")

    # Appointment and standing takeover

    def test_appointment_records_contexts_and_refuses_bad_deputies(self) -> None:
        record = self.appoint()
        self.assertEqual(record["contexts"], {"claude-personal": "personal", "codex": "personal"})
        self.assertEqual(record["standing"], {"statuses": ["blocked", "credits"], "seconds": 600})
        same = self.authority(
            "appoint",
            "--team",
            "other",
            "--lead-session",
            LEAD,
            "--lead-harness",
            "claude",
            "--lead-pool",
            "claude-personal:all",
            "--deputy-harness",
            "claude",
            "--deputy-pool",
            "claude-personal:fable",
            ok=False,
        )
        self.assertIn("another account", same)
        work = self.authority(
            "appoint",
            "--team",
            "other",
            "--lead-session",
            LEAD,
            "--lead-harness",
            "claude",
            "--lead-pool",
            "claude-personal:all",
            "--deputy-harness",
            "claude",
            "--deputy-pool",
            "claude-work:all",
            ok=False,
        )
        self.assertIn("another context", work)

    def test_standing_takeover_needs_an_exhausted_pool_and_a_gone_lead(self) -> None:
        self.appoint()
        self.deputy_session()
        self.quiet_lead()
        self.assertIn("the lead's pool is available", self.standing_takeover())
        self.set_lane("claude-personal", "all", status="credits", usable=0)
        # On credits a lead can still act: its live process blocks a takeover without the user.
        self.assertIn("process is still running", self.standing_takeover())
        self.set_lane("claude-personal", "all", status="blocked", usable=0)
        record = self.standing_takeover(ok=True)
        self.assertEqual(
            (record["status"], record["lead"]["session"], record["lead"]["pool"]), ("pending", DEPUTY, "codex:codex")
        )
        self.assertIsNone(record["deputy"])
        self.assertTrue(record["log"][-1]["standing"])

    def test_standing_takeover_refusals(self) -> None:
        self.appoint()
        self.deputy_session()
        self.set_lane("claude-personal", "all", status="blocked", usable=0)
        self.assertIn("the lead was active", self.standing_takeover())
        self.quiet_lead()
        self.set_lane("claude-personal", "all", status="blocked", usable=0, stale=True)
        self.assertIn("the lead's pool is unknown", self.standing_takeover())
        self.set_lane("claude-personal", "all", stale=False)
        self.config["accounts"][2]["billing"] = "work"
        self.write()
        self.assertIn("context changed since appointment", self.standing_takeover())
        self.config["accounts"][2]["billing"] = "personal"
        self.write()
        AUTHORITY.offer("team", LEAD, 1, AUTHORITY.member("someone", "claude"))
        self.assertIn("already offered", self.standing_takeover())

    def test_only_a_session_on_the_deputy_account_may_take_over_standing(self) -> None:
        self.appoint()
        MAILBOX.register(
            MAILBOX.root(),
            session="claude-other",
            harness="claude",
            cwd=str(self.repo),
            account=str(self.claude_home),
        )
        refused = self.authority(
            "takeover",
            "--team",
            "team",
            "--session",
            "claude-other",
            "--harness",
            "claude",
            "--expect-generation",
            "1",
            "--standing",
            ok=False,
        )
        self.assertIn("named deputy's account", refused)

    # Hook notices

    def hook(self, session, harness, cwd=None):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(cwd or self.repo)}
        with (
            mock.patch.object(MAILBOX, "harness_pid", return_value=os.getpid()),
            mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]),
            mock.patch.object(
                MAILBOX, "account_home", return_value=str(self.claude_home) if harness == "claude" else "/x"
            ),
        ):
            output = MAILBOX.hook(payload, harness=harness, base=MAILBOX.root())
        MAILBOX.annotate(MAILBOX.root(), session, successionCheckedAt=None)
        return (output or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_the_lead_hears_once_per_status_change(self) -> None:
        self.appoint()
        self.assertNotIn("Team team", self.hook(LEAD, "claude"))
        self.set_lane("claude-personal", "all", status="low", usable=5)
        notice = self.hook(LEAD, "claude")
        self.assertIn("your pool claude-personal:all is low", notice)
        self.assertIn("authority.py offer --team team", notice)
        self.assertNotIn("Team team", self.hook(LEAD, "claude"))
        self.set_lane("claude-personal", "all", status="blocked", usable=0)
        self.assertIn("is blocked", self.hook(LEAD, "claude"))

    def test_the_deputy_hears_when_the_lead_is_out_and_gone_without_a_stop(self) -> None:
        self.appoint()
        self.deputy_session()
        self.set_lane("claude-personal", "all", status="blocked", usable=0)
        MAILBOX.unregister(MAILBOX.root(), LEAD)  # died: no Stop, no SessionEnd, no further turns
        notice = self.hook(DEPUTY, "codex")
        self.assertIn("the lead's pool claude-personal:all is blocked", notice)
        self.assertIn("--standing", notice)
        self.assertNotIn("Team team", self.hook(DEPUTY, "codex"))

    def test_another_repository_on_the_same_account_is_not_the_deputy(self) -> None:
        self.appoint()
        elsewhere = self.root / "work-repo"
        subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
        self.deputy_session("codex-elsewhere", cwd=elsewhere)
        self.set_lane("claude-personal", "all", status="blocked", usable=0)
        self.quiet_lead()
        self.assertNotIn("Team team", self.hook("codex-elsewhere", "codex", cwd=elsewhere))
        refused = self.authority(
            "takeover",
            "--team",
            "team",
            "--session",
            "codex-elsewhere",
            "--harness",
            "codex",
            "--expect-generation",
            "1",
            "--standing",
            ok=False,
        )
        self.assertIn("team's repository", refused)

    def test_a_process_that_cannot_be_checked_counts_as_running(self) -> None:
        peer = {"pid": os.getpid(), "pidBirth": "Thu Oct 22 00:00:00 2026"}
        with mock.patch.object(MAILBOX, "process_started", return_value=None):
            self.assertFalse(SUCCESSION.pid_replaced(peer))
        with mock.patch.object(MAILBOX, "pid_alive", return_value=None):
            self.assertFalse(SUCCESSION.pid_replaced(peer))
        with mock.patch.object(MAILBOX, "pid_alive", return_value=False):
            self.assertTrue(SUCCESSION.pid_replaced(peer))

    def test_a_stale_deputy_pool_is_not_healthy(self) -> None:
        self.appoint()
        self.deputy_session()
        self.quiet_lead()
        self.set_lane("claude-personal", "all", status="blocked", usable=0)
        self.set_lane("codex", "codex", stale=True)
        self.assertIn("the deputy's own pool is unknown", self.standing_takeover())

    def test_an_offer_made_during_the_monitor_read_wins(self) -> None:
        self.appoint()
        self.deputy_session()
        self.quiet_lead()
        self.set_lane("claude-personal", "all", status="blocked", usable=0)
        succession = AUTHORITY.module("succession")
        real = succession.fresh_view

        def returning_lead():
            AUTHORITY.offer("team", LEAD, 1, AUTHORITY.member("someone", "claude"))
            return real()

        with mock.patch.object(succession, "fresh_view", returning_lead):
            self.assertIn("already offered", self.standing_takeover())

    def test_set_billing_never_changes_an_existing_context(self) -> None:
        refused = self.authority("set-billing", "--account", "codex", "--billing", "work", ok=False)
        self.assertIn("already personal", refused)
        self.authority("set-billing", "--account", "codex", "--billing", "personal")

    def test_a_lead_without_a_deputy_is_told_once(self) -> None:
        self.authority(
            "appoint",
            "--team",
            "team",
            "--lead-session",
            LEAD,
            "--lead-harness",
            "claude",
            "--lead-pool",
            "claude-personal:all",
            "--repository",
            self.git_dir,
        )
        self.assertIn("has no deputy", self.hook(LEAD, "claude"))
        self.assertNotIn("has no deputy", self.hook(LEAD, "claude"))

    def test_proposed_deputy_sessions_are_in_the_teams_repository(self) -> None:
        elsewhere = self.root / "other-repo"
        subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
        self.deputy_session("codex-here")
        self.deputy_session("codex-there", cwd=elsewhere)
        worker = self.deputy_session("codex-worker")
        MAILBOX.annotate(MAILBOX.root(), worker["session"], delegatedRun="run-1")
        proposal = SUCCESSION.propose(
            {
                "session": LEAD,
                "harness": "claude",
                "model": "claude-opus-5-5",
                "accountHome": str(self.claude_home),
                "repo": self.git_dir,
            },
            view=SUCCESSION.fresh_view(),
            config=SUCCESSION.monitor_config(),
            routes={},
            peers=MAILBOX.peers(MAILBOX.root()),
        )
        self.assertEqual([s["session"] for s in proposal["deputy"]["sessions"]], ["codex-here"])

    def test_the_deputy_is_told_only_when_a_standing_takeover_would_pass(self) -> None:
        self.appoint()
        self.deputy_session()
        self.quiet_lead()
        self.set_lane("claude-personal", "all", status="credits", usable=0)
        # On credits with the lead's process alive, a takeover would be refused: no notice, nothing latched.
        self.assertNotIn("Team team", self.hook(DEPUTY, "codex"))
        MAILBOX.annotate(MAILBOX.root(), LEAD, pid=2**22 - 3)
        self.assertIn("is credits", self.hook(DEPUTY, "codex"))

    def test_an_offered_deputy_is_told_to_review_and_accept(self) -> None:
        self.appoint()
        self.deputy_session()
        AUTHORITY.offer("team", LEAD, 1, AUTHORITY.member(DEPUTY, "codex"))
        notice = self.hook(DEPUTY, "codex")
        self.assertIn("offered you the handover", notice)
        self.assertIn("authority.py accept --team team", notice)
        self.assertNotIn("offered you the handover", self.hook(DEPUTY, "codex"))

    def test_no_monitor_cache_means_no_check(self) -> None:
        self.appoint()
        self.set_lane("claude-personal", "all", status="low", usable=5)
        (Path(os.environ["MONITOR_CACHE_DIR"]) / "snapshot.json").unlink()
        self.assertNotIn("Team team", self.hook(LEAD, "claude"))


if __name__ == "__main__":
    unittest.main()
