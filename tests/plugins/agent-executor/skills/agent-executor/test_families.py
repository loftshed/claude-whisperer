from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[5] / "plugins" / "agent-executor" / "skills" / "agent-executor"
os.environ["AI_USAGE_BIN"] = str(Path(__file__).parent / "no-ai-usage")
# Families come from the bundled defaults only, never the developer's own config.
os.environ["AGENT_EXECUTOR_CONFIG"] = str(Path(tempfile.mkdtemp()) / "absent-config.json")


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"agent_executor_{name}", SKILL_DIR / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAMILIES = load("families")
RUNNER = load("run_agent")
COORDINATOR = RUNNER.bundled_module("coordinator")
RULES = FAMILIES.load_families()
CODEX = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.5", "gpt-oss-120b-medium"]
AGY = ["gemini-3.8-flash-medium", "gemini-3.10-flash-medium", "gemini-3.9-flash-medium", "gemini-3.1-pro-high"]
ALIASES = ["openrouter/~deepseek/deepseek-flash-latest", "openrouter/deepseek/deepseek-v4.1-flash"]
DEEPSEEK = [
    "openrouter/deepseek/deepseek-v4-flash-0731",
    "openrouter/deepseek/deepseek-v4.1-flash",
    "openrouter/deepseek/deepseek-v3-flash",
]


def live(ids: list[str]) -> dict:
    return {"status": "live", "models": [{"id": i} for i in ids], "executable": "/bin/true"}


def with_family(name: str, family: dict) -> dict:
    return FAMILIES.load_families({"families": {name: family}})


class FamilyResolutionTests(unittest.TestCase):
    def resolve(self, selector: str, available: list[str], rules: dict = RULES) -> str:
        name, tier = FAMILIES.parse_selector(selector, rules)
        return FAMILIES.resolve(name, tier, rules, available)

    def test_highest_version_wins_numerically_not_lexically(self) -> None:
        self.assertEqual(self.resolve("gpt:sol", CODEX), "gpt-6.1-sol")
        self.assertEqual(self.resolve("gpt:sol", [*CODEX, "gpt-10-sol"]), "gpt-10-sol")
        self.assertEqual(self.resolve("gemini", AGY), "gemini-3.10-flash-medium")

    def test_tier_keyword_matches_whole_words_only(self) -> None:
        self.assertEqual(self.resolve("gpt:luna", CODEX), "gpt-6-luna")
        self.assertEqual(self.resolve("gemini:pro", AGY), "gemini-3.1-pro-high")
        with self.assertRaises(FAMILIES.SelectorError):
            self.resolve("gpt:sol", ["gpt-6-solar", "gpt-oss-120b-medium"])

    def test_bare_family_uses_default_tier_and_latest_is_optional(self) -> None:
        self.assertEqual(self.resolve("gpt", CODEX), "gpt-6.1-sol")
        self.assertEqual(self.resolve("latest:gpt:luna", CODEX), "gpt-6-luna")

    def test_exact_alias_is_used_as_is(self) -> None:
        self.assertEqual(self.resolve("deepseek", ALIASES), "openrouter/~deepseek/deepseek-flash-latest")
        self.assertEqual(self.resolve("claude", ["opus", "sonnet"]), "opus")

    def test_unknown_tier_and_missing_model_raise(self) -> None:
        with self.assertRaises(FAMILIES.SelectorError):
            self.resolve("gpt:nope", CODEX)
        with self.assertRaises(FAMILIES.SelectorError):
            self.resolve("deepseek:pro", ALIASES)

    def test_exact_ids_unknown_names_and_empty_parts_are_not_selectors(self) -> None:
        for text in ("gpt-6-sol", "mystery", "gpt:", ":sol", "latest:", "latest:gpt:"):
            with self.subTest(text=text):
                self.assertIsNone(FAMILIES.parse_selector(text, RULES))

    def test_a_v_version_outranks_a_later_date_suffix(self) -> None:
        self.assertEqual(FAMILIES.version_of("deepseek-v4-flash-0731"), (4,))
        rules = with_family(
            "ds", {"engine": "opencode", "prefix": "openrouter/deepseek/deepseek-", "tiers": {"flash": "flash"}}
        )
        self.assertEqual(self.resolve("ds", DEEPSEEK, rules), "openrouter/deepseek/deepseek-v4.1-flash")

    def test_a_slash_separates_words(self) -> None:
        rules = with_family("vendor", {"engine": "opencode", "prefix": "openrouter/", "tiers": {"ds": "deepseek"}})
        available = ["openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseeker/x-9"]
        self.assertEqual(self.resolve("vendor", available, rules), "openrouter/deepseek/deepseek-v4-flash")

    def test_a_family_without_a_prefix_takes_exact_ids_only(self) -> None:
        rules = with_family("loose", {"engine": "opencode", "tiers": {"any": "flash"}})
        with self.assertRaises(FAMILIES.SelectorError):
            self.resolve("loose", DEEPSEEK, rules)

    def test_config_adds_a_family_without_touching_the_defaults(self) -> None:
        merged = with_family("mistral", {"engine": "opencode", "tiers": {"big": "mistral-large"}})
        self.assertIn("mistral", merged)
        self.assertIn("gpt", merged)

    def test_malformed_config_raises_a_selector_error(self) -> None:
        for config in (
            [],
            {"families": []},
            {"families": {"bad": {"engine": "codex"}}},
            {"families": {"bad": {"engine": "codex", "tiers": ["sol"]}}},
        ):
            with self.subTest(config=config), self.assertRaises(FAMILIES.SelectorError):
                FAMILIES.load_families(config)

    def test_report_keeps_families_whose_engine_has_no_catalog(self) -> None:
        rows = {row["family"]: row for row in FAMILIES.report(RULES, {"codex": CODEX})}
        self.assertEqual(rows["gpt"]["catalog"], "live")
        self.assertEqual(rows["gemini"]["catalog"], "unavailable")
        self.assertEqual(set(rows["gemini"]["resolved"].values()), {None})


class SelectLiveModelTests(unittest.TestCase):
    def test_selector_resolves_against_the_live_catalog(self) -> None:
        with mock.patch.object(RUNNER, "note_selector_advance"):
            model, reason = RUNNER.select_live_model("codex", "gpt:sol", live(CODEX))
        self.assertEqual((model, reason), ("gpt-6.1-sol", "family_latest"))

    def test_exact_id_still_wins_over_selector_logic(self) -> None:
        model, reason = RUNNER.select_live_model("codex", "gpt-5.6-sol", live(CODEX))
        self.assertEqual((model, reason), ("gpt-5.6-sol", "exact_user_request"))

    def test_selector_for_another_engine_is_rejected(self) -> None:
        with self.assertRaises(RUNNER.RunnerError):
            RUNNER.select_live_model("codex", "deepseek", live(CODEX))

    def test_every_selection_outcome_has_a_preflight_label(self) -> None:
        with mock.patch.object(RUNNER, "note_selector_advance"):
            outcomes = {
                RUNNER.select_live_model("codex", "gpt-6-sol", live(CODEX))[1],
                RUNNER.select_live_model("codex", "gpt:sol", live(CODEX))[1],
                RUNNER.select_live_model("claude", "claude-opus-5", live(["opus"]))[1],
            }
            with mock.patch.object(RUNNER, "preferred_model", return_value="gpt-6-sol"):
                outcomes.add(RUNNER.select_live_model("codex", None, live(CODEX))[1])
        self.assertEqual(outcomes, set(RUNNER.MODEL_PREFLIGHT))
        self.assertEqual(RUNNER.MODEL_PREFLIGHT["family_latest"], "live_family_resolved")

    def test_a_selector_resolving_to_a_native_family_is_refused_through_opencode(self) -> None:
        rules = with_family("sneaky", {"engine": "opencode", "tiers": {"a": "openrouter/openai/gpt-6-sol"}})
        with (
            mock.patch.object(RUNNER, "load_model_families", return_value=rules),
            mock.patch.object(RUNNER, "discover_engine_catalog", return_value=live(["openrouter/openai/gpt-6-sol"])),
            mock.patch.object(RUNNER, "note_selector_advance"),
            self.assertRaises(RUNNER.RunnerError) as caught,
        ):
            RUNNER.executor_preflight("opencode", "sneaky")
        self.assertEqual(caught.exception.exit_code, RUNNER.EXIT_ROUTE_NOT_ALLOWED)


class SelectorCacheTests(unittest.TestCase):
    def test_a_corrupt_cache_is_replaced_and_an_advance_is_noted(self) -> None:
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"AGENT_EXECUTOR_HOME": home}):
            path = Path(home) / "resolved-selectors-v1.json"
            path.write_text("[]", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                RUNNER.note_selector_advance("gpt:sol", "gpt-6-sol")
                RUNNER.note_selector_advance("gpt:sol", "gpt-6-sol")
            self.assertEqual(stderr.getvalue(), "")
            with contextlib.redirect_stderr(stderr):
                RUNNER.note_selector_advance("gpt:sol", "gpt-6.1-sol")
            self.assertIn("AGENT_NOTE=model_advanced selector=gpt:sol from=gpt-6-sol to=gpt-6.1-sol", stderr.getvalue())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"gpt:sol": "gpt-6.1-sol"})


class ResumePinTests(unittest.TestCase):
    def pinned(self, model: str | None, engine: str = "codex") -> str | None:
        with tempfile.TemporaryDirectory() as folder:
            previous = Path(folder) / "result.json"
            previous.write_text(json.dumps({"engine": "codex", "model": "gpt-6-sol"}), encoding="utf-8")
            args = argparse.Namespace(model=model, engine=engine, resume_result=previous)
            return RUNNER.resume_pinned_model(args)

    def test_a_selector_or_no_model_stays_on_the_recorded_model(self) -> None:
        self.assertEqual(self.pinned("gpt:sol"), "gpt-6-sol")
        self.assertEqual(self.pinned(None), "gpt-6-sol")

    def test_an_exact_id_or_another_engine_is_left_for_validation(self) -> None:
        self.assertEqual(self.pinned("gpt-6.1-sol"), "gpt-6.1-sol")
        self.assertEqual(self.pinned("gemini:fast", engine="agy"), "gemini:fast")

    def test_without_a_resume_the_request_is_unchanged(self) -> None:
        args = argparse.Namespace(model="gpt:sol", engine="codex", resume_result=None)
        self.assertEqual(RUNNER.resume_pinned_model(args), "gpt:sol")


class CoordinatorSelectorTests(unittest.TestCase):
    def test_dispatch_records_the_exact_id_a_selector_resolves_to(self) -> None:
        with (
            mock.patch.object(COORDINATOR.RUNNER, "discover_engine_catalog", return_value=live(CODEX)) as discover,
            mock.patch.object(COORDINATOR.RUNNER, "note_selector_advance"),
        ):
            self.assertEqual(COORDINATOR.exact_model("codex", "gpt:sol"), "gpt-6.1-sol")
            self.assertEqual(COORDINATOR.exact_model("codex", "gpt-5.6-sol"), "gpt-5.6-sol")
        discover.assert_called_once_with("codex")

    def test_a_selector_is_native_to_its_family_engine(self) -> None:
        self.assertTrue(COORDINATOR.native("codex", "opencode", "gpt:sol"))
        self.assertFalse(COORDINATOR.native("claude", "opencode", "deepseek"))
        self.assertTrue(COORDINATOR.native("claude", "opencode", "claude:best"))

    def test_profiles_resolve_selectors_and_keep_exact_ids(self) -> None:
        registry = {
            "profiles": [
                {"id": "sol", "selectors": ["gpt:sol", "gpt"]},
                {"id": "flash", "selectors": ["gemini:fast"]},
                {"id": "opus", "model_ids": ["claude-opus-5"]},
            ]
        }

        def catalog(engine: str) -> dict:
            if engine == "codex":
                return live(CODEX)
            raise COORDINATOR.RUNNER.RunnerError("agy is not installed", 13)

        with (
            mock.patch.object(COORDINATOR.RUNNER, "discover_engine_catalog", side_effect=catalog),
            mock.patch.object(COORDINATOR.RUNNER, "note_selector_advance"),
        ):
            profiles = {p["id"]: p["model_ids"] for p in COORDINATOR.resolve_profiles(registry)["profiles"]}
        self.assertEqual(profiles, {"sol": ["gpt-6.1-sol"], "flash": [], "opus": ["claude-opus-5"]})

    def test_a_profile_with_an_unknown_selector_is_rejected(self) -> None:
        with self.assertRaises(COORDINATOR.CoordinationError):
            COORDINATOR.resolve_profiles({"profiles": [{"id": "x", "selectors": ["mystery:tier"]}]})


if __name__ == "__main__":
    unittest.main()
