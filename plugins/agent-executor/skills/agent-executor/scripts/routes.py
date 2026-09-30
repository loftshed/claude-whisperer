"""Role-based routes: which engine, model and effort a role (implementation, consultation, ...) uses.

Defaults live in references/routes.default.json; the user's ~/.config/agent-executor/config.json (or
$AGENT_EXECUTOR_CONFIG) overrides any route. The code holds no model IDs of its own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "references" / "routes.default.json"
ENGINE_DEFAULT_ORDER = ("implementation", "gemini", "claude", "opencode", "consultation")


def config_path() -> Path:
    explicit = os.environ.get("AGENT_EXECUTOR_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".config") / "agent-executor" / "config.json"


def load_config() -> dict[str, Any]:
    path = config_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"{path}: invalid JSON: {error}") from error


def load_routes() -> dict[str, dict[str, Any]]:
    routes = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))["routes"]
    for role, route in (load_config().get("routes") or {}).items():
        if not isinstance(route, dict) or not route.get("engine"):
            raise ValueError(f"{config_path()}: route {role!r} needs at least an engine")
        routes[role] = dict(route)
    return routes


def engine_default(engine: str, routes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The route an engine uses when only --engine is given: the first role, in a fixed order, on it."""
    routes = routes if routes is not None else load_routes()
    ordered = [*ENGINE_DEFAULT_ORDER, *sorted(set(routes) - set(ENGINE_DEFAULT_ORDER))]
    return next((routes[role] for role in ordered if role in routes and routes[role].get("engine") == engine), None)


def starter_config() -> dict[str, Any]:
    defaults = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    return {"schema": "agent-executor.config.v1", "routes": defaults["routes"]}
