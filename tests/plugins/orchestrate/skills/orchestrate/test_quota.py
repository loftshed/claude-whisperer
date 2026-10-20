from __future__ import annotations

import contextlib
import datetime as dt
import io
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
ROUTES = RUNNER.bundled_module("routes")
TELEMETRY = RUNNER.bundled_module("telemetry")


def lane(
    account: str,
    pool: str,
    label: str,
    *,
    status: str = "available",
    usable: float = 60,
    surplus: int | None = 10,
    blocked_until: str | None = None,
    credits: dict | None = None,
) -> dict:
    return {
        "label": label,
        "accountId": account,
        "poolId": pool,
        "status": status,
        "credits": credits,
        "availableNowPct": usable,
        "weeklyRemainingPct": usable,
        "surplusPts": surplus,
        "blockedUntil": blocked_until,
        "advice": "on pace",
        "stale": False,
    }


CODEX_ON_CREDITS = lane(
    "codex",
    "codex",
    "Codex · ChatGPT",
    status="credits",
    usable=0,
    surplus=None,
    blocked_until="2026-10-03T14:50:49.000Z",
    credits={"text": "62,168.59 cr", "unlimited": False},
)


def view() -> dict:
    """Shape of `monitor json` (schema monitor.snapshot.v1), lanes ranked best first."""
    return {
        "schema": "monitor.snapshot.v1",
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
            lane(
                "claude-work",
                "all",
                "Claude · work · all models",
                status="blocked",
                usable=0,
                surplus=-5,
                blocked_until="2026-09-26T00:59:00.000Z",
            ),
            lane(
                "claude-work",
                "fable",
                "Claude · work · Fable",
                status="blocked",
                usable=0,
                surplus=-5,
                blocked_until="2026-09-26T00:59:00.000Z",
            ),
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
        self.assertEqual(
            [(entry["accountId"], entry["poolId"]) for entry in lanes],
            [("claude-personal", "fable"), ("claude-work", "fable")],
        )
        self.assertEqual(lanes[0]["billing"], "personal")


class CheckTests(unittest.TestCase):
    def test_statuses(self) -> None:
        self.assertEqual(QUOTA.check("codex", "gpt-5.6-luna", view())["status"], "available")
        self.assertEqual(QUOTA.check("agy", "gemini-3.8-flash-medium", view())["status"], "low")
        self.assertEqual(QUOTA.check("opencode", "openrouter/x", view())["status"], "not_applicable")
        self.assertEqual(QUOTA.check("codex", "gpt-5.6-luna", None)["status"], "unknown")

    def test_blocked_message_names_refill_and_live_alternatives(self) -> None:
        blocked = view()
        blocked["lanes"][4] = lane(
            "codex", "codex", "Codex · ChatGPT", status="blocked", usable=0, blocked_until="2026-09-29T14:45:00.000Z"
        )
        result = QUOTA.check("codex", "gpt-5.6-luna", blocked)
        self.assertEqual(result["status"], "blocked")
        message = QUOTA.blocked_message("codex", "gpt-5.6-luna", result, blocked)
        self.assertIn("until 2026-09-29T14:45:00.000Z", message)
        self.assertIn("--engine agy (claude-* / gpt-oss-*): Antigravity · Claude & GPT, 27% usable now", message)
        self.assertNotIn("--engine codex", message)

    def test_a_pool_on_credits_is_usable_and_named_as_an_alternative(self) -> None:
        current = view()
        current["lanes"][4] = CODEX_ON_CREDITS
        result = QUOTA.check("codex", "gpt-5.6-luna", current)
        self.assertEqual(
            (result["status"], result["credits"]), ("credits", {"text": "62,168.59 cr", "unlimited": False})
        )
        current["lanes"][3] = lane(
            "antigravity",
            "gemini",
            "Antigravity · Gemini",
            status="blocked",
            usable=0,
            blocked_until="2026-10-02T20:44:52.000Z",
        )
        gemini = QUOTA.check("agy", "gemini-3.8-flash-medium", current)
        message = QUOTA.blocked_message("agy", "gemini-3.8-flash-medium", gemini, current)
        self.assertIn("--engine codex (gpt-*): Codex · ChatGPT, on credits, 62,168.59 cr left", message)
        registry = {"profiles": [{"id": "luna", "model_ids": ["gpt-5.6-luna"], "roles": ["implementation"]}]}
        [record] = QUOTA.access_records(current, registry)
        self.assertEqual((record["available"], record["billing_mode"]), (True, "credits"))
        self.assertEqual(record["quota"]["status"], "credits")

    def test_an_account_monitor_could_not_read_is_unknown_not_low(self) -> None:
        failed = view()
        failed["lanes"][4] = {
            "label": "Codex · ChatGPT",
            "accountId": "codex",
            "poolId": None,
            "status": "error",
            "availableNowPct": 0,
            "weeklyRemainingPct": 0,
            "surplusPts": None,
            "blockedUntil": None,
            "advice": "no data: codex app-server exited 1",
            "stale": True,
        }
        result = QUOTA.check("codex", "gpt-5.6-luna", failed)
        self.assertEqual(result, {"status": "unknown", "detail": "no data: codex app-server exited 1"})
        registry = {"profiles": [{"id": "luna", "model_ids": ["gpt-5.6-luna"], "roles": ["implementation"]}]}
        [record] = QUOTA.access_records(failed, registry)
        self.assertEqual((record["available"], record["source"]), (True, "monitor:unknown"))

    def test_claude_config_dir_selects_matching_account_pool(self) -> None:
        current = view()
        # Default ~/.claude (work) is blocked even though claude-personal is first in the lane list.
        work_result = QUOTA.check("claude", "opus", current, claude_config_dir=Path("~/.claude"))
        self.assertEqual((work_result["accountId"], work_result["status"]), ("claude-work", "blocked"))
        personal_result = QUOTA.check("claude", "opus", current, claude_config_dir=Path("~/.claude-personal"))
        self.assertEqual((personal_result["accountId"], personal_result["status"]), ("claude-personal", "available"))
        # check_quota alias is also available.
        self.assertEqual(
            QUOTA.check_quota("claude", "opus", current, claude_config_dir="~/.claude")["accountId"], "claude-work"
        )
        # Inherited CLAUDE_CONFIG_DIR does not redirect a default check (claude_config_dir=None),
        # because executor_environment("claude") strips CLAUDE_CONFIG_DIR unless --claude-config-dir is passed.
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/Users/example/.claude-personal"}):
            default_result = QUOTA.check("claude", "opus", current, claude_config_dir=None)
        self.assertEqual((default_result["accountId"], default_result["status"]), ("claude-work", "blocked"))
        # Parent path segments containing "personal" or "work" do not shadow the profile directory basename.
        nested_work = QUOTA.check("claude", "opus", current, claude_config_dir="/Users/example/personal/.claude-work")
        self.assertEqual(nested_work["accountId"], "claude-work")
        # A lane with availableNowPct=None is unknown, not low, and never crashes blocked_message.
        missing_pct = view()
        missing_pct["lanes"][3]["availableNowPct"] = None
        self.assertEqual(QUOTA.check("agy", "gemini-3.8-flash-medium", missing_pct)["status"], "unknown")
        self.assertIn("quota is exhausted", QUOTA.blocked_message("claude", "opus", work_result, missing_pct))


class RecommendTests(unittest.TestCase):
    registry = {
        "profiles": [
            {
                "id": "luna",
                "model_ids": ["gpt-5.6-luna"],
                "roles": ["implementation"],
                "effort_candidate": "medium",
                "prompt_adjustment": "",
            },
            {
                "id": "flash",
                "model_ids": ["gemini-3.8-flash-medium"],
                "roles": ["implementation"],
                "effort_candidate": "medium",
                "prompt_adjustment": "",
            },
            {
                "id": "sonnet",
                "model_ids": ["claude-sonnet-5"],
                "roles": ["implementation"],
                "effort_candidate": "high",
                "prompt_adjustment": "",
            },
        ]
    }

    def recommend(self, current: dict | None = None, **packet: object) -> list[tuple[str, str]]:
        records = QUOTA.access_records(current or view(), self.registry, "claude")
        result = POLICY.recommend(
            {"role": "implementation", "host": "claude", "access": records, **packet}, self.registry
        )
        return [(candidate["model"], candidate["access_source"]) for candidate in result["candidates"]]

    def test_a_pool_on_credits_ranks_after_free_room_and_before_nearly_empty_pools(self) -> None:
        current = view()
        current["lanes"][4] = CODEX_ON_CREDITS
        # Gemini has 6% usable (low): Codex on credits can finish a task, so it comes first.
        self.assertEqual(
            self.recommend(current),
            [("gpt-5.6-luna", "monitor:codex"), ("gemini-3.8-flash-medium", "monitor:antigravity")],
        )
        current["lanes"][3] = lane("antigravity", "gemini", "Antigravity · Gemini", usable=60, surplus=2)
        # Free quota with room goes first: it is lost at the rollover, credits are not.
        self.assertEqual(
            self.recommend(current),
            [("gemini-3.8-flash-medium", "monitor:antigravity"), ("gpt-5.6-luna", "monitor:codex")],
        )

    def test_live_access_drops_blocked_pools_and_personal_quota_by_default(self) -> None:
        # Codex has room; Gemini is low (6% usable), so it ranks after despite more unused capacity.
        self.assertEqual(
            self.recommend(), [("gpt-5.6-luna", "monitor:codex"), ("gemini-3.8-flash-medium", "monitor:antigravity")]
        )

    def test_personal_quota_joins_when_authorized_and_ranks_by_unused_capacity(self) -> None:
        self.assertEqual(
            self.recommend(personal_quota_authorized=True)[0], ("claude-sonnet-5", "monitor:claude-personal")
        )

    def test_without_monitor_access_is_unknown_not_zero(self) -> None:
        records = QUOTA.access_records(None, self.registry)
        self.assertTrue(all(record["available"] and record["source"] == "monitor:unknown" for record in records))


class OpenRouterCreditTests(unittest.TestCase):
    def opencode_row(self, credit: dict | None) -> dict:
        return next(row for row in QUOTA.route_table(view(), credit) if row["engine"] == "opencode")

    def test_opencode_row_shows_remaining_dollars(self) -> None:
        row = self.opencode_row({"totalUsd": 210, "usedUsd": 200.128, "remainingUsd": 9.872})
        self.assertEqual((row["status"], row["detail"]), ("available", "$9.87 OpenRouter credit left"))
        self.assertEqual(self.opencode_row({"totalUsd": 10, "usedUsd": 9.5, "remainingUsd": 0.5})["status"], "low")
        self.assertEqual(self.opencode_row({"totalUsd": 10, "usedUsd": 10, "remainingUsd": 0})["status"], "blocked")
        self.assertEqual(self.opencode_row(None)["status"], "unknown")

    def test_key_comes_from_env_then_opencode_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "opencode" / "auth.json"
            auth.parent.mkdir()
            auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "sk-or-stored"}}))
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": directory, "OPENROUTER_API_KEY": ""}):
                self.assertEqual(QUOTA.openrouter_key(), "sk-or-stored")
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": directory, "OPENROUTER_API_KEY": "sk-or-env"}):
                self.assertEqual(QUOTA.openrouter_key(), "sk-or-env")
        with (
            tempfile.TemporaryDirectory() as empty,
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": empty, "OPENROUTER_API_KEY": ""}),
        ):
            self.assertIsNone(QUOTA.openrouter_key())
            self.assertIsNone(QUOTA.openrouter_credit())

    def test_credit_parses_the_credits_endpoint_and_failures_are_unknown(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":{"total_credits":210,"total_usage":200.5}}'
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}):
            with mock.patch.object(QUOTA.urllib.request, "urlopen", return_value=response) as urlopen:
                self.assertEqual(QUOTA.openrouter_credit(), {"totalUsd": 210.0, "usedUsd": 200.5, "remainingUsd": 9.5})
            self.assertEqual(urlopen.call_args.args[0].get_header("Authorization"), "Bearer sk-or-test")
            with mock.patch.object(QUOTA.urllib.request, "urlopen", side_effect=OSError("offline")):
                self.assertIsNone(QUOTA.openrouter_credit())


class RunnerGateTests(unittest.TestCase):
    def fake_monitor(self, directory: str, payload: dict) -> str:
        script = Path(directory) / "monitor"
        script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(payload)}\nEOF\n")
        script.chmod(0o755)
        return str(script)

    def test_snapshot_reads_monitor_and_rejects_unknown_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"MONITOR_BIN": self.fake_monitor(directory, view())}):
                self.assertEqual(QUOTA.snapshot()["schema"], "monitor.snapshot.v1")
            with mock.patch.dict(os.environ, {"MONITOR_BIN": self.fake_monitor(directory, {"schema": "other"})}):
                self.assertIsNone(QUOTA.snapshot())
        with mock.patch.dict(os.environ, {"MONITOR_BIN": "/nonexistent/monitor"}):
            self.assertIsNone(QUOTA.snapshot())

    def test_gate_refuses_an_exhausted_pool_with_exit_15_unless_ignored(self) -> None:
        blocked = view()
        blocked["lanes"][4] = lane(
            "codex", "codex", "Codex · ChatGPT", status="blocked", usable=0, blocked_until="2026-09-29T14:45:00.000Z"
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"MONITOR_BIN": self.fake_monitor(directory, blocked)}):
                with self.assertRaises(RUNNER.RunnerError) as caught:
                    RUNNER.quota_gate("codex", "gpt-5.6-luna", ignore=False)
                self.assertEqual(caught.exception.exit_code, 15)
                RUNNER.quota_gate("codex", "gpt-5.6-luna", ignore=True)
                RUNNER.quota_gate("agy", "claude-opus-4-6-thinking", ignore=False)

    def test_gate_lets_a_pool_on_credits_through_and_says_so(self) -> None:
        current = view()
        current["lanes"][4] = CODEX_ON_CREDITS
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"MONITOR_BIN": self.fake_monitor(directory, current)}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    RUNNER.quota_gate("codex", "gpt-5.6-luna", ignore=False)
                self.assertEqual(
                    out.getvalue().strip(),
                    'AGENT_LIFECYCLE=quota_credits pool="Codex \\u00b7 ChatGPT" balance="62,168.59 cr" '
                    'included_quota_back="2026-10-03T14:50:49.000Z"',
                )
                # No OpenRouter key anywhere, so the table's OpenCode row never reaches the network.
                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "XDG_DATA_HOME": directory}):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(RUNNER.quota_main([]), 0)
                codex_row = next(line for line in out.getvalue().splitlines() if line.startswith("codex"))
                self.assertIn("credits   on credits, 62,168.59 cr left  Codex · ChatGPT: on pace", codex_row)

    def test_gate_lets_runs_through_when_quota_is_unknown(self) -> None:
        with mock.patch.dict(os.environ, {"MONITOR_BIN": "/nonexistent/monitor"}):
            RUNNER.quota_gate("codex", "gpt-5.6-luna", ignore=False)


class DispatchableRecommendTests(unittest.TestCase):
    registry = {
        "profiles": [
            {
                "id": "luna",
                "model_ids": ["gpt-5.6-luna"],
                "roles": ["implementation"],
                "effort_candidate": "medium",
                "prompt_adjustment": "",
            },
            {
                "id": "flash",
                "model_ids": ["gemini-3.8-flash-medium"],
                "roles": ["implementation"],
                "effort_candidate": "medium",
                "prompt_adjustment": "",
            },
            {
                "id": "sonnet",
                "model_ids": ["claude-sonnet-5"],
                "roles": ["implementation"],
                "effort_candidate": "high",
                "prompt_adjustment": "",
            },
        ]
    }

    def ranked(self, host: str, current: dict, **packet: object) -> list[str]:
        records = QUOTA.access_records(current, self.registry, host)
        result = POLICY.recommend({"role": "implementation", "host": host, "access": records, **packet}, self.registry)
        return [candidate["model"] for candidate in result["candidates"]]

    def test_a_codex_host_is_offered_claude_through_the_claude_engine(self) -> None:
        roomy = view()
        roomy["lanes"][3] = lane("antigravity", "gemini", "Antigravity · Gemini", usable=60, surplus=2)
        self.assertIn("claude-sonnet-5", self.ranked("codex", roomy, personal_quota_authorized=True))
        records = QUOTA.access_records(roomy, self.registry, "opencode")
        self.assertNotIn("opencode", {record["engine"] for record in records}, "never route hosted models via OpenCode")

    def test_expiring_pools_come_first_then_native(self) -> None:
        current = view()
        current["lanes"][3] = {
            **lane("antigravity", "gemini", "Antigravity · Gemini", usable=60, surplus=2),
            "expiring": True,
            "expiresAt": "2026-09-26T00:44:52.000Z",
            "burnPctPerHour": 7.5,
        }
        # Gemini is expiring: first even though Codex is native to a Codex host and has more surplus.
        self.assertEqual(self.ranked("codex", current), ["gemini-3.8-flash-medium", "gpt-5.6-luna"])
        current["lanes"][3]["expiring"] = False
        # Nothing expiring: the host's own model (native) comes first.
        self.assertEqual(self.ranked("codex", current), ["gpt-5.6-luna", "gemini-3.8-flash-medium"])


class ArchetypeRecommendTests(unittest.TestCase):
    routes = {
        "debug-invariants": {
            "engine": "codex",
            "model": "gpt:sol",
            "effort": "high",
            "alternates": [
                {"engine": "agy", "model": "gemini:flash-high"},
                {"engine": "claude", "model": "claude:frontier", "effort": "high"},
            ],
        }
    }
    registry = {
        "profiles": [
            {"id": "sol", "selectors": ["gpt:sol"], "model_ids": ["gpt-6.1-sol"], "roles": []},
            {
                "id": "fable",
                "selectors": ["claude:frontier"],
                "model_ids": ["claude-fable-5-1"],
                "roles": [],
            },
        ]
    }

    @staticmethod
    def access(status_by_engine: dict[str, str] | None = None) -> list[dict]:
        status_by_engine = status_by_engine or {}
        return [
            {
                "engine": engine,
                "model": model,
                "available": status_by_engine.get(engine) != "blocked",
                "source": f"monitor:{engine}",
                "observed_at": dt.datetime.now(dt.UTC).isoformat(),
                "billing_mode": "subscription",
                "quota": {
                    "status": status_by_engine.get(engine, "available"),
                    "poolId": engine,
                    "surplusPts": 10,
                },
            }
            for engine, model in (
                ("codex", "gpt-6.1-sol"),
                ("agy", "gemini-3.8-flash-high"),
                ("claude", "claude-fable-5-1"),
            )
        ]

    @staticmethod
    def summary(codex: tuple[int, int], claude: tuple[int, int]) -> dict:
        groups = []
        for engine, model, selector, effort, (successes, trials) in (
            ("codex", "gpt-6.1-sol", "gpt:sol", "high", codex),
            ("claude", "claude-fable-5-1", "claude:frontier", "high", claude),
        ):
            groups.append(
                {
                    "task_archetype": "debug-invariants",
                    "engine": engine,
                    "model": model,
                    "effort": effort,
                    "runs": trials,
                    "accepted_runs": successes,
                    "first_pass_runs": successes,
                    "accepted_rate": successes / trials if trials else 0,
                    "wilson_lb_95": TELEMETRY.wilson_lower_bound(successes, trials),
                    "requested_models": [selector],
                }
            )
        return {"groups": groups}

    def recommend(self, access: list[dict], summary: dict, *, personal_quota_authorized: bool = False) -> dict:
        return POLICY.recommend(
            {
                "role": "debug-invariants",
                "host": "codex",
                "access": access,
                "personal_quota_authorized": personal_quota_authorized,
            },
            self.registry,
            route_config=self.routes,
            telemetry_summary=summary,
        )

    def test_archetype_routes_are_available_even_without_profiles_for_the_role(self) -> None:
        summary = self.summary((2, 5), (5, 5))
        result = self.recommend(self.access(), summary)

        self.assertEqual(result["role"], "debug-invariants")
        self.assertEqual(
            [candidate["model"] for candidate in result["candidates"]],
            ["claude:frontier", "gemini:flash-high", "gpt:sol"],
        )
        self.assertEqual(result["candidates"][0]["sample_count"], 5)
        self.assertGreater(result["candidates"][0]["wilson_lb_95"], result["candidates"][2]["wilson_lb_95"])

    def test_low_quota_does_not_reorder_routes_and_blocked_routes_are_removed(self) -> None:
        access = self.access({"agy": "low", "claude": "blocked"})
        summary = self.summary((2, 5), (5, 5))
        result = self.recommend(access, summary)

        self.assertEqual([candidate["model"] for candidate in result["candidates"]], ["gpt:sol", "gemini:flash-high"])
        self.assertEqual(result["candidates"][0]["quota_pace"], "available")
        self.assertEqual(result["candidates"][1]["quota"]["status"], "low")

    def test_stale_or_unattributed_access_records_are_hard_filtered(self) -> None:
        access = self.access()
        access[0]["observed_at"] = "2000-01-01T00:00:00Z"
        access[1]["source"] = ""
        access[2].pop("observed_at")
        result = self.recommend(access, {"groups": []})

        self.assertEqual([candidate["model"] for candidate in result["candidates"]], [])

    def test_wilson_delta_must_exceed_point_zero_five_to_change_route_priority(self) -> None:
        summary = self.summary((950, 1000), (940, 1000))
        result = self.recommend(self.access(), summary)

        self.assertLess(abs(result["candidates"][0]["wilson_lb_95"] - result["candidates"][2]["wilson_lb_95"]), 0.05)
        self.assertEqual(
            [candidate["model"] for candidate in result["candidates"]],
            ["gpt:sol", "gemini:flash-high", "claude:frontier"],
        )

    def test_personal_pools_remain_excluded_without_authorization(self) -> None:
        access = self.access()
        access[2] = {**access[2], "billing_mode": "personal_subscription"}
        summary = self.summary((2, 5), (5, 5))

        self.assertNotIn("claude:frontier", [c["model"] for c in self.recommend(access, summary)["candidates"]])
        self.assertIn(
            "claude:frontier",
            [c["model"] for c in self.recommend(access, summary, personal_quota_authorized=True)["candidates"]],
        )

    def test_all_six_default_archetypes_use_their_configured_candidate_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "no-config.json"
            with mock.patch.dict(os.environ, {"AGENT_EXECUTOR_CONFIG": str(config)}):
                routes = ROUTES.load_routes()
                roles = POLICY.ARCHETYPE_ROLES
                access = self.access()
                summary = {"groups": []}

                for role in roles:
                    with self.subTest(role=role):
                        expected = [candidate["model"] for candidate in ROUTES.route_candidates(role, routes)]
                        result = POLICY.recommend(
                            {"role": role, "host": "unknown", "access": access},
                            {"profiles": []},
                            telemetry_summary=summary,
                        )
                        self.assertEqual([candidate["model"] for candidate in result["candidates"]], expected)

    def test_live_local_receipts_feed_policy_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            runs = home / "runs-v1"
            for engine, model, selector, successes in (
                ("codex", "gpt-6.1-sol", "gpt:sol", 3),
                ("claude", "claude-fable-5-1", "claude:frontier", 5),
            ):
                for index in range(5):
                    directory = runs / f"{engine}-{index}"
                    directory.mkdir(parents=True)
                    (directory / "result.json").write_text(
                        json.dumps(
                            {
                                "task_archetype": "debug-invariants",
                                "engine": engine,
                                "model": model,
                                "requested_model": selector,
                                "effort": {"bound": "high"},
                                "status": "COMPLETE" if index < successes else "FAILED",
                            }
                        ),
                        encoding="utf-8",
                    )
            with mock.patch.dict(os.environ, {"AGENT_EXECUTOR_HOME": temporary}):
                result = POLICY.recommend(
                    {"role": "debug-invariants", "host": "unknown", "access": self.access()},
                    self.registry,
                    route_config=self.routes,
                )

        self.assertEqual(
            [candidate["model"] for candidate in result["candidates"]],
            ["claude:frontier", "gemini:flash-high", "gpt:sol"],
        )
        self.assertEqual([candidate["sample_count"] for candidate in result["candidates"]], [5, None, 5])


if __name__ == "__main__":
    unittest.main()
