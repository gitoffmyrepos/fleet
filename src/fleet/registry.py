"""Union agent registry across ruflo / superpowers / claude / gsd."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dc_fields
from pathlib import Path

from .graphiti_client import GraphitiClient

_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---", re.DOTALL)
_TAG_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = {
    "the",
    "and",
    "for",
    "with",
    "use",
    "uses",
    "used",
    "this",
    "that",
    "your",
    "you",
    "are",
    "agent",
    "specialist",
    "expert",
    "review",
    "from",
    "into",
    "about",
    "after",
    "before",
    "when",
    "where",
    "what",
}


def namespace_id(source: str, name: str) -> str:
    return f"{source}:{name}"


@dataclass(frozen=True)
class AgentDef:
    id: str
    name: str
    source: str
    description: str
    path: str
    model: str | None
    tools: list[str] = field(default_factory=list)

    def role_tags(self) -> set[str]:
        text = f"{self.name} {self.description}".lower()
        return {t for t in _TAG_TOKEN_RE.findall(text) if t not in _STOP}


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
    return fields


def _read_def(path: Path, source: str) -> AgentDef | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = _parse_frontmatter(text)
    name = fm.get("name") or path.stem
    if path.name == "SKILL.md":
        name = fm.get("name") or path.parent.name
    return AgentDef(
        id=namespace_id(source, name),
        name=name,
        source=source,
        description=fm.get("description", "")[:400],
        path=str(path),
        model=fm.get("model") or None,
        tools=[t.strip() for t in fm.get("tools", "").split(",") if t.strip()],
    )


def scan_directory(root: Path | str, *, source: str, pattern: str) -> list[AgentDef]:
    root = Path(root)
    if not root.exists():
        return []
    out: list[AgentDef] = []
    for p in sorted(root.glob(pattern)):
        if p.is_file():
            d = _read_def(p, source)
            if d is not None:
                out.append(d)
    return out


_MODEL_RANK: dict[str | None, int] = {"haiku": 1, "sonnet": 2, "opus": 3, None: 2}


@dataclass
class RegistryConfig:
    sources: list[dict[str, str]]


class Registry:
    def __init__(self, cfg: RegistryConfig, *, graphiti: GraphitiClient) -> None:
        self._cfg = cfg
        self._g = graphiti
        self._index: dict[str, AgentDef] = {}
        self._stale: bool = False

    def size(self) -> int:
        return len(self._index)

    def is_stale(self) -> bool:
        return self._stale

    def get(self, agent_id: str) -> AgentDef | None:
        return self._index.get(agent_id)

    def all(self) -> list[AgentDef]:
        return list(self._index.values())

    async def load(self) -> None:
        defs: list[AgentDef] = []
        any_source_loaded = False
        for src in self._cfg.sources:
            ds = scan_directory(Path(src["root"]), source=src["name"], pattern=src["pattern"])
            if ds:
                any_source_loaded = True
            defs.extend(ds)
        if not any_source_loaded:
            self._stale = True
            facts = await self._g.search_facts(kind_prefix="fleet_registry_snapshot", limit=1)
            if facts:
                snap = (facts[0].get("body") or {}).get("agents", [])
                valid_fields = {f.name for f in dc_fields(AgentDef)}
                defs = [AgentDef(**{k: v for k, v in a.items() if k in valid_fields}) for a in snap]
        else:
            self._stale = False
            await self._save_snapshot(defs)
        self._index = {d.id: d for d in defs}

    async def _save_snapshot(self, defs: list[AgentDef]) -> None:
        # TODO(refresh-loop): when load() runs periodically, hash-dedup against
        # the newest existing snapshot to avoid unbounded episode churn.
        await self._g.add_episode(
            kind="fleet_registry_snapshot",
            parent_task_id=None,
            body={"agents": [asdict(d) for d in defs]},
        )

    def score_for(self, *, task: str, limit: int = 5) -> list[AgentDef]:
        tokens = {t for t in _TAG_TOKEN_RE.findall(task.lower()) if t not in _STOP}
        ranked: list[tuple[int, int, AgentDef]] = []
        for d in self._index.values():
            tags = d.role_tags()
            score = len(tokens & tags)
            cheapness = -_MODEL_RANK.get((d.model or "").lower() or None, 2)
            ranked.append((score, cheapness, d))
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2].id))
        # NB: zero-score agents are intentionally returned (up to `limit`) so
        # callers always get a best-effort suggestion when no role-tag match.
        return [d for _s, _c, d in ranked[:limit]]
