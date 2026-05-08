# Fleet — Unified Meta-Orchestrator Design

**Date:** 2026-05-07
**Status:** Design approved, awaiting user spec review
**Working name:** **Fleet** (placeholder — finalize before code)
**Author:** kelvin + Claude (brainstorming session)

---

## 1. Summary

Fleet is a meta-orchestrator that unifies three existing systems — **ruflo / claude-flow** (60-agent parallel swarms), **superpowers** (workflow methodology skills), and **gsd / getshitdone** (project lifecycle commands) — behind a single MCP service plus skill/command bundle. Goals: cohesion, token reduction, cross-host parity (Claude Code / OpenClaw / Goose), and continuous growth as the three upstreams evolve.

The three originals stay vanilla; Fleet wraps them. Their growth becomes Fleet's growth automatically.

## 2. Goals

- **One entry point** — `/fleet "<task>"` (smart auto-route) or `/fleet <verb>` (explicit), available identically on Claude Code, OpenClaw, and Goose
- **Token reduction** — slim curated MCP surface (~14 tools vs upstream ~150+), subagent isolation by default, hash-keyed result caching, ≤2k-token return summaries
- **Cross-host parity** — HTTP/SSE MCP in k8s; Goose and OpenClaw can issue the same task as Claude and see the same Graphiti episodes
- **Provable observability** — every dispatch is a Graphiti episode; `/fleet explain <task_id>` returns the full citation chain
- **Continuous growth** — registry auto-discovers new agents from upstream sources; new ruflo/superpowers/gsd capabilities are picked up without code changes

## 3. Non-goals

- **Not** a fork or rewrite of ruflo / superpowers / gsd. They remain upstream dependencies.
- **Not** a new agent definition format. Fleet uses existing agent defs from each source via namespacing.
- **Not** a Claude Code replacement — it sits inside the host, not above it.
- **Not** a multi-tenant SaaS. Single operator, single homelab cluster.

## 4. Architecture

### 4.1 Layered overview

```
┌─────────────────────────────────────────────────────┐
│  HOSTS: Claude Code  •  OpenClaw  •  Goose          │   speak MCP
└─────────────────┬───────────────────────────────────┘
                  │ HTTP / SSE (Streamable HTTP MCP)
                  ▼
┌─────────────────────────────────────────────────────┐
│  fleet-mcp   (k8s memory-stack ns, NodePort)        │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Curated tool surface (~14 tools)                │ │
│ │  route • plan • dispatch • swarm • verify •     │ │
│ │  ship • status • explain • cache_lookup •       │ │
│ │  list_agents • register_agent • telemetry •     │ │
│ │  cancel • circuit_close                         │ │
│ ├─────────────────────────────────────────────────┤ │
│ │ Router (Sonnet 4.6 classifier + heuristic gate) │ │
│ │  task → {swarm | phase | subagent | verify}     │ │
│ ├─────────────────────────────────────────────────┤ │
│ │ Registry (union pool, namespaced)               │ │
│ │  ruflo:* • superpowers:* • claude:* • gsd:*     │ │
│ │  scored by role-tag + outcome history           │ │
│ ├─────────────────────────────────────────────────┤ │
│ │ Telemetry + Cache (Graphiti episodes)           │ │
│ │  tokens • latency • cache hit • hash-keyed      │ │
│ ├─────────────────────────────────────────────────┤ │
│ │ Circuit breakers (per upstream)                 │ │
│ │  ruflo • superpowers • gsd                      │ │
│ └─────────────────────────────────────────────────┘ │
└────────┬─────────────┬─────────────┬────────────────┘
         ▼             ▼             ▼
   ruflo CLI     superpowers    gsd lifecycle
   (claude-flow)    skills      (.planning artifacts)
```

### 4.2 Three packages in the new repo (`fleet/`)

| Package | Purpose | Install target |
|---|---|---|
| `fleet-mcp/` | Python FastAPI Streamable-HTTP MCP server + Helm chart | k8s `memory-stack` ns (NodePort) |
| `fleet-skills/` | Auto-activating router skill + per-verb skills | `~/.claude/skills/fleet/` (and OpenClaw skills dir) |
| `fleet-commands/` | Slash commands | `~/.claude/commands/fleet/` |

### 4.3 Why this shape

- **HTTP MCP is lowest common denominator.** Goose can't use Claude skills/commands but can absolutely use MCP. Parity for free, same precedent as Graphiti at `192.168.119.117:30800/mcp`.
- **Skills/commands are the ergonomic layer**, not new behavior — they make the HTTP tools feel native in Claude/OpenClaw.
- **Originals stay vanilla.** No merge debt.

## 5. Components

### 5.1 `fleet-mcp` modules

Each module ≤500 lines, single responsibility. Shared deps: every module emits to `telemetry.py`; dispatchers consult `circuit.py` before spawning. External deps (Anthropic SDK, Graphiti client, claude-flow CLI) noted per module below.

| Module | Purpose | Notes |
|---|---|---|
| `router.py` | Classify `task → {swarm \| phase \| subagent \| verify \| ship}` | Two-stage: cheap heuristic first, Sonnet 4.6 only if confidence < 0.7 |
| `registry.py` | Union pool of 200+ agents, namespaced | Loaded from disk + rescanned periodically; `score(role, task, history)` ranks candidates; cheaper agents win ties |
| `dispatcher/swarm.py` | Wraps `claude-flow swarm start` / `hive-mind spawn` | Streams progress as MCP notifications |
| `dispatcher/phase.py` | Drives `gsd plan-phase → execute-phase → verify-work` | Reads `.planning/` artifacts, returns slim summaries |
| `dispatcher/subagent.py` | Single isolated subagent | Reuses `superpowers/dispatching-parallel-agents` pattern; ≤2k-token return |
| `dispatcher/verify.py` | Verification gate | Wraps `superpowers/verification-before-completion` |
| `telemetry.py` | Episode-log every dispatch to Graphiti | task_hash, kind, agent, tokens, duration, cache_hit, outcome |
| `cache.py` | Hash-keyed result memoization | `sha256(task_normalized + scope_paths)`; default 24h TTL; stored as Graphiti episodes |
| `circuit.py` | Per-upstream circuit breakers | 3 failures in 10 min → trip; 5 min cooldown; half-open probe; close on success |
| `server.py` | FastAPI Streamable-HTTP MCP entrypoint | Bearer-token authn, same pattern as Graphiti |
| `dashboard.py` | Read-only HTMX page | Active dispatches, recent history, cost burn-down, cache hit-rate, registry size, circuit state |

### 5.2 `fleet-skills`

- **`fleet/router/SKILL.md`** — auto-activates on parallelizable / multi-step task descriptions. Sole job: *"before you spawn anything yourself, call `mcp__fleet__route` first."* This is the skill that converts ad-hoc agent spawning into Fleet-routed dispatch.
- **`fleet/swarm/`**, **`fleet/plan/`**, **`fleet/verify/`**, **`fleet/ship/`**, **`fleet/explain/`**, **`fleet/cost/`** — per-verb skills with examples + gotchas, lazy-loaded.

### 5.3 `fleet-commands`

`/fleet "<task>"` (auto-route) · `/fleet swarm "<task>" [--agents N]` · `/fleet plan "<goal>"` · `/fleet ship` · `/fleet verify [scope]` · `/fleet status [--hung]` · `/fleet explain <task_id>` · `/fleet cost [--hours N]` · `/fleet cancel <task_id>`

### 5.4 What changes upstream in ruflo / superpowers / gsd?

**Nothing.** They stay vanilla; Fleet calls them.

## 6. Data Flow

### 6.1 Path A — Auto-route → Swarm (fan-out task)

```
User: /fleet "audit health of all 73 microservices"
   ▼
[fleet-skills/router] → POST /mcp tool=route
[router.py] heuristic gate hits ("all 73" + "audit") → kind=swarm, conf=0.92, skip LLM
   ▼ cache.lookup(task_hash) → MISS
[dispatcher/swarm.py]
   ├ telemetry.start(task_id, kind=swarm)
   ├ subprocess: claude-flow swarm start -o "<task>" -s development --agents 20
   ├ stream stdout → MCP notifications → caller
   └ on completion: telemetry.end(tokens, duration), cache.write(task_hash, summary)
   ▼
[Graphiti episode] kind=fleet_dispatch, parent=task_id, summary ≤2k tokens
   ▼
Return: {task_id, summary, telemetry: {tokens, duration, agents_used}}
```

### 6.2 Path B — Auto-route → Phase (multi-step build)

```
User: /fleet "add SSE event for tool_dispatched in HUD"
   ▼
[router.py] heuristic low conf → escalate to Sonnet 4.6
            LLM: {kind: "phase", reason: "spans plan+code+verify"}
   ▼ tool=plan
[dispatcher/phase.py]
   ├ shells: claude (gsd:discuss-phase → gsd:plan-phase)
   ├ awaits PLAN.md in .planning/<phase>/
   ├ shells: claude (gsd:execute-phase)
   ├ shells: claude (gsd:verify-work)
   └ returns slim summary + path to .planning/<phase>/VERIFICATION.md
```

### 6.3 Path C — Cache hit (repeat work)

```
User: /fleet "audit health of all 73 microservices"  (same task hours later)
   ▼
[cache.py] Graphiti query → HIT (age 2h, TTL 24h)
   ▼
Return immediately: cached summary + cache_hit=true + age_seconds
   ZERO LLM calls. ZERO subagent spawns. ~10ms.
```

### 6.4 Path D — Audit chain (`/fleet explain`)

```
User: /fleet explain task_8a4f...
   ▼ tool=explain
[server.py]
   ├ Graphiti search: parent=task_id → all child episodes
   ├ index by episode_kind: route_decision, registry_score, dispatch_*, telemetry_end
   └ build chain: route → registry pick → dispatch → result, each with ≤120-char summary
   ▼
Returns structured citation list (mirrors Nova L17 pattern). Dashboard renders lazily.
```

### 6.5 Cross-host reach

Goose has no skills, but calls `mcp__fleet__route` then `mcp__fleet__dispatch_*` directly. OpenClaw uses the skill bundle like Claude. **Same MCP, same outcomes, same Graphiti episodes** — `/fleet status` from Claude can show what Goose dispatched five minutes ago.

### 6.6 Critical rule — router is read-only dispatch advisor

The router NEVER spawns Claude/Goose itself. It returns a *dispatch decision*; the **caller** drives the dispatch. This means:

- Telemetry of the parent host stays accurate (Goose's tokens stay on Goose's bill)
- The caller can override (model says "actually swarm with 40 agents not 20")
- Easy to dry-run (`route` is read-only)

## 7. Error Handling

### 7.1 Three guiding rules

1. **Never silently swallow.** Every failure → `telemetry_failure` Graphiti episode, surfaced via `/fleet explain`.
2. **Degrade, don't crash.** Failed router → fall back to "subagent" with warning. Failed dispatch → return partial results + error. The MCP itself rarely 500s.
3. **Caller stays in charge.** Errors return as MCP tool results with structured `{ok:false, error, recovery_hint}`. Host LLM decides retry/escalate/ask. No internal auto-retry.

### 7.2 Failure matrix

| Failure | Detection | Fleet behavior | Caller sees |
|---|---|---|---|
| **Router LLM unreachable** | Anthropic API timeout / 5xx | Heuristic-only mode, `degraded=true` | `{kind, degraded:true, reason:"router LLM unavailable"}` |
| **Heuristic + LLM both inconclusive** | Both confidence < 0.5 | Default to `subagent` (cheapest safe option) | `{kind:"subagent", confidence:"low", recovery_hint:"specify verb explicitly"}` |
| **Dispatcher subprocess crashes** | non-zero exit / SIGCHLD | Capture stderr, kill child swarm, write partial telemetry | `{ok:false, error, partial_summary, agents_completed:N}` |
| **Cache returns corrupt entry** | Schema validation fails on read | Treat as miss, log corruption, evict key | Normal (slower) dispatch |
| **Registry sync fails on startup** | Filesystem path missing | Boot with last-good cached registry from Graphiti, `stale=true` | `list_agents` annotates `stale_since` timestamp |
| **ruflo not installed on host** | `which claude-flow` returns non-zero | Disable swarm dispatcher only — phase/subagent/verify still work | `{ok:false, error:"swarm capability unavailable on this host"}` |
| **MCP stream drops mid-dispatch** | SSE heartbeat lost | Server keeps running, caller polls `mcp__fleet__status?task_id=X` | Caller resumes via status |
| **Long-running phase timeout** | Default 30 min, configurable | SIGTERM, then SIGKILL after grace, write partial results | `{ok:false, error:"timeout", partial:..., recovery:"raise --timeout"}` |
| **Agent not in registry** | Lookup miss | Suggest 3 nearest by role-tag distance | `{ok:false, suggestions:["ruflo:coder","superpowers:coder",...]}` |
| **Auth (bearer token)** | 401 on inbound | Reject, log source IP + token prefix | Standard 401 |
| **Cost runaway (per-task)** | Per-task limit (default 200k tokens) | Halt dispatch, return what's done | `{ok:false, error:"budget_exceeded", spent_tokens, recovery:"raise --budget"}` |
| **Upstream circuit trips** | 3 failures inside 10 min on one upstream | Trip breaker, emit `circuit_tripped` episode + dashboard banner | `{ok:false, error:"circuit_open", upstream, retry_after_seconds}` |

### 7.3 Operator escape hatches

- `mcp__fleet__cancel(task_id)` — graceful kill of any in-flight dispatch
- `mcp__fleet__circuit_close(name)` — manual override of a tripped breaker
- `/fleet status --hung` — lists tasks with no telemetry update >5min, suggests cancel
- Deployment env var `FLEET_DRY_RUN=true` — router still runs, no actual dispatch (migration testing)

### 7.4 Privacy boundary

Mirrors Nova L9/L10 pattern: user task text stored in Graphiti for caching/explain. Tool *outputs* (raw subagent responses) are stored too but **truncated to ≤2k tokens** before any cross-host return. Full logs available only via `/fleet explain` from inside Claude/OpenClaw — never streamed unsolicited.

## 8. Testing

### 8.1 Coverage targets

- 80% line overall
- **90% on `router.py` and `registry.py`** (highest blast radius)

### 8.2 Tier 1 — Unit (fast, no network)

| Module | Key tests |
|---|---|
| `router.py` | heuristic-gate hits, LLM-fallback path, confidence boundary (0.5/0.7), degraded-mode shape, malformed input → safe default |
| `registry.py` | namespace dedup, scoring with empty history, scoring with mixed outcomes, stale-after-startup boot, score-tie tiebreak by cost |
| `dispatcher/*` | subprocess mocked: zero-exit, non-zero-exit, signal-killed, stderr captured, partial summary on timeout |
| `cache.py` | hash normalization (whitespace / case / path order independence), TTL respected, corrupt-entry eviction |
| `telemetry.py` | episode shape matches Graphiti schema, redaction of >2k-token outputs, failure episodes recorded |
| `circuit.py` | trip after N failures, cooldown enforced, half-open probe success closes, half-open probe fail re-trips |

Mocks: fake Anthropic SDK (record/replay JSON fixtures), fake `claude-flow` binary (shell script emitting scripted stdout/exit codes), in-memory Graphiti stub.

### 8.3 Tier 2 — Integration (real-ish, inside one container)

- Full route → dispatch → cache → telemetry chain with in-memory Graphiti
- Circuit-breaker trip across 3 simulated swarm crashes + recovery probe
- Timeout path: long-running fake dispatcher, SIGTERM+SIGKILL grace, partial result returned
- Registry rescan picks up a newly-dropped agent file mid-test
- Authn: missing/expired/wrong bearer token → 401 each
- Budget cap trip mid-dispatch returns `budget_exceeded` with spent count

Driver: `docker compose up -d graphiti-test fleet-mcp-test && pytest -m integration`

### 8.4 Tier 3 — E2E parity (the headline test)

```python
@pytest.mark.parametrize("host", ["claude", "goose", "openclaw"])
def test_same_task_same_outcome(host):
    task_id = invoke_via(host, '/fleet "audit fake-svc-{1..10}"')
    chain = graphiti.search_facts(parent=task_id)
    assert chain[0].kind == "route_decision"
    assert chain[-1].kind == "telemetry_end"
    assert chain[-1].outcome == "ok"
```

Hosts driven by:
- **claude**: `claude --print --mcp-config ... '/fleet ...'`
- **goose**: `goose run -t '<MCP tool call json>'`
- **openclaw**: `openclaw exec '/fleet ...'`

Same task from all three must produce same Graphiti chain shape (kinds match; agents/tokens may differ). This is the test that fails if parity ever breaks.

### 8.5 Tier 4 — Smoke against staging cluster

`make smoke` hits `https://fleet.memory-stack.svc.cluster.local:30NNN/health`, runs three canned tasks (one swarm, one phase, one verify), asserts dashboard renders, `/fleet status` returns within 1s.

### 8.6 Test data hygiene

Fixtures under `tests/fixtures/{router_cases,registry_snapshots,graphiti_episodes,cli_outputs}/`. Every new heuristic-gate keyword adds a fixture row. Every new agent in the registry adds a snapshot. **No test reaches the real Anthropic API or real claude-flow CLI** outside Tier 4.

### 8.7 CI gates

- Unit + Integration on every push (target ≤4 min wall)
- E2E parity on PR + nightly (≤15 min)
- Smoke on every cluster deploy (post-Helm-apply)
- Coverage report uploaded; PR blocked if <80%

## 9. Open Questions / TODO before code

1. **Final name.** "Fleet" is a placeholder. Alternatives: Nexus, Constellation, Conductor, Maestro, Nova-Orchestrator, Swarm-OS. Decide before the writing-plans skill creates the implementation plan.
2. **Bearer-token storage.** Vault path TBD — likely `secret/fleet/mcp_bearer` mirroring Graphiti's setup. ExternalSecret manifest needed.
3. **NodePort number.** Pick during Helm chart authoring; reserve in homelab port-allocation doc.
4. **Sonnet vs Opus for router.** Default Sonnet 4.6 (cheap + fast); allow `FLEET_ROUTER_MODEL` override. Confirm with cost target.
5. **Per-task budget default.** Spec says 200k tokens — validate against typical phase-plan cost from gsd telemetry.
6. **Registry refresh interval.** 5 min vs 15 min vs on-demand-only. Trade-off: freshness vs filesystem load.
7. **Where does the `fleet/` repo live?** New github.com/strategybase repo? sb-claude-config submodule? Decide before scaffolding.

## 10. Success Criteria (definition of done for v1)

- [ ] `fleet-mcp` deployed to `memory-stack` ns, NodePort reachable from Claude Code, OpenClaw, Goose
- [ ] All 12 MCP tools present and tested
- [ ] Router heuristic gate covers ≥80% of typical tasks without LLM call
- [ ] Registry contains ≥200 namespaced agents from all four sources
- [ ] Graphiti episodes written for every dispatch; `/fleet explain` returns chain
- [ ] Cache hit on repeat task returns in <100ms with zero LLM cost
- [ ] Circuit breakers tested via Tier 2 integration suite
- [ ] E2E parity test (Tier 3) passes for Claude / OpenClaw / Goose
- [ ] Dashboard renders active dispatches, cost burn-down, cache hit-rate, circuit state
- [ ] Coverage ≥80% line / ≥90% on router + registry
- [ ] Operator runbook in `fleet/docs/RUNBOOK.md` covering: deploy, rollback, hung-task recovery, manual circuit close

## 11. Out of scope for v1 (future growth)

- Multi-region / multi-cluster fleet federation
- Fine-grained per-user quotas (single-operator assumption)
- Agent definition authoring UI (use existing upstream conventions)
- Auto-tuning of cache TTL per task class
- Replay / time-travel of historical dispatches
- Web UI for direct task submission (CLI/MCP only in v1)
