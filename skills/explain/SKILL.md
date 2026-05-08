---
name: fleet-explain
description: Use when the user asks "why did Fleet do X" or "what was the reasoning chain". Returns the citation chain over Graphiti episodes.
---

# Fleet Explain

Call `mcp__fleet__explain` with `{task_id}`. Returns the ordered chain: route_decision → registry_score → dispatch_started → tool events → dispatch_completed/failed.

## When to use
- Operator asks for an audit trail of a past dispatch
- Investigating a failed task
- Composing a postmortem
