---
description: Smart-route a task through Fleet — auto-classifies into swarm/phase/subagent/verify/ship.
argument-hint: <task description>
---

# /fleet

Call `mcp__fleet__route` with `{"task": "$ARGUMENTS"}`. Inspect the returned `kind` and call the matching dispatch tool. Honor `suggested_agents` / `suggested_topology` unless the user has overridden them.

If `degraded=true`, fall back to your own judgment and note that the LLM router was unavailable.
