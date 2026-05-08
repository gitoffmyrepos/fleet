---
name: fleet-plan
description: Use when starting a multi-step build/refactor that benefits from gsd's plan→execute→verify lifecycle.
---

# Fleet Plan

Call `mcp__fleet__dispatch_phase` to drive the gsd lifecycle.

## Arguments
- `task` (required): goal sentence
- `stage` (default plan): plan | execute | verify | discuss | ship

## Behavior
- `stage=plan` shells `claude /gsd:plan-phase <task>` and returns `{phase_dir, stage}`.
- `stage=execute` runs `/gsd:execute-phase` against the previously-created phase.
- `stage=verify` runs `/gsd:verify-work`.
- All stages live under the gsd circuit breaker.

## Typical flow
1. Plan → check `.planning/<phase>/PLAN.md`
2. Execute → wait for completion summary
3. Verify → read `VERIFICATION.md`
