"""Model family selectors: `deepseek`, `gpt:luna`, `gemini:fast` resolve to the newest live ID.

Rules live in references/families.json; a `families` key in the user's config overrides or adds a family
by name. The code holds no model IDs: a new release is picked up from the live catalog.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "references" / "families.json"


class SelectorError(ValueError):
    pass


def load_families(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    families = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))["families"]
    if config is not None and not isinstance(config, dict):
        raise SelectorError("the config must be a JSON object")
    overrides = (config or {}).get("families")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise SelectorError("`families` in the config must be an object of family name -> rules")
    for name, family in overrides.items():
        if not isinstance(family, dict) or not family.get("engine") or not isinstance(family.get("tiers"), dict):
            raise SelectorError(f"family {name!r} in the config needs an engine and a tiers object")
        families[name] = family
    return families


def parse_selector(text: str, families: dict[str, dict[str, Any]]) -> tuple[str, str | None] | None:
    """`deepseek`, `deepseek:pro` or `latest:deepseek:pro` -> (family, tier or None); None if not a selector."""
    parts = text.split(":")
    if parts[0] == "latest" and len(parts) > 1:
        parts = parts[1:]
    if len(parts) > 2 or parts[0] not in families or "" in parts:
        return None
    return parts[0], (parts[1] if len(parts) == 2 else None)


def words(model_id: str) -> list[str]:
    """An ID's words: `/` and `-` both separate them."""
    return model_id.replace("/", "-").split("-")


def version_of(model_id: str) -> tuple[int, ...]:
    """The first word that is a version like `6`, `6.1` or `v4.1`, as a tuple; () when there is none.

    Only the first one counts, so a later date suffix (`-0731`) never outranks the version.
    """
    for word in words(model_id):
        parts = word.removeprefix("v").split(".")
        if all(part.isdigit() for part in parts):
            return tuple(int(part) for part in parts)
    return ()


def resolve(family_name: str, tier: str | None, families: dict[str, dict[str, Any]], available: list[str]) -> str:
    family = families[family_name]
    tier = tier or family.get("default_tier") or next(iter(family["tiers"]))
    if tier not in family["tiers"]:
        raise SelectorError(f"{family_name} has no tier {tier!r}; tiers: {', '.join(family['tiers'])}")
    value = family["tiers"][tier]
    if value in available:
        return value
    prefix = family.get("prefix")
    if not prefix:
        # Without a prefix a keyword would match across vendors, so tier values must be exact IDs.
        raise SelectorError(f"{value} ({family_name}:{tier}) is not in the live {family['engine']} catalog")
    keyword = f"-{'-'.join(words(value))}-"
    matches = [i for i in available if i.startswith(prefix) and keyword in f"-{'-'.join(words(i[len(prefix) :]))}-"]
    if not matches:
        raise SelectorError(f"no {family_name}:{tier} model in the live {family['engine']} catalog")
    return max(matches, key=version_of)


def report(families: dict[str, dict[str, Any]], catalogs: dict[str, list[str]]) -> list[dict[str, Any]]:
    rows = []
    for name, family in families.items():
        available = catalogs.get(family["engine"])
        resolved: dict[str, str | None] = {}
        for tier in family["tiers"]:
            try:
                resolved[tier] = None if available is None else resolve(name, tier, families, available)
            except SelectorError:
                resolved[tier] = None
        rows.append(
            {
                "family": name,
                "engine": family["engine"],
                "default_tier": family.get("default_tier"),
                "catalog": "live" if available is not None else "unavailable",
                "resolved": resolved,
            }
        )
    return rows
