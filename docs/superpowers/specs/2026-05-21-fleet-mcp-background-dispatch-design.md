# Fleet MCP Background Dispatch — Design Spec

**Date:** 2026-05-21
**Repo:** `/home/kelvin/SB-HomeLAb/fleet`
**Purpose:** Unblock long-running specialist dispatches by making them fire-and-forget by default. Solves the "MCP timeout while the subprocess actually completes" pattern that has blocked the FX training E2E audit.

## Problem

`_dispatch_subagent` (and variants `_dispatch_subagent_cheap`, `_dispatch_subagent_inherit`) in `src/fleet/tools.py` await `self._d.subagent.dispatch(...)`. That call in turn waits (`asyncio.wait_for`) for the spawned `claude --print` subprocess to finish — typical 200-2000 seconds for a real audit task.

The MCP HTTP request from a client (Claude Code) has a much shorter timeout (~60 s). Result: the client sees `The operation timed out.` while the dispatched subprocess KEEPS RUNNING server-side. The operator loses visibility into the in-flight work and cannot batch dispatch 30 agents in parallel without each call timing out.

Evidence from this session: dispatched ml-predictor L7 specialist timed out at MCP layer but actually completed (commit `e0c2b73a3`, 574 insertions / 280-line test suite) ~15 minutes later.

## Goal

Let callers opt into fire-and-forget semantics:
- Caller passes `run_in_background: true`
- MCP returns within ~1 second with `{task_id, status: "started"}`
- The subprocess + worktree + telemetry continue server-side
- Caller polls `mcp__fleet__status` (existing tool) to check completion

## Non-goal

- No change to the existing synchronous semantics (legacy callers continue to await results).
- No change to the dispatcher itself, only to how `tools.py` invokes it.
- No change to circuit breakers, telemetry, or worktree lifecycle.

## Design

### API surface (additive)

Add a single optional argument to each of these tools:

| Tool | New arg | Default | Semantic |
|---|---|---|---|
| `dispatch_subagent` | `run_in_background: bool` | `false` | When true, return immediately with `{task_id, status: "started"}` and continue dispatch in the background |
| `dispatch_subagent_cheap` | `run_in_background: bool` | `false` | Same |
| `dispatch_subagent_inherit` | `run_in_background: bool` | `false` | Same |

### Implementation pattern

```python
async def _dispatch_subagent(self, a: dict[str, Any]) -> dict[str, Any]:
    task = _require(a, "task")
    task_id = a.get("task_id") or _new_task_id()
    cwd = _resolve_cwd(a)
    isolation = a.get("isolation", "worktree")
    skill_kind = a.get("route_kind") or "subagent"
    skill_limit = int(a.get("skill_limit", 15))
    skill_payload = await self._build_skill_payload(skill_kind, skill_limit)

    coro = self._d.subagent.dispatch(
        task_id=task_id,
        task=task,
        agent_hint=a.get("agent_hint"),
        cwd=cwd,
        auto_commit=bool(a.get("auto_commit", True)),
        isolation=isolation,
        skill_header=skill_payload["header"],
        skill_roots=skill_payload["roots"],
    )

    if bool(a.get("run_in_background", False)):
        asyncio.create_task(coro)
        return {
            "task_id": task_id,
            "status": "started",
            "background": True,
            "note": "Poll mcp__fleet__status for completion.",
        }

    result = await coro
    return _result_dict(task_id, result)
```

### Why `asyncio.create_task` is safe here

- The Fleet MCP server is a long-running FastAPI process; spawned tasks run within its event loop, not tied to the HTTP request lifecycle.
- Telemetry events are written via `self._t` (Graphiti / file-backed log) independent of the HTTP response — operator can replay via `mcp__fleet__telemetry` or `mcp__fleet__explain`.
- Subprocess cleanup runs in `dispatcher/base.py:dispatch` regardless of whether the HTTP request awaits.
- The same fire-and-forget pattern is already used by the autonomous-trading reactor (`asyncio.create_task` after SSE done event) — proven idempotent under outage.

### Error handling

- If the background coroutine raises before `asyncio.create_task` schedules it (e.g., parameter validation error), the HTTP response would return the exception synchronously. **Mitigation:** validate parameters BEFORE scheduling.
- If the dispatch fails after backgrounding (subprocess fails, circuit opens), telemetry records `fleet_dispatch_failed`. Operator sees it via `mcp__fleet__status`.
- Unhandled exceptions in the background task would be silently swallowed by asyncio. **Mitigation:** wrap the coro in a small `try/except` that calls `self._t.failure(...)` on any unhandled exception.

### Telemetry compatibility

The dispatcher's `_t.start(...)` and `_t.success/failure(...)` events fire identically — the caller can correlate via `task_id`. No schema changes needed.

## Testing strategy

1. **Unit test** (`tests/test_tools_background.py`): mock `self._d.subagent.dispatch` to return a sleep-1 coroutine. Verify `_dispatch_subagent({"task": "...", "run_in_background": True})` returns within 100ms with `status="started"`. Verify telemetry `fleet_dispatch_started` was emitted before return.
2. **Integration smoke** (manual via MCP client): call `mcp__fleet__dispatch_subagent` with a 5-second sleep task and `run_in_background=true`. Verify response in <1s. Poll `mcp__fleet__status` after 6s and confirm a `fleet_dispatch_completed` entry exists.
3. **Backward-compat regression**: existing call shapes (no `run_in_background`) continue to block until completion. Pin via existing tests.

## Rollout

1. Land the change on `main` of `/home/kelvin/SB-HomeLAb/fleet`.
2. Rebuild + restart the Fleet MCP daemon (`uv run python -m fleet`).
3. Smoke-test from this session.
4. Resume batched FX audit dispatches.

## Risk

- **Low.** Strictly additive. Default behavior is unchanged. Operators must explicitly opt in.
- **Cleanup edge case:** if Fleet server restarts mid-dispatch, the background task dies. Already handled by existing `worktree_retained` cleanup at next startup.

## Success criterion

Re-issuing the 10-prompt batched dispatch from earlier this session with `run_in_background=true` returns all 10 responses within 10 seconds total, and the dispatched subprocesses run to completion in parallel.
