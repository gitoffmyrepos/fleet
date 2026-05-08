"""Drive each host (claude / goose / openclaw) by subprocess to issue an MCP tool call."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


class HostUnavailable(RuntimeError): ...


def _require(binary: str) -> str:
    p = shutil.which(binary)
    if not p:
        raise HostUnavailable(f"{binary} not on PATH")
    return p


def _extract_json(s: str) -> str:
    i = s.find("{")
    j = s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError(f"no json found in: {s[:200]}")
    return s[i : j + 1]


def via_claude(task: str, fleet_url: str, bearer: str) -> dict[str, Any]:
    bin_ = _require("claude")
    prompt = (
        f"Call mcp__fleet__route with task='{task}'. "
        f"Then print JSON {{task_id, kind, confidence}} only."
    )
    out = subprocess.run(
        [bin_, "--print", "--output-format", "text", prompt],
        env={**os.environ, "FLEET_URL": fleet_url, "FLEET_BEARER": bearer},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(_extract_json(out.stdout))


def via_goose(task: str, fleet_url: str, bearer: str) -> dict[str, Any]:
    bin_ = _require("goose")
    body = {"name": "route", "arguments": {"task": task}}
    out = subprocess.run(
        [
            bin_,
            "run",
            "-t",
            json.dumps({"mcp_call": body, "fleet_url": fleet_url, "bearer": bearer}),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(_extract_json(out.stdout))


def via_openclaw(task: str, fleet_url: str, bearer: str) -> dict[str, Any]:
    bin_ = _require("openclaw")
    out = subprocess.run(
        [bin_, "exec", f'/fleet "{task}"'],
        env={**os.environ, "FLEET_URL": fleet_url, "FLEET_BEARER": bearer},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(_extract_json(out.stdout))
