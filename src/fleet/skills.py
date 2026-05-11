"""Unified skills catalog discovery for Fleet dispatchers.

Reads:
  - ~/.hermes/skills/.hub/index.json (if present) — fastest path
  - ~/.hermes/skills/   (fallback: walk SKILL.md files)
  - ~/.claude/skills/   (the user's local Claude skills)
  - ~/.claude/plugins/marketplaces/*/skills/  (installed marketplaces)

Exposed via the `list_skills` Fleet MCP tool and via the dispatcher skill-
header injection so Fleet-spawned `claude --print` subagents see the same
skills the parent has, with --add-dir permission to load them on demand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HOME = Path.home()
ROOTS = [
    _HOME / ".hermes/skills",
    _HOME / ".claude/skills",
]
MARKETPLACE_GLOB = _HOME / ".claude/plugins/marketplaces"
INDEX_HINT = _HOME / ".hermes/skills/.hub/index.json"

_CACHE: dict[str, Any] = {"ts": 0.0, "catalog": None}
_TTL = 60.0  # 60s cache — cheap; we re-read at most once a minute


async def load_catalog() -> dict[str, Any]:
    """Load (and cache for 60s) the unified skill catalog.

    Returns:
        {
          "skills": [
            {name, description, tags, mcp_servers, category, path, root}, ...
          ],
          "roots": [str, ...]   # filesystem roots — pass to --add-dir
        }
    """
    now = time.time()
    if _CACHE["catalog"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["catalog"]
    # Walk filesystem in a thread to keep the asyncio loop responsive on
    # slow disks. Catalog build is pure I/O.
    catalog = await asyncio.to_thread(_build_catalog_sync)
    _CACHE["catalog"] = catalog
    _CACHE["ts"] = now
    return catalog


def invalidate_cache() -> None:
    """Force the next load_catalog() to rebuild from disk."""
    _CACHE["catalog"] = None
    _CACHE["ts"] = 0.0


def _build_catalog_sync() -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    roots: list[str] = []

    # Prefer Hermes hub index if fresh (Hermes assessment 2026-05-10: this
    # is the "biggest win" cache and may not exist yet on this host).
    if INDEX_HINT.exists():
        try:
            data = json.loads(INDEX_HINT.read_text(encoding="utf-8"))
            for s in data.get("skills", []) or []:
                name = str(s.get("name") or "").strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                skills.append(
                    {
                        "name": name[:64],
                        "description": str(s.get("description") or "")[:300],
                        "tags": s.get("tags") or [],
                        "mcp_servers": s.get("mcp_servers") or [],
                        "category": str(s.get("category") or "hub"),
                        "path": str(s.get("path") or ""),
                        "root": str(INDEX_HINT.parent.parent),
                        "source": "hermes-hub-index",
                    }
                )
        except Exception as e:
            logger.debug("hermes hub index unreadable: %s", e)

    # Walk on-disk roots
    for root in ROOTS:
        if not root.exists():
            continue
        roots.append(str(root))
        for skill_md in root.rglob("SKILL.md"):
            # Hub bookkeeping + quarantine/archive dirs are not real skills.
            if any(p in (".hub", ".archive", ".quarantine") for p in skill_md.parts):
                continue
            meta = _parse_skill_md(skill_md, root, source="walk")
            if not meta:
                continue
            key = meta["name"].lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            skills.append(meta)

    # Marketplace roots — these are version-pinned dirs, take only the
    # newest. (We surface all SKILL.md found; marketplaces themselves
    # already version-symlink to current.)
    if MARKETPLACE_GLOB.exists():
        for mp_skills_dir in MARKETPLACE_GLOB.glob("*/skills"):
            roots.append(str(mp_skills_dir))
            for skill_md in mp_skills_dir.rglob("SKILL.md"):
                meta = _parse_skill_md(skill_md, mp_skills_dir, source="marketplace")
                if not meta:
                    continue
                key = meta["name"].lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                skills.append(meta)

    return {"skills": skills, "roots": roots}


def _parse_skill_md(skill_md: Path, root: Path, *, source: str) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a SKILL.md. Returns None on parse fail."""
    try:
        head = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    parts = head.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        # Avoid PyYAML dependency at import time: tiny hand parser that
        # handles the keys we care about. Full YAML for unknown keys is
        # left to ~/.hermes/.hub/index.json if it exists.
        fm = _parse_simple_yaml(parts[1])
    except Exception:
        return None
    name = str(fm.get("name") or skill_md.parent.name)[:64].strip()
    if not name:
        return None
    try:
        category = str(skill_md.relative_to(root).parent).split("/")[0]
    except ValueError:
        category = "unknown"
    return {
        "name": name,
        "description": str(fm.get("description") or "")[:300].strip(),
        "tags": fm.get("tags") or [],
        "mcp_servers": fm.get("mcp_servers") or [],
        "category": category,
        "path": str(skill_md),
        "root": str(root),
        "source": source,
    }


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Very small YAML parser — handles {name, description, tags, mcp_servers}
    in the shapes we see in SKILL.md frontmatter. Anything fancy → empty val.
    Avoids a PyYAML dependency at the fleet package level (PyYAML lives in
    hermes; fleet stays minimal)."""
    out: dict[str, Any] = {}
    current_key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(" ") or line.startswith("\t"):
            stripped = line.strip()
            if current_key and stripped.startswith("- "):
                # list item under current_key
                out.setdefault(current_key, [])
                if isinstance(out[current_key], list):
                    out[current_key].append(stripped[2:].strip().strip("'\""))
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                current_key = k
                continue
            if v.startswith("[") and v.endswith("]"):
                items = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                out[k] = items
            else:
                out[k] = v.strip("'\"")
            current_key = None
    return out


# Heuristic tag-bucket matching by dispatched task kind.
_KIND_TAGS: dict[str, set[str]] = {
    "swarm": {"parallel", "bulk", "fan-out", "scan", "audit", "survey", "review"},
    "phase": {"plan", "execute", "feature", "refactor", "tdd", "implementation"},
    "verify": {"verify", "test", "qa", "regression", "lint", "check"},
    "ship": {"deploy", "release", "merge", "ship", "ci", "rollback"},
    "subagent": {"explain", "summarise", "summarize", "describe", "docs", "investigate"},
}


def filter_skills(
    catalog: dict[str, Any],
    *,
    kind: str | None = None,
    tag: str | None = None,
    mcp: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter the unified catalog by kind/tag/mcp.

    Empty filters return up to `limit` skills in catalog order — useful for
    a general "what's available" listing. `kind` matches against the
    skill's tags AND its name+description (substring) to forgive skills
    that haven't tagged themselves correctly yet.
    """
    out: list[dict[str, Any]] = []
    kind_tags = _KIND_TAGS.get(kind or "", set())
    for s in catalog.get("skills", []):
        if tag and tag not in (s.get("tags") or []):
            continue
        if mcp and mcp not in (s.get("mcp_servers") or []):
            continue
        if kind_tags:
            text = (s.get("name", "") + " " + s.get("description", "")).lower()
            tags_lower = {str(t).lower() for t in s.get("tags", [])}
            if not (tags_lower & kind_tags) and not any(k in text for k in kind_tags):
                continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def render_prompt_header(skills: list[dict[str, Any]]) -> str:
    """Render a compact skill list for injection into a subagent prompt."""
    if not skills:
        return ""
    lines = "\n".join(
        f"- {s['name']}: {(s.get('description') or '').strip()[:120]}" for s in skills
    )
    return (
        "Skills available for this task (invoke via the Skill tool with the "
        "matching `name=`):\n"
        f"{lines}\n\n"
        "Full SKILL.md files are readable under the skills roots passed via "
        "--add-dir; use Read/Glob/Grep to inspect them.\n\n"
    )
