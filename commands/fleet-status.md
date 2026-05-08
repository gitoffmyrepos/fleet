---
description: Show recent Fleet dispatches and circuit state.
argument-hint: [--limit N] [--hung]
---

Call `mcp__fleet__status` with optional `limit`. If `--hung` is passed, filter for dispatches with no telemetry update in >5min and suggest cancel commands for each.
