"""Union agent registry across ruflo / superpowers / claude / gsd."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

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
