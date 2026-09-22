from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "render_brief.py"
SPEC = importlib.util.spec_from_file_location("agent_executor_brief_renderer", SCRIPT)
assert SPEC and SPEC.loader
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)

RUNNER_SCRIPT = Path(__file__).parents[1] / "scripts" / "run_agent.py"
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "agent_executor_runner_for_brief_renderer", RUNNER_SCRIPT
)
assert RUNNER_SPEC and RUNNER_SPEC.loader
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


def brief_spec() -> dict[str, object]:
    return {
        "objective": "Add the bounded implementation.",
        "repository_root": "/tmp/example-repository",
        "current_state": "The focused test exposes the missing behavior.",
        "pre_existing_changes": ["src/unrelated.py"],
        "plan": ["Implement the focused change.", "Add a regression test."],
        "allow_paths": ["src/jobs", "tests/jobs"],
        "track_paths": ["plans/generated-handoff.md"],
        "development_checks": ["python3 -m unittest tests.test_jobs"],
        "acceptance_criteria": ["The regression test passes."],
    }


class BriefRendererTests(unittest.TestCase):
    def test_context_packet_preserves_failed_attempts_and_versioned_evidence(self) -> None:
        spec = brief_spec()
        spec["context_packet"] = {"decisions": ["Keep the existing public API."], "failed_attempts": ["Cancellation alone did not stop the callback."], "versions": ["client 2.4.1"], "evidence": ["tests/race.py: observed callback after cancellation"]}
        rendered = RENDERER.render_brief(spec)
        self.assertIn("Failed attempts:\n- Cancellation alone did not stop the callback.", rendered)
        self.assertIn("Versions:\n- client 2.4.1", rendered)
        self.assertEqual(RUNNER.brief_report_contract_violations(rendered), [])

    def test_renders_complete_runner_compliant_brief(self) -> None:
        rendered = RENDERER.render_brief(brief_spec())
        self.assertIn("# Objective\n\nAdd the bounded implementation.", rendered)
        self.assertEqual(RENDERER.REPORT_HEADINGS, (
            "STATUS", "FILES CHANGED", "COMMANDS RUN", "VERIFICATION", "RISKS OR BLOCKERS"
        ))
        positions = [rendered.index(f"### {heading}") for heading in RENDERER.REPORT_HEADINGS]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(sum(rendered.count(f"### {heading}") for heading in RENDERER.REPORT_HEADINGS), 5)

    def test_rendered_brief_passes_the_runner_preflight_parser(self) -> None:
        self.assertEqual(
            RUNNER.brief_report_contract_violations(RENDERER.render_brief(brief_spec())),
            [],
        )

    def test_rejects_missing_required_fields(self) -> None:
        spec = brief_spec()
        del spec["objective"]
        with self.assertRaisesRegex(RENDERER.BriefSpecError, "missing required fields: objective"):
            RENDERER.render_brief(spec)

    def test_effective_scope_matches_normalized_runner_path_inputs(self) -> None:
        spec = brief_spec()
        spec["allow_paths"] = ["src/jobs/", "src/jobs", "tests/jobs"]
        spec["track_paths"] = ["plans/generated-handoff.md", "src/jobs/output.json"]
        rendered = RENDERER.render_brief(spec)
        self.assertIn("- src/jobs\n- tests/jobs\n- plans/generated-handoff.md\n- src/jobs/output.json", rendered)
        self.assertIn("Runner `--allow-path` prefixes:\n- src/jobs\n- tests/jobs", rendered)
        self.assertIn("Runner `--track-path` deliverables:\n- plans/generated-handoff.md\n- src/jobs/output.json", rendered)

    def test_rejects_invalid_scope_path(self) -> None:
        spec = brief_spec()
        spec["allow_paths"] = ["../outside"]
        with self.assertRaisesRegex(RENDERER.BriefSpecError, "repository-relative"):
            RENDERER.render_brief(spec)


if __name__ == "__main__":
    unittest.main()
