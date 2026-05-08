"""Read-only HTMX dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=select_autoescape(["html"]))


async def render_html(*, deps: Any) -> str:
    recent = await deps.graphiti.search_facts(kind_prefix="fleet_dispatch", limit=20)
    return _env.get_template("dashboard.html").render(
        registry={"size": deps.registry.size(), "stale": deps.registry.is_stale()},
        circuits=deps.circuits.snapshot_all(),
        recent=recent,
    )


async def metrics_json(*, deps: Any) -> dict[str, Any]:
    recent = await deps.graphiti.search_facts(kind_prefix="fleet_dispatch", limit=200)
    completed = [f for f in recent if f.get("kind") == "fleet_dispatch_completed"]
    failed = [f for f in recent if f.get("kind") == "fleet_dispatch_failed"]
    return {
        "registry": {"size": deps.registry.size(), "stale": deps.registry.is_stale()},
        "circuits": deps.circuits.snapshot_all(),
        "dispatches_completed": len(completed),
        "dispatches_failed": len(failed),
    }
