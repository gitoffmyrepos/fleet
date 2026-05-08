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
- Logs: `kubectl -n memory-stack logs deploy/fleet -f`

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
