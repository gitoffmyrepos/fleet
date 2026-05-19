# Fleet Runbook

## Deploy

1. Ensure Vault paths exist:
   ```
   vault kv put secret/fleet/mcp_bearer token="$(openssl rand -hex 32)"
   vault kv put secret/fleet/graphiti_bearer token="<paste from graphiti>"
   vault kv put secret/fleet/anthropic_api_key key="<sk-ant-...>"
   ```
2. Build and push image:
   ```
   docker build -t harbor.strategybase.local/fleet/fleet:<tag> .
   docker push harbor.strategybase.local/fleet/fleet:<tag>
   ```
3. Helm upgrade:
   ```
   helm upgrade --install fleet deploy/helm -n memory-stack \
     --set image.tag=<tag>
   kubectl -n memory-stack rollout status deploy/fleet --timeout=120s
   ```
4. Smoke:
   ```
   FLEET_URL=http://192.168.119.117:30801 \
     FLEET_BEARER=$(vault kv get -field=token secret/fleet/mcp_bearer) \
     make smoke
   ```

## Rollback

```
helm history fleet -n memory-stack
helm rollback fleet <revision> -n memory-stack
```

## Hung-task recovery

A task is "hung" if it has no `fleet_dispatch_*` episode update in >5min:

```
/fleet:status --hung
```

Cancel a specific task:
```
mcp__fleet__cancel({"task_id": "task_..."})
```

If the dispatcher subprocess is wedged at the OS level, restart the pod:
```
kubectl -n memory-stack rollout restart deploy/fleet
```

## Manual circuit close

If a circuit is open and you've fixed the underlying upstream issue:

```
mcp__fleet__circuit_close({"name": "ruflo"})
```

(Names: `ruflo`, `superpowers`, `gsd`.)

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| 401 from MCP | bearer mismatch | re-fetch from `secret/fleet/mcp_bearer` |
| 5xx on /mcp/tools/call | dependency crashed | check pod logs; rollout restart |
| Cache always misses | Graphiti unreachable | verify `FLEET_GRAPHITI_URL` + ExternalSecret |
| Registry stale | scan path missing on pod | confirm host mounts (none expected; rebuild image with current snapshot) |

## Observability

- Health: `curl http://192.168.119.117:30801/health`
- Dashboard: `http://192.168.119.117:30801/dashboard`
- Metrics JSON: `http://192.168.119.117:30801/dashboard/metrics.json`
- Prometheus metrics: `curl http://192.168.119.117:30801/metrics`
- Logs: `kubectl -n memory-stack logs deploy/fleet -f`

### Prometheus metrics (2026-05-19, A2)

The `/metrics` endpoint exposes:

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `fleet_dispatches_total` | counter | `kind` | per-kind dispatch counter |
| `fleet_dispatches_succeeded_total` | counter | — | `ok=True` dispatches |
| `fleet_dispatches_failed_total` | counter | `reason` | failure reason taxonomy |
| `fleet_branches_created_total` | counter | — | per-dispatch branches created |
| `fleet_branches_merged_total` | counter | — | branches merged back to master |
| `fleet_branches_orphaned_total` | counter | — | orphans found by the reconciler |
| `fleet_worktrees_active` | gauge | — | current Fleet-managed worktree count |
| `fleet_dispatch_duration_seconds` | histogram | `kind` | end-to-end wall-clock |

If `prometheus_client` is missing the endpoint returns a single-line
`# fleet metrics disabled` comment instead of failing — the rest of the
service stays up.

## Orphan cleanup (2026-05-19, A2)

Per-dispatch teardown (Agent A1) handles the happy path. The reconciler
is the safety net for everything else — crashes, kills, network glitches
that left a worktree+branch dangling.

### Run on demand

```bash
# Dry-run (default). Writes /tmp/fleet-reconciler-report.json.
fleet-reconciler

# Actually delete MERGED worktrees.
fleet-reconciler --apply

# Also delete STALE (>7d) worktrees. ALWAYS review the dry-run report first.
fleet-reconciler --apply --stale-apply

# Custom repos / threshold:
fleet-reconciler --repo /path/to/repo --stale-days 14
```

Environment variables (override defaults):

- `FLEET_RECONCILER_REPOS` — colon-separated repo list. Defaults to
  FX + sb-gitops + sb-dev-infra.
- `FLEET_RECONCILER_STATE_FILE` — Agent A1's active-dispatches state file.
  Default `~/.local/state/fleet/active_dispatches.json`.
- `FLEET_RECONCILER_REPORT_PATH` — Default `/tmp/fleet-reconciler-report.json`.

### Verdict taxonomy

Each Fleet-managed worktree is classified into one of four verdicts:

| Verdict | Action (`--apply`) | Action (`--apply --stale-apply`) |
|---|---|---|
| `active` (in state file) | skipped | skipped |
| `merged` (ancestor of `origin/master`) | deleted | deleted |
| `stale` (last commit ≥7d, not merged) | none | deleted |
| `unknown` (detached / can't determine) | skipped | skipped |

The reconciler is **fail-safe**: any state it can't determine is left
alone. The dry-run report flags STALE and UNKNOWN rows so an operator
can investigate before opting into deletion.

### What to look for in the report

The JSON report at `/tmp/fleet-reconciler-report.json` (or the
`--report-path` override) carries:

- `state_file_present` — `false` here means A1's lifecycle never ran
  or the state file got wiped; without it the reconciler can't tell
  ACTIVE worktrees apart from orphans. Investigate before passing
  `--apply`.
- `summary.errors` — non-zero means git operations failed during
  cleanup. Check `worktrees[].error` for the offending paths.
- `summary.unknown` — worktrees the reconciler refused to classify
  (detached HEAD, no master ref, etc.). Manual review needed; these
  are NEVER deleted automatically.

### Daily CronJob

The Helm chart includes a daily CronJob at 05:00 UTC running in
dry-run mode. Enable / configure via:

```yaml
# values.yaml
reconciler:
  enabled: true
  schedule: "0 5 * * *"
  apply: false          # set true to delete MERGED orphans
  staleApply: false     # set true to also delete STALE orphans
  staleDays: 7
```

A standalone manifest (no Helm) lives at
[`deploy/cronjob-reconciler.yaml`](../deploy/cronjob-reconciler.yaml).

## Upstream installations on the cluster pod

The k8s pod cannot shell out to host CLIs (`claude-flow`, `claude`). For dispatcher functionality, either:

- (v1) Set `FLEET_DRY_RUN=true` in the deployment env. The router and registry remain functional; dispatch tools return `dry_run` shaped responses.
- (v2 — see open issue) Bake the upstream CLIs into the fleet image, OR move dispatchers to a host-side companion process and have fleet-mcp talk to it via local socket.

For homelab use today, prefer running fleet-mcp **on the host** (uvicorn directly) when full dispatch is required. The Helm chart is for the API + dashboard surface.

## Security TODOs (Phase 14 hardening)

Tracked in code:
- `src/fleet/config.py` — convert `bearer_token`, `graphiti_bearer`, `anthropic_api_key` to `SecretStr`
- `src/fleet/graphiti_client.py` — exception messages currently embed up to 200 chars of upstream response body
- `src/fleet/registry.py` — `_save_snapshot` runs every `load()`; add hash-dedup before refresh-loop wiring
