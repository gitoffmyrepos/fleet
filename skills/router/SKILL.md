---
name: fleet-router
description: Use when starting any task with parallelizable, multi-step, or verification-worthy structure. Calls Fleet to classify the task and dispatch instead of spawning agents directly. ALWAYS pass defer_to_caller=true — your own LLM context is the right place to make ambiguous routing decisions.
---

# Fleet Router

Before spawning your own subagent or dispatching a swarm, call `mcp__fleet__route` first. **Always pass `defer_to_caller=true`** — Fleet should not burn a server-side LLM call when you (the calling harness) already have an LLM context.

## When to use this skill

Activate when the task description matches any of:
- Mentions "all N", "every N", or fan-out work ("audit", "scan", "survey")
- Spans plan + code + verify (multi-step build / refactor / feature)
- Asks "verify / validate / does this work"
- Asks to ship / release / merge / deploy

## How to use

1. Call `mcp__fleet__route` with `{"task": "<the user's task>", "defer_to_caller": true}`.
2. Read the returned decision:
   - `kind` — one of `swarm | phase | subagent | verify | ship`
   - `confidence` — 0.0 to 0.95
   - `requires_caller_classification` — boolean (NEW)
3. **If `requires_caller_classification=true`**: the heuristic gate was unsure. **You** classify the task using your own LLM context. Pick one of `{swarm, phase, subagent, verify, ship}` based on task semantics, then call the matching dispatch tool. Do not call route again.
4. **If `requires_caller_classification=false`**: the heuristic was confident. Use the returned `kind` directly.
5. Call the matching dispatch tool:
   - `kind=swarm` → `mcp__fleet__dispatch_swarm`
   - `kind=phase` → `mcp__fleet__dispatch_phase`
   - `kind=subagent` → `mcp__fleet__dispatch_subagent`
   - `kind=verify` → `mcp__fleet__dispatch_verify`
   - `kind=ship` → `mcp__fleet__ship`
6. Override the suggestion freely (you may swarm with 40 agents instead of 20).

## Why `defer_to_caller`

You are an LLM-driven harness (Claude Code / OpenClaw / Goose). Your session already has full task context. Asking Fleet to make a separate LLM call for ambiguous classification:
- Burns extra tokens on the same problem you can solve in-context
- Forces Fleet to maintain its own LLM credentials (a security and ops burden)
- Loses the conversation context that you have

By passing `defer_to_caller=true`, Fleet returns its heuristic best-guess plus a flag saying "you decide." You then classify using one cheap thought and proceed.

## Example (high-confidence heuristic — no caller decision needed)

User: "audit the health of all 73 microservices"
- Call `route(task=..., defer_to_caller=true)` → `{kind: "swarm", confidence: 0.95, requires_caller_classification: false}`
- Call `dispatch_swarm(agents=20, topology="parallel")`
- Return summary; if `cache_hit=true`, the answer was free.

## Example (ambiguous — caller classifies)

User: "explain how the L18 anomaly detector works"
- Call `route(task=..., defer_to_caller=true)` → `{kind: "subagent", confidence: 0.30, requires_caller_classification: true, reason: "low confidence — caller LLM should classify"}`
- You think: "explain X" is a Q&A on existing code → `subagent`. Confirmed.
- Call `dispatch_subagent(task="explain how the L18 anomaly detector works")`.
