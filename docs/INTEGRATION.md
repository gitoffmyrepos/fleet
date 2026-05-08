# Fleet Integration Status — Host Mode

## What's running

Fleet HTTP MCP server is **live** at `http://127.0.0.1:18001/mcp` (host mode, not k8s).

- Bearer token: `fleet-local-dev-token` (from `.env`, gitignored)
- Health: `curl http://127.0.0.1:18001/health` → `{"ok":true,"version":"0.1.0"}`
- Dashboard: `http://127.0.0.1:18001/dashboard`
- 14 MCP tools advertised via standard JSON-RPC at `POST /mcp`

## Verified end-to-end (all via MCP JSON-RPC)

| Tool | Status | Notes |
|---|---|---|
| `route` (high-conf) | ✅ | "audit all 73 microservices" → swarm conf 0.95 |
| `route` (low-conf) | ✅ | gracefully degrades to subagent + reason="llm not configured" |
| `dispatch_verify` | ✅ | real `claude --print` invocation, parsed verdict from output |
| `dispatch_subagent` | ✅ | real claude invocation, returned answer |
| `status` | ✅ | shows 3 pre-instantiated circuits (ruflo/superpowers/gsd, all closed) |
| `list_agents` | ✅ | 120 agents loaded from 4 source paths |
| `cache_lookup` | ✅ | deterministic hash, miss returns None |
| `register_agent` | ✅ | returns `accepted=false` (filesystem-driven in v1) |
| `telemetry` | ✅ | event ack |
| `cancel` | ✅ | logs intent |
| `circuit_close` | ✅ | known upstreams pre-registered, can close before any dispatch |
| `explain` | ⚠️ | Fleet calls succeed; Graphiti returns empty until backend pipeline catches up (see "Known issues") |

## Host integrations

All three hosts now have Fleet registered as an MCP server. Each picks it up on next start.

### 1. Claude Code (`~/.claude.json`)

Added to **both** the global `mcpServers` and the `/home/kelvin` project scope:

```json
"fleet": {
  "type": "http",
  "url": "http://127.0.0.1:18001/mcp",
  "headers": {"Authorization": "Bearer fleet-local-dev-token"}
}
```

Backup: `~/.claude.json.bak-pre-fleet`

**To activate:** restart Claude Code. Fleet's 14 tools will appear as `mcp__fleet__*` and the `/fleet:swarm`, `/fleet:plan` etc. slash commands (already installed at `~/.claude/commands/fleet/`) become live.

### 2. Goose (`~/.config/goose/config.yaml`)

Added under `extensions:`

```yaml
fleet:
  enabled: true
  type: streamable_http
  name: Fleet
  description: Unified meta-orchestrator — auto-routes tasks to ruflo swarms / gsd phases / superpowers verify / single subagents.
  uri: http://127.0.0.1:18001/mcp
  headers:
    Authorization: Bearer fleet-local-dev-token
  timeout: 1800
```

Backup: `~/.config/goose/config.yaml.bak-pre-fleet`

**To activate:** next `goose run` / `goose session start` picks it up.

Test: `goose run -t "use the fleet tool to route the task: audit all microservices"`

### 3. OpenClaw (`~/.openclaw/openclaw.json`)

Added under `mcp.servers`:

```json
"fleet": {
  "url": "http://127.0.0.1:18001/mcp",
  "headers": {
    "Accept": "application/json, text/event-stream",
    "Authorization": "Bearer fleet-local-dev-token"
  },
  "autoStart": true,
  "lazy": false,
  "alwaysOn": true,
  "transport": "streamable-http",
  "description": "Fleet meta-orchestrator: auto-routes tasks to ruflo swarms / gsd phases / superpowers verify / single subagents. PREFER over claude-flow direct."
}
```

Backup: `~/.openclaw/openclaw.json.bak-pre-fleet`

**To activate:** restart OpenClaw or reload its MCP servers.

## Restart Fleet (if it dies)

```bash
cd /home/kelvin/SB-HomeLAb/fleet
pkill -9 -f "python -m fleet" 2>/dev/null
sleep 2
nohup uv run python -m fleet > /tmp/fleet.log 2>&1 &
disown
```

Verify: `curl -sf http://127.0.0.1:18001/health`

## Resolved issues (live rollout findings)

### 1. Graphiti "node not found" — FIXED

**Root cause:** Fleet's `add_episode` was passing a `uuid` field to Graphiti's `add_memory`. Graphiti interprets that as "update existing node UUID" rather than "create with this UUID", so the queue worker logged `Failed to process episode X: node X not found` for every Fleet write.

**Fix:** Don't pass `uuid` to `add_memory`. Let Graphiti generate its own internal UUIDs. Fleet stores its correlation id (`fleet_id`) inside the JSON `episode_body` instead, so client-side filters in `search_facts`/`explain` still work.

**Verified:** writes now appear in `get_episodes` and surface through Fleet's `/status` and `/explain` endpoints.

**Operational caveat:** Graphiti's pipeline takes ~5 minutes per episode (OpenAI for entity extraction + Ollama embeddings + Neo4j writes). Live status/explain will lag by that window. For postmortem and audit this is fine; for real-time observability it's slow. Not a Fleet issue — that's how Graphiti is configured.

### 2. Anthropic API key — ARCHITECTURAL FIX (defer to caller)

**Root cause:** Fleet's router was calling its own LLM via Anthropic SDK for ambiguous task classification. That required a billable api03 key. The user's available keys were either $0-credit api03 or rate-limited Max-plan OAuth tokens that the standard SDK can't use.

**Fix:** Per the user's design intuition, the calling harness (Claude Code, OpenClaw, Goose+MiniMax) is itself an LLM with full context. New parameter `defer_to_caller=true` on `mcp__fleet__route` returns Fleet's heuristic best-guess plus `requires_caller_classification: true`. The harness then classifies in-context using its own LLM session and calls the matching dispatch tool directly. No server-side LLM call needed; no Fleet-side credentials required.

**Side benefit:** Fleet's router is now fully functional with no LLM credentials at all. The Fleet skill `~/.claude/skills/fleet/router/SKILL.md` was updated to instruct callers to always pass `defer_to_caller=true`.

**Non-LLM callers** (cron jobs, dashboards, scripts) leave `defer_to_caller` unset (default `false`) and still get Fleet's own LLM fallback if `FLEET_ANTHROPIC_API_KEY` is configured. If unconfigured, those callers see `via=fallback, reason="llm not configured", kind=subagent` — safe degraded behavior.

## Test commands

### Smoke from any shell

```bash
curl -sf -X POST http://127.0.0.1:18001/mcp \
  -H "authorization: Bearer fleet-local-dev-token" -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"route","arguments":{"task":"audit all 73 microservices"}}}' \
  | jq '.result.structuredContent.result'
```

Expected:
```json
{"task_id":"task_...","kind":"swarm","confidence":0.95,"via":"heuristic"}
```

### From Claude Code (after restart)

```
> use mcp__fleet__route to classify "ship the L20 cost tracker"
```

### From Goose (next session)

```bash
goose run -t "use fleet to dispatch a swarm to audit all FX services"
```
