---
name: fleet-swarm
description: Use when fanning out parallel work across many similar items via Fleet. Wraps claude-flow swarms with circuit-protected dispatch and result caching.
---

# Fleet Swarm

Call `mcp__fleet__dispatch_swarm` for fan-out tasks.

## Arguments
- `task` (required): the goal sentence
- `agents` (default 20): swarm size, max 60
- `topology` (default parallel): parallel | hive-mind | mesh | hierarchical
- `strategy` (default development): development | analysis | devops | documentation
- `scope_paths` (optional): list of dirs that scope the task — used in cache hash

## Behavior
- First lookup: hash(task + scope_paths) checked against the Fleet cache. Hit returns instantly.
- Miss: shells `claude-flow swarm start` (or `hive-mind spawn`) under the ruflo circuit breaker.
- Failure (non-zero exit, timeout, or open circuit) returns `{ok: false, error, recovery_hint}`.

## When NOT to use
- Single-step Q&A → use `dispatch_subagent`
- Multi-step build → use `dispatch_phase`
