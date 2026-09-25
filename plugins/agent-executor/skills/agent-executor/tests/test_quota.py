from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_run_agent as fixtures

RUNNER = fixtures.RUNNER
QUOTA = RUNNER.bundled_module("quota")
POLICY = RUNNER.bundled_module("conductor_policy")


def lane(account: str, pool: str, label: str, *, status: str = "available", usable: float = 60,
         surplus: int | None = 10, blocked_until: str | None = None) -> dict:
    return {"label": label, "accountId": account, "poolId": pool, "status": status, "availableNowPct": usable,
            "weeklyRemainingPct": usable, "surplusPts": surplus, "blockedUntil": blocked_until,
            "advice": "on pace", "stale": False}


def view() -> dict:
    """Shape of `ai-usage json` (schema ai-usage.snapshot.v1), lanes ranked best first."""
    return {
        "schema": "ai-usage.snapshot.v1",
        "generatedAt": "2026-09-25T17:00:00.000Z",
        "accounts": [
            {"id": "claude-work", "provider": "claude", "billing": "work"},
            {"id": "claude-personal", "provider": "claude", "billing": "personal"},
            {"id": "codex", "provider": "codex", "billing": None},
            {"id": "antigravity", "provider": "antigravity", "billing": None},
        ],
        "lanes": [
            lane("claude-personal", "all", "Claude · personal · all models", surplus=44),
            lane("claude-personal", "fable", "Claude · personal · Fable", surplus=44),
            lane("antigravity", "claude-gpt", "Antigravity · Claude & GPT", usable=27, surplus=9),
            lane("antigravity", "gemini", "Antigravity · Gemini", usable=6, surplus=2),
            lane("codex", "codex", "Codex · ChatGPT", usable=51, surplus=-19),
            lane("claude-work", "all", "Claude · work · all models", status="blocked", usable=0, surplus=-5,
                 blocked_until="2026-09-26T00:59:00.000Z"),
            lane("claude-work", "fable", "Claude · work · Fable", status="blocked", usable=0, surplus=-5,
                 blocked_until="2026-09-26T00:59:00.000Z"),
        ],
    }


class RouteMappingTests(unittest.TestCase):
    def test_engines_map_onto_the_pool_that_pays_for_them(self) -> None:
        self.assertEqual(QUOTA.route_for("codex", "gpt-5.6-luna"), ("codex", None))
        self.assertEqual(QUOTA.route_for("agy", "gemini-3.8-flash-medium"), ("antigravity", "gemini"))
        self.assertEqual(QUOTA.route_for("agy", "claude-opus-4-6-thinking"), ("antigravity", "claude-gpt"))
        self.assertEqual(QUOTA.route_for("agy", "gpt-oss-120b-medium"), ("antigravity", "claude-gpt"))
        self.assertIsNone(QUOTA.route_for("opencode", "openrouter/z-ai/glm-5.3-flash"))

    def test_per_model_claude_cap_is_preferred_over_the_account_pool(self) -> None:
        lanes = QUOTA.pool_lanes("claude", "claude-fable-5-1", view())
        self.assertEqual([(entry["accountId"], entry["poolId"]) for entry in lanes],
                         [("claude-personal", "fable"), ("claude-work", "fable")])
        self.assertEqual(lanes[0]["billing"], "personal")


class CheckTests(unittest.TestCase):
    def test_statuses(self) -> None:
        self.assertEqual(QUOTA.check("codex", "gpt-5.6-luna", view())["status"], "available")
        self.assertEqual(QUOTA.check("agy", "gemini-3.8-flash-medium", view())["status"], "low")
        self.assertEqual(QUOTA.check("opencode", "openrouter/x", view())["status"], "not_applicable")
        self.assertEqual(QUOTA.check("codex", "gpt-5.6-luna", None)["status"], "unknown")

    def test_blocked_message_names_refill_and_live_alternatives(self) -> None:
        blocked = view()
        blocked["lanes"][4] = lane("codex", "codex", "Codex · ChatGPT", status="blocked", usable=0,
                                   blocked_until="2026-09-29T14:45:00.000Z")
        result = QUOTA.check("codex", "gpt-5.6-luna", blocked)
        self.assertEqual(result["status"], "blocked")
        message = QUOTA.blocked_message("codex", "gpt-5.6-luna", result, blocked)
        self.assertIn("until 2026-09-29T14:45:00.000Z", message)
        self.assertIn("--engine agy (claude-* / gpt-oss-*): Antigravity · Claude & GPT, 27% usable now", message)
        self.assertNotIn("--engine codex", message)


    def test_an_account_ai_usage_could_not_read_is_unknown_not_low(self) -> None:
        failed = view()
        failed["lanes"][4] = {"label": "Codex · ChatGPT", "accountId": "codex", "poolId": None, "status": "error",
                              "availableNowPct": 0, "weeklyRemainingPct": 0, "surplusPts": None, "blockedUntil": None,
                              "advice": "no data: codex app-server exited 1", "stale": True}
        result = QUOTA.check("codex", "gpt-5.6-luna", failed)
        self.assertEqual(result, {"status": "unknown", "detail": "no data: codex app-server exited 1"})
        registry = {"profiles": [{"id": "luna", "model_ids": ["gpt-5.6-luna"], "roles": ["implementation"]}]}
        [record] = QUOTA.access_records(failed, registry)
        self.assertEqual((record["available"], record["source"]), (True, "ai-usage:unknown"))


class RecommendTests(unittest.TestCase):
    registry = {"profiles": [
        {"id": "luna", "model_ids": ["gpt-5.6-luna"], "roles": ["implementation"], "effort_candidate": "medium", "prompt_adjustment": ""},
        {"id": "flash", "model_ids": ["gemini-3.8-flash-medium"], "roles": ["implementation"], "effort_candidate": "medium", "prompt_adjustment": ""},
        {"id": "sonnet", "model_ids": ["claude-sonnet-5"], "roles": ["implementation"], "effort_candidate": "high", "prompt_adjustment": ""},
    ]}

    def recommend(self, **packet: object) -> list[tuple[str, str]]:
        records = QUOTA.access_records(view(), self.registry, "claude")
        result = POLICY.recommend({"role": "implementation", "host": "claude", "access": records, **packet}, self.registry)
        return [(candidate["model"], candidate["access_source"]) for candidate in result["candidates"]]

    def test_live_access_drops_blocked_pools_and_personal_quota_by_default(self) -> None:
        # Codex has room; Gemini is low (6% usable), so it ranks after despite more unused capacity.
        self.assertEqual(self.recommend(), [("gpt-5.6-luna", "ai-usage:codex"),
                                            ("gemini-3.8-flash-medium", "ai-usage:antigravity")])

    def test_personal_quota_joins_when_authorized_and_ranks_by_unused_capacity(self) -> None:
        self.assertEqual(self.recommend(personal_quota_authorized=True)[0], ("claude-sonnet-5", "ai-usage:claude-personal"))

    def test_without_ai_usage_access_is_unknown_not_zero(self) -> None:
        records = QUOTA.access_records(None, self.registry)
        self.assertTrue(all(record["available"] and record["source"] == "ai-usage:unknown" for record in records))


class RunnerGateTests(unittest.TestCase):
    def fake_ai_usage(self, directory: str, payload: dict) -> str:
        script = Path(directory) / "ai-usage"
        script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(payload)}\nEOF\n")
        script.chmod(0o755)
        return str(script)

    def test_snapshot_reads_ai_usage_and_rejects_unknown_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"AI_USAGE_BIN": self.fake_ai_usage(directory, view())}):
                self.assertEqual(QUOTA.snapshot()["schema"], "ai-usage.snapshot.v1")
            with mock.patch.dict(os.environ, {"AI_USAGE_BIN": self.fake_ai_usage(directory, {"schema": "other"})}):
                self.assertIsNone(QUOTA.snapshot())
        with mock.patch.dict(os.environ, {"AI_USAGE_BIN": "/nonexistent/ai-usage"}):
            self.assertIsNone(QUOTA.snapshot())

    def test_gate_refuses_an_exhausted_pool_with_exit_15_unless_ignored(self) -> None:
        blocked = view()
        blocked["lanes"][4] = lane("codex", "codex", "Codex · ChatGPT", status="blocked", usable=0,
                                   blocked_until="2026-09-29T14:45:00.000Z")
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"AI_USAGE_BIN": self.fake_ai_usage(directory, blocked)}):
                with self.assertRaises(RUNNER.RunnerError) as caught:
                    RUNNER.quota_gate("codex", "gpt-5.6-luna", ignore=False)
                self.assertEqual(caught.exception.exit_code, 15)
                RUNNER.quota_gate("codex", "gpt-5.6-luna", ignore=True)
                RUNNER.quota_gate("agy", "claude-opus-4-6-thinking", ignore=False)

    def test_gate_lets_runs_through_when_quota_is_unknown(self) -> None:
        with mock.patch.dict(os.environ, {"AI_USAGE_BIN": "/nonexistent/ai-usage"}):
            RUNNER.quota_gate("codex", "gpt-5.6-luna", ignore=False)


class DispatchableRecommendTests(unittest.TestCase):
    registry = {"profiles": [
        {"id": "luna", "model_ids": ["gpt-5.6-luna"], "roles": ["implementation"], "effort_candidate": "medium", "prompt_adjustment": ""},
        {"id": "flash", "model_ids": ["gemini-3.8-flash-medium"], "roles": ["implementation"], "effort_candidate": "medium", "prompt_adjustment": ""},
        {"id": "sonnet", "model_ids": ["claude-sonnet-5"], "roles": ["implementation"], "effort_candidate": "high", "prompt_adjustment": ""},
    ]}

    def ranked(self, host: str, current: dict, **packet: object) -> list[str]:
        records = QUOTA.access_records(current, self.registry, host)
        result = POLICY.recommend({"role": "implementation", "host": host, "access": records, **packet}, self.registry)
        return [candidate["model"] for candidate in result["candidates"]]

    def test_a_codex_host_is_never_offered_claude_it_cannot_dispatch(self) -> None:
        roomy = view()
        roomy["lanes"][3] = lane("antigravity", "gemini", "Antigravity · Gemini", usable=60, surplus=2)
        self.assertNotIn("claude-sonnet-5", self.ranked("codex", roomy, personal_quota_authorized=True))
        self.assertIn("claude-sonnet-5", self.ranked("claude", roomy, personal_quota_authorized=True))

    def test_expiring_pools_come_first_then_native(self) -> None:
        current = view()
        current["lanes"][3] = {**lane("antigravity", "gemini", "Antigravity · Gemini", usable=60, surplus=2),
                               "expiring": True, "expiresAt": "2026-09-26T00:44:52.000Z", "burnPctPerHour": 7.5}
        # Gemini is expiring: first even though Codex is native to a Codex host and has more surplus.
        self.assertEqual(self.ranked("codex", current), ["gemini-3.8-flash-medium", "gpt-5.6-luna"])
        current["lanes"][3]["expiring"] = False
        # Nothing expiring: the host's own model (native) comes first.
        self.assertEqual(self.ranked("codex", current), ["gpt-5.6-luna", "gemini-3.8-flash-medium"])


if __name__ == "__main__":
    unittest.main()
