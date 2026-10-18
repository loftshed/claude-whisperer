from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[5] / "plugins" / "orchestrate" / "skills" / "orchestrate"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"agent_executor_{name}", SKILL_DIR / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TELEMETRY = load("telemetry")
ROUTES = load("routes")
FAMILIES = load("families")


class TelemetrySummaryTests(unittest.TestCase):
    def write_result(self, folder: Path, name: str, value: dict) -> None:
        run = folder / name
        run.mkdir(parents=True)
        (run / "result.json").write_text(json.dumps(value), encoding="utf-8")

    def test_summarize_groups_receipts_and_computes_rates_and_median(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary)
            self.write_result(
                runs,
                "one",
                {
                    "task_archetype": "debug-invariants",
                    "engine": "codex",
                    "model": "gpt-6-sol",
                    "effort": {"bound": "high"},
                    "status": "COMPLETE",
                    "verification": {"passed": True},
                    "duration_ms": 1000,
                },
            )
            self.write_result(
                runs,
                "two",
                {
                    "role": "debug-invariants",
                    "route": {"engine": "codex", "model": "gpt-6-sol", "effort": "high"},
                    "review": {"task_outcome": "ACCEPTED", "verification": {"status": "passed"}},
                    "resumed_from": "earlier-result.json",
                    "attempts": 1,
                    "elapsed_ms": 3000,
                },
            )
            self.write_result(
                runs,
                "three",
                {
                    "task_archetype": "debug-invariants",
                    "request": {"engine": "codex", "model": "gpt-6-sol", "effort": "high"},
                    "status": "FAILED",
                    "executor_attempts": [{}, {}],
                    "duration_seconds": 5,
                },
            )
            self.write_result(
                runs,
                "four",
                {
                    "task_archetype": "debug-invariants",
                    "engine": "codex",
                    "model": "gpt-6-sol",
                    "effort": "high",
                    "status": "COMPLETE",
                    "verification": {"passed": False},
                    "timing": {"duration_ms": 2000},
                },
            )

            summary = TELEMETRY.summarize_runs(runs)

        self.assertEqual(summary["runs"], 4)
        self.assertEqual(summary["skipped_results"], 0)
        self.assertEqual(
            summary["groups"],
            [
                {
                    "task_archetype": "debug-invariants",
                    "engine": "codex",
                    "model": "gpt-6-sol",
                    "effort": "high",
                    "runs": 4,
                    "first_pass_rate": 0.5,
                    "accepted_rate": 0.75,
                    "wilson_lb_95": 0.300642,
                    "median_duration_ms": 2500.0,
                }
            ],
        )

    def test_small_samples_get_a_lower_wilson_bound_than_larger_successful_samples(self) -> None:
        one_of_one = TELEMETRY.wilson_lower_bound(1, 1)
        forty_five_of_fifty = TELEMETRY.wilson_lower_bound(45, 50)

        self.assertIsNotNone(one_of_one)
        self.assertIsNotNone(forty_five_of_fifty)
        self.assertLess(one_of_one, forty_five_of_fifty)

    def test_missing_metadata_is_grouped_as_unspecified_and_bad_files_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary)
            self.write_result(runs, "missing", {"status": "FAILED"})
            broken = runs / "broken"
            broken.mkdir()
            (broken / "result.json").write_text("{", encoding="utf-8")

            summary = TELEMETRY.summarize_runs(runs)

        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["skipped_results"], 1)
        self.assertEqual(
            summary["groups"][0],
            {
                "task_archetype": "unspecified",
                "engine": "unspecified",
                "model": "unspecified",
                "effort": "unspecified",
                "runs": 1,
                "first_pass_rate": 0.0,
                "accepted_rate": 0.0,
                "wilson_lb_95": 0.0,
                "median_duration_ms": None,
            },
        )

    def test_cli_emits_json_and_default_cache_location_follows_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENT_EXECUTOR_HOME": temporary}), contextlib.redirect_stdout(output):
                exit_code = TELEMETRY.main(["summarize", "--runs-dir", temporary, "--format", "json"])
                default_dir = TELEMETRY.default_runs_dir()

        self.assertEqual(exit_code, 0)
        self.assertEqual(default_dir, Path(temporary) / "runs-v1")
        self.assertEqual(json.loads(output.getvalue())["groups"], [])


class TaskArchetypeRouteTests(unittest.TestCase):
    def test_route_candidates_preserves_primary_first_and_alternate_order(self) -> None:
        routes = {
            "debug-invariants": {
                "engine": "codex",
                "model": "gpt:sol",
                "effort": "high",
                "alternates": [
                    {"engine": "claude", "model": "claude:frontier", "effort": "high"},
                    {"engine": "codex", "model": "gpt:luna", "effort": "max"},
                ],
            }
        }

        candidates = ROUTES.route_candidates("debug-invariants", routes)

        self.assertEqual(
            candidates,
            [
                {"engine": "codex", "model": "gpt:sol", "effort": "high"},
                {"engine": "claude", "model": "claude:frontier", "effort": "high"},
                {"engine": "codex", "model": "gpt:luna", "effort": "max"},
            ],
        )
        candidates[0]["model"] = "changed"
        self.assertEqual(routes["debug-invariants"]["model"], "gpt:sol")

    def test_default_routes_add_six_archetypes_without_changing_existing_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "absent-config.json"
            with mock.patch.dict(os.environ, {"AGENT_EXECUTOR_CONFIG": str(config)}):
                routes = ROUTES.load_routes()

        existing = {"implementation", "consultation", "claude", "gemini", "opencode"}
        archetypes = {
            "debug-invariants",
            "structural-refactor",
            "ui-design-system",
            "architecture-conductor",
            "mechanical-sweep",
            "independent-review",
        }
        self.assertTrue(existing.issubset(routes))
        self.assertTrue(archetypes.issubset(routes))
        families = FAMILIES.load_families()
        for role in archetypes:
            with self.subTest(role=role):
                candidates = ROUTES.route_candidates(role, routes)
                self.assertEqual(len(candidates), 3)
                for candidate in candidates:
                    self.assertTrue(candidate.get("engine") and candidate.get("model"))
                    self.assertIsNotNone(FAMILIES.parse_selector(candidate["model"], families))

    def test_tier_strengths_describe_each_family_tier_without_changing_selector_values(self) -> None:
        families = FAMILIES.load_families()

        for name, family in families.items():
            strengths = family.get("tier_strengths")
            self.assertIsInstance(strengths, dict, name)
            self.assertEqual(set(strengths), set(family["tiers"]), name)
            for tier, value in family["tiers"].items():
                with self.subTest(family=name, tier=tier):
                    self.assertIsInstance(value, str)
                    self.assertTrue(strengths[tier])
                    self.assertTrue(all(isinstance(item, str) and item for item in strengths[tier]))


if __name__ == "__main__":
    unittest.main()
