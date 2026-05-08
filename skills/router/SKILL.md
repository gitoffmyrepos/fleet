---
name: fleet-router
description: Use when starting any task with parallelizable, multi-step, or verification-worthy structure. Calls Fleet to classify the task and dispatch instead of spawning agents directly.
---

# Fleet Router

Before spawning your own subagent or dispatching a swarm, call `mcp__fleet__route` first.

## When to use this skill

Activate when the task description matches any of:
- Mentions "all N", "every N", or fan-out work ("audit", "scan", "survey")
- Spans plan + code + verify (multi-step build / refactor / feature)
- Asks "verify / validate / does this work"
- Asks to ship / release / merge / deploy

## How to use

1. Call `mcp__fleet__route` with `{"task": "<the user's task>"}`.
2. Read the returned `{kind, confidence, reason, suggested_agents, suggested_topology}`.
3. Call the matching dispatch tool:
   - `kind=swarm` → `mcp__fleet__dispatch_swarm`
   - `kind=phase` → `mcp__fleet__dispatch_phase`
   - `kind=subagent` → `mcp__fleet__dispatch_subagent`
   - `kind=verify` → `mcp__fleet__dispatch_verify`
   - `kind=ship` → `mcp__fleet__ship`
4. If `degraded=true`, the LLM was unreachable — fall back to your own judgment but log a note.
5. Override the suggestion freely (you may swarm with 40 agents instead of 20).

## Example

User: "audit the health of all 73 microservices"
- Call `route` → `{kind: "swarm", confidence: 0.92, suggested_agents: 20}`
- Call `dispatch_swarm` with `agents=20, topology=parallel`
- Return the summary; if `cache_hit=true`, the answer was free.
