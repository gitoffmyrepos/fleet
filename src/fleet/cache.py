"""Hash-keyed result memoization backed by Graphiti episodes."""

from __future__ import annotations

import hashlib
import re

_WS_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    return _WS_RE.sub(" ", s.strip().lower())


def task_hash(*, task: str, scope_paths: list[str]) -> str:
    canon = _normalize(task) + "\n" + "\n".join(sorted(_normalize(p) for p in scope_paths))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
