"""Model family selectors: `deepseek`, `gpt:luna`, `gemini:fast` resolve to the newest live ID.

Rules live in references/families.json; a `families` key in the user's config overrides or adds a family
by name. The code holds no model IDs: a new release is picked up from the live catalog.
"""

from __future__ import annotations

import json
import re
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
    if len(parts) > 2 or parts[0] not in families or any(not p.strip() for p in parts):
        return None
    return parts[0], (parts[1].strip() if len(parts) == 2 else None)


def words(model_id: str) -> list[str]:
    """An ID's words: `/` and `-` both separate them."""
    return model_id.replace("/", "-").split("-")


def version_of(model_id: str) -> tuple[int, ...]:
    """The first word that is a version like `6`, `6.1`, `v4.1`, or attached `qwen2.5` / `glm4`, as a tuple; () when none.

    Only the first one counts, so a later date suffix (`-0731`) never outranks the version.
    """
    for word in words(model_id):
        if not word:
            continue
        parts = word.removeprefix("v").split(".")
        if all(part.isdigit() for part in parts):
            return tuple(int(part) for part in parts)
        if not word[0].isdigit():
            match = re.search(r"(\d+(?:\.\d+)*)$", word)
            if match:
                return tuple(int(part) for part in match.group(1).split("."))
    return ()


def resolve(family_name: str, tier: str | None, families: dict[str, dict[str, Any]], available: list[str]) -> str:
    family = families[family_name]
    if tier is not None and not tier.strip():
        raise SelectorError(f"Unsupported selector {f'{family_name}:{tier}'!r}")
    tier = tier.strip() if tier is not None else (family.get("default_tier") or next(iter(family["tiers"])))
    if tier not in family["tiers"]:
        raise SelectorError(f"{family_name} has no tier {tier!r}; tiers: {', '.join(family['tiers'])}")
    value = family["tiers"][tier]
    candidates = [item.strip() for item in value.split("|")] if isinstance(value, str) else list(value)
    for candidate in candidates:
        if candidate in available:
            return candidate
    prefix = family.get("prefix")
    if not prefix:
        # Without a prefix a keyword would match across vendors, so tier values must be exact IDs.
        raise SelectorError(f"{candidates[0]} ({family_name}:{tier}) is not in the live {family['engine']} catalog")
    for candidate in candidates:
        keyword = f"-{'-'.join(words(candidate))}-"
        matches = [i for i in available if i.startswith(prefix) and keyword in f"-{'-'.join(words(i[len(prefix) :]))}-"]
        if matches:
            return max(matches, key=version_of)
    raise SelectorError(f"no {family_name}:{tier} model in the live {family['engine']} catalog")


def resolve_selector(selector: str, families: dict[str, dict[str, Any]], available: list[str]) -> str:
    parts = selector.split(":")
    if parts[0] == "latest" and len(parts) > 1:
        parts = parts[1:]
    if len(parts) > 2 or parts[0] not in families or (len(parts) == 2 and not parts[1].strip()):
        raise SelectorError(f"Unsupported selector {selector!r}")
    return resolve(parts[0], parts[1].strip() if len(parts) == 2 else None, families, available)


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
