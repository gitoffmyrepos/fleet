---
description: Show Fleet cost / dispatch / cache metrics for the last N hours.
argument-hint: [--hours N]
---

Fetch `/dashboard/metrics.json` from the Fleet MCP service (or call `mcp__fleet__telemetry` for the rollup body). Render totals + by-kind breakdown.
