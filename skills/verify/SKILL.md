---
name: fleet-verify
description: Use when validating that a feature/fix actually works. Wraps superpowers verification-before-completion via Fleet.
---

# Fleet Verify

Call `mcp__fleet__dispatch_verify` with `{task, scope?}`. Returns `{verdict: PASS|FAIL|UNKNOWN, summary}`.

The verify dispatcher invokes `claude` with the verification-before-completion skill prompt. Failures land under the superpowers circuit breaker.

## When to use
- After a phase completes, before claiming done
- After a swarm reports success, to gate-check
- Before `/fleet ship`
