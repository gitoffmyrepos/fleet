---
name: fleet-cost
description: Use when the user asks about token spend, dispatch counts, or cache hit-rate over a time window.
---

# Fleet Cost

Call `mcp__fleet__telemetry` with `{kind: "fleet_cost_query", body: {hours: N}}` to request a rollup, then read `/dashboard/metrics.json` for live numbers.

For v1, raw cost lives in Graphiti episodes; aggregation happens client-side or via `/dashboard`. v2 promotes this into a first-class endpoint.
