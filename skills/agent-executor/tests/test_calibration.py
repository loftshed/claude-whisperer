from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import test_run_agent as fixtures

CALIBRATION = fixtures.RUNNER.bundled_module("calibration")


def manifest():
    return {"tasks": [{"task_id": f"task-{index}", "category": "bug", "objective": "Preserve a scoped behavior",
        "start_revision": "a" * 40, "acceptance_criteria": ["Expected behavior holds"],
        "context_packet": {"decisions": ["Preserve scope"]}, "provenance": "Reviewed executor issue",
        "split": "held_out" if index >= 9 else "training", "repeat": index < 2} for index in range(12)]}


class CalibrationTests(unittest.TestCase):
    def test_missing_holdout_and_repeats_cannot_initialize_a_comparison(self):
        values = manifest()
        CALIBRATION.validate_manifest(values)
        for task in values["tasks"]:
            task["split"] = "training"
        with self.assertRaisesRegex(ValueError, "held-out"):
            CALIBRATION.validate_manifest(values)
        values = manifest()
        for task in values["tasks"]:
            task["repeat"] = False
        with self.assertRaisesRegex(ValueError, "repeat"):
            CALIBRATION.validate_manifest(values)

    def test_failed_attempt_costs_count_and_incomplete_data_does_not_promote(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            CALIBRATION.write(folder / "manifest.json", manifest())
            (folder / "results").mkdir()
            row = {"candidate": "observed-candidate", "strategy": "fresh_worker", "task_id": "task-0",
                "repeat_index": 0, "accepted": True, "regressions": [], "missed_constraints": [],
                "review_false_positives": [], "wall_seconds": 30, "human_repair_minutes": 2,
                "provider_quota_units": None, "attempts": [
                    {"status": "failed", "tariff_estimate": {"estimated_usd": .01}, "usage": {"status": "known"}},
                    {"status": "completed", "tariff_estimate": {"estimated_usd": .02}, "usage": {"status": "known"}}]}
            CALIBRATION.write(folder / "results/one.json", row)
            result = CALIBRATION.summarize(folder)
            self.assertEqual(result["groups"][0]["api_reference_cost_per_accepted_task"], .03)
            self.assertEqual(result["groups"][0]["accepted"], 1)
            self.assertEqual(result["promotion"], "not_established")
            self.assertFalse(result["comparison_ready"])
            self.assertEqual(len(result["groups"][0]["missing_trials"]), 13)
            row["attempts"][0]["tariff_estimate"] = None
            row["attempts"][0]["usage"]["status"] = "unknown"
            CALIBRATION.write(folder / "results/one.json", row)
            result = CALIBRATION.summarize(folder)
            self.assertIsNone(result["groups"][0]["api_reference_cost_per_accepted_task"])
            self.assertEqual(result["groups"][0]["unknown_usage_trials"], 1)


if __name__ == "__main__":
    unittest.main()
