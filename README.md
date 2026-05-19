# Fleet

Unified meta-orchestrator wrapping ruflo (claude-flow swarms), superpowers (workflow skills), and gsd (project lifecycle) behind one MCP service + skill bundle + slash commands.

- One command: `/fleet "<task>"` — auto-classifies into swarm / phase / subagent / verify / ship
- Cross-host parity: the same MCP serves Claude Code, OpenClaw, Goose
- Token reduction: 14 curated tools (vs ~150 upstream), result caching, ≤2k-token returns
- Observability: every dispatch is a Graphiti episode; `/fleet explain` returns the chain

## Quick start

```bash
uv sync --all-extras
make test
make cov            # ≥80% overall, ≥90% router + registry
make build          # docker image
bash scripts/install-skills.sh
bash scripts/install-commands.sh
```

## Deploy to k8s

See [docs/RUNBOOK.md](docs/RUNBOOK.md). TL;DR:

```bash
helm upgrade --install fleet deploy/helm -n memory-stack
make smoke
```

## Architecture

See [docs/SPEC.md](docs/SPEC.md). Streamable HTTP MCP, FastAPI, NodePort 30801. 18 source files, 149 unit tests, 9 integration tests, 97.57% coverage.

## Branch lifecycle + reconciler (2026-05-19)

Every dispatch now runs inside an isolated git worktree (`/tmp/fleet-worktrees/<task_id>`) on a per-dispatch branch (`fleet/<task_id>`) created off `origin/master`; on success the branch is rebased + pushed and torn down, on failure it's torn down without merge. A daily reconciler (`fleet-reconciler` or the `fleet-reconciler` CronJob) detects orphans the per-dispatch teardown may have missed (crashes, kills, network glitches) and runs by default in dry-run, writing a JSON report to `/tmp/fleet-reconciler-report.json`. Pass `--apply` to delete MERGED orphans, plus `--stale-apply` to additionally delete STALE (>7d) ones. Prometheus metrics (`fleet_dispatches_*`, `fleet_branches_*`, `fleet_worktrees_active`, `fleet_dispatch_duration_seconds`) are exported on `/metrics`. See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the cleanup runbook.
