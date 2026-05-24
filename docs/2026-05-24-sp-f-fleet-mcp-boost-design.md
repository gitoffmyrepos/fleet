# SP-F — Fleet MCP Boost: LLM Router + Agent Coordination

**Date:** 2026-05-24
**Status:** Approved, implementing
**Sub-project of:** 7-part fleet + agent-swarm initiative (SP-A through SP-G)
**Depends on:** none (foundation)
**Unblocks:** SP-B, SP-C, SP-D, SP-E (need LLM chain), SP-G (independent)

## Goal

Add three capabilities to Fleet MCP that the rest of the initiative depends on:

1. **Work-LLM provider chain** — when an agent (SP-B/E) needs an LLM completion, try a configured priority list (Opus 4.7 → GPT-5 → Sonnet 4.6 → MiniMax → DeepSeek) with automatic fallback on rate-limit, timeout, 5xx, or auth errors. **Distinct from the existing `Router` which classifies tasks into dispatch kinds.**
2. **Agent coordination primitive** — SP-E (Openclaw + Hermes parallel issue-workers) needs an atomic claim/release mechanism on GitHub issues so the two harnesses don't pick the same issue, plus a peer-review request helper.
3. **MCP tool surface** — expose (1) over the existing MCP server (`mcp__fleet__llm_complete`), expose (2) via `mcp__fleet__claim_issue` + `mcp__fleet__peer_review_request`. SP-E agents call them; no other harness changes needed.

SP-F is intentionally NOT solving:
- The actual LLM-based work (that's SP-B/E)
- The skill registry (existing, 190 skills already work)
- Vault auth (existing — ExternalSecret already injects keys as env vars)

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ Fleet pod (in-cluster, ns=fleet)                         │
│                                                          │
│ Existing:                                                │
│   ┌───────────┐  ┌────────────┐  ┌────────────────────┐ │
│   │ Router    │  │ Dispatcher │  │ MCP server (FastAPI│ │
│   │ (classify)│  │ (subagent/ │  │ + /mcp/ endpoints) │ │
│   └───────────┘  │  swarm/…)  │  └────────────────────┘ │
│                  └────────────┘                          │
│                                                          │
│ NEW (SP-F):                                              │
│   ┌──────────────────────────────────┐  ┌─────────────┐ │
│   │ llm/provider_chain.py            │  │coordination │ │
│   │ ┌──────┐ ┌──────┐ ┌──────┐ ┌──┐ │  │.py          │ │
│   │ │opus  │→│ gpt  │→│sonnet│→│..│ │  │             │ │
│   │ └──────┘ └──────┘ └──────┘ └──┘ │  │GitHub claim/│ │
│   │ Returns first OK response       │  │peer-review  │ │
│   └──────────────────────────────────┘  └─────────────┘ │
│                       ↓                       ↓          │
│         exposed via tools.py as:                         │
│         - mcp__fleet__llm_complete                       │
│         - mcp__fleet__claim_issue                        │
│         - mcp__fleet__peer_review_request                │
└──────────────────────────────────────────────────────────┘
                       ↑
                       │ HTTPS to https://api.anthropic.com,
                       │ https://openrouter.ai/api/v1, https://api.deepseek.com
                       │ keys from env (ExternalSecret-managed)
```

## Components

### 1. `src/fleet/llm/provider_chain.py`

```python
class LLMChain:
    """Try a priority list of (provider, model) tuples with fallback.

    Construction:
        chain = LLMChain.from_env(settings)  # builds default 5-rung chain

    Use:
        result = await chain.complete(
            prompt="Fix this bug: ...",
            max_tokens=4000,
            prefer_model=None,        # None = start at rung 0
            system=None,              # optional system prompt
        )
        # result: LLMResult(text=..., model_used=..., rungs_attempted=[...],
        #                   elapsed_ms=..., token_usage={...})
    """

    DEFAULT_CHAIN = [
        ("anthropic",  "claude-opus-4-7"),
        ("openrouter", "openai/gpt-5"),         # via openrouter (already wired)
        ("anthropic",  "claude-sonnet-4-6"),
        ("minimax",    "minimax-m2"),           # direct, key in vault
        ("deepseek",   "deepseek-chat"),        # direct, key in vault
        ("gemini",     "gemini-2.5-pro"),       # last-resort fallback
    ]
```

**Behavior:**
- For each rung in order:
  - Up to 3 attempts (exponential backoff 1s, 2s, 4s)
  - Fallback on: `httpx.TimeoutException`, `httpx.HTTPStatusError(429 or 5xx)`, `httpx.HTTPStatusError(401 or 403)` (auth), provider-specific rate-limit exceptions
  - Permanent failures (`HTTPStatusError(400)` — invalid input) abort the chain entirely; return error
- Each attempt fires telemetry event `fleet_llm_attempt` with `{provider, model, rung, outcome, elapsed_ms, http_code?}`
- Returns `LLMResult` with `model_used` so caller can log/decide
- Total wall-clock cap: 5 minutes per `complete()` call

**Provider adapters** (`src/fleet/llm/providers/`):
- `anthropic.py`: thin wrapper around existing `anthropic` SDK in `requirements`
- `openrouter.py`: OpenAI-compatible client (`openai` SDK with `base_url=https://openrouter.ai/api/v1`)
- `minimax.py`: MiniMax-compatible client (`base_url=https://api.minimax.io/v1`) — likely OpenAI-compatible chat API; verify against latest docs at implementation time
- `deepseek.py`: OpenAI-compatible client (`openai` SDK with `base_url=https://api.deepseek.com/v1`)
- `gemini.py`: Google Generative AI SDK (`google-generativeai`) — different API shape from the rest, needs its own adapter

Each adapter implements:
```python
async def complete(self, prompt: str, *, model: str, max_tokens: int,
                   system: str | None) -> str
```
Raises adapter-specific exceptions that `LLMChain` recognizes for fallback.

### 2. `src/fleet/coordination.py`

```python
class Coordinator:
    """GitHub-Issues coordination primitives for SP-E parallel workers."""

    def __init__(self, gh: GitHubClient, *, telemetry: Telemetry): ...

    async def claim_issue(
        self,
        repo: str,                  # "owner/repo"
        number: int,
        agent: str,                 # "openclaw" or "hermes"
    ) -> ClaimResult:
        """Atomically add label `claimed-by-<agent>` IFF no other
        `claimed-by-*` label is present. Uses ETag-conditional PATCH.
        Returns ClaimResult(ok=bool, blocked_by_agent=str|None, ...)."""

    async def release_issue(self, repo: str, number: int, agent: str) -> None:
        """Remove `claimed-by-<agent>` label. Idempotent."""

    async def peer_review_request(
        self,
        pr_url: str,                # "https://github.com/owner/repo/pull/N"
        reviewer_agent: str,        # the OTHER agent
    ) -> None:
        """Add a comment to the PR pinging `reviewer_agent` with a
        templated 'please peer-review' block (issue link, files
        changed, test status). reviewer_agent's poller picks it up."""

    async def list_claimable(self, repo: str, agent: str) -> list[Issue]:
        """Issues that are: state=open, no `claimed-by-*` label, not
        labeled `do-not-auto-fix`. Used by SP-E poll loop."""
```

**Atomicity:** GitHub doesn't have native CAS on labels. Implementation uses:
1. `GET /repos/{repo}/issues/{number}` → capture `ETag`
2. Check labels client-side
3. `PATCH /repos/{repo}/issues/{number}/labels` with `If-Match: <ETag>` adding the claim
4. On 412 (precondition failed), re-fetch + retry up to 3× — assume someone else claimed

If after 3 retries still 412, return `ClaimResult(ok=False, blocked_by_agent=<from latest fetch>)`.

### 3. `src/fleet/tools.py` extension — 3 new MCP tools

```python
@mcp_tool(name="llm_complete")
async def llm_complete(prompt: str, max_tokens: int = 4000,
                       prefer_model: str | None = None,
                       system: str | None = None) -> dict:
    """Try Fleet's LLM chain. Returns text + which model answered."""

@mcp_tool(name="claim_issue")
async def claim_issue(repo: str, number: int, agent: str) -> dict: ...

@mcp_tool(name="peer_review_request")
async def peer_review_request(pr_url: str, reviewer_agent: str) -> dict: ...
```

All three tools require existing Fleet bearer auth (no new auth surface).

### 4. ExternalSecret update — `sb-gitops`

Fleet pod currently has env: `FLEET_ANTHROPIC_API_KEY`, `FLEET_OPENROUTER_API_KEY`.
Need to add 3 more keys so all 6 rungs work: `FLEET_MINIMAX_API_KEY`,
`FLEET_DEEPSEEK_API_KEY`, `FLEET_GEMINI_API_KEY`.

File: `prod/platform-workloads/manifests/fleet/external-secrets.yaml` (or wherever
the existing fleet ExternalSecret lives — discover during implementation).

```yaml
- secretKey: minimax
  remoteRef:
    key: secret/forex/llm/minimax
    property: api_key
- secretKey: deepseek
  remoteRef:
    key: secret/forex/llm/deepseek
    property: api_key
- secretKey: gemini
  remoteRef:
    key: secret/forex/llm/gemini
    property: api_key
```

Vault keys all confirmed present via `vault kv list secret/forex/llm`:
anthropic, deepseek, gemini, minimax, ollama, openrouter — all with `api_key` property.

## Data flow

**SP-E example:** Hermes wants to fix issue #42 in `gitoffmyrepos/FX`.

```
1. Hermes harness polls: mcp__fleet__list_claimable("FX")
2. Fleet → GitHub API → returns issues w/o claimed-by-* label
3. Hermes picks #42, calls: mcp__fleet__claim_issue("FX", 42, "hermes")
4. Fleet sets label `claimed-by-hermes` (atomic CAS via ETag)
5. Hermes works the issue, opens PR
6. Hermes calls: mcp__fleet__peer_review_request(pr_url, "openclaw")
7. Openclaw's poller sees the comment, reviews
8. Hermes merges or addresses review, then:
   mcp__fleet__release_issue("FX", 42, "hermes")
9. Label removed, issue is claimable again (closed if merged)
```

**SP-B example:** A SP-B-spawned subagent needs to analyze a vuln description.

```
1. Subagent calls: mcp__fleet__llm_complete(prompt=<vuln + code>, max_tokens=8000)
2. Fleet tries opus-4-7 → 429 → retry × 3 → fail → fallback
3. Fleet tries openrouter openai/gpt-5 → OK
4. Returns {text, model_used: "openai/gpt-5", rungs_attempted: ["opus:429", "gpt:ok"]}
5. Subagent logs which model answered (cost-tracking)
```

## Error handling

| Failure | Handling |
|---|---|
| All 5 LLM rungs fail | `LLMChainExhaustedError` raised; MCP tool returns `{ok: false, rungs: [...]}`. Caller decides retry or abort. |
| GitHub auth fails on `claim_issue` | Tool returns `{ok: false, error: "gh_auth"}`. SP-E poller surfaces to ops. |
| ETag CAS retry exhausted (race with other agent) | Tool returns `{ok: false, blocked_by_agent: <name>}`. Caller picks a different issue. |
| ExternalSecret missing minimax/deepseek/gemini key | Corresponding rung fails → telemetry alarms; chain still works for rungs that have keys. |

## Testing

Per `feedback_no_skipping_issues.md`: comprehensive coverage.

- **Unit:** `tests/unit/test_llm_chain.py` — mock all 5 providers, test fallback transitions for each error class (429, 500, timeout, 401).
- **Unit:** `tests/unit/test_coordination.py` — mock GitHub API, test claim race (412 → retry → resolve / fail), test peer_review_request comment format.
- **Integration:** `tests/integration/test_llm_complete_mcp.py` — spin up FastAPI test client, hit `/mcp/llm_complete`, assert telemetry events fire. Mock providers (don't burn real API calls in CI).
- **Smoke (manual):** after deploy, run a real `llm_complete` against each rung to confirm keys + base URLs work.

## Open questions resolved (was 3, now 0)

1. ✅ **Vault path layout** — `secret/forex/llm/<provider>` with `api_key` property. Confirmed via gitops grep.
2. ✅ **Model IDs** — Anthropic IDs from runtime constants (`claude-opus-4-7`, `claude-sonnet-4-6`). OpenRouter prefixed IDs (`openai/gpt-5`, `minimax/minimax-m2`) — verify exact current IDs against `https://openrouter.ai/api/v1/models` before pinning; spec uses placeholders and impl will lock to live values.
3. ✅ **Fleet runs in-cluster** (`ns=fleet`, NodePort 31801). Keys via ExternalSecret env vars. No in-process Vault client needed.

## File deliverables

| Path | Change |
|---|---|
| `fleet/src/fleet/llm/__init__.py` | new |
| `fleet/src/fleet/llm/provider_chain.py` | new, ~180 LOC |
| `fleet/src/fleet/llm/providers/anthropic.py` | new, ~40 LOC |
| `fleet/src/fleet/llm/providers/openrouter.py` | new, ~50 LOC |
| `fleet/src/fleet/llm/providers/minimax.py` | new, ~50 LOC |
| `fleet/src/fleet/llm/providers/deepseek.py` | new, ~40 LOC |
| `fleet/src/fleet/llm/providers/gemini.py` | new, ~60 LOC (different API shape) |
| `fleet/src/fleet/coordination.py` | new, ~140 LOC |
| `fleet/src/fleet/config.py` | +5 lines for openrouter/deepseek env vars |
| `fleet/src/fleet/tools.py` | +60 LOC for 3 MCP tools |
| `fleet/tests/unit/test_llm_chain.py` | new, ~150 LOC |
| `fleet/tests/unit/test_coordination.py` | new, ~100 LOC |
| `fleet/tests/integration/test_llm_complete_mcp.py` | new, ~80 LOC |
| `sb-gitops/prod/platform-workloads/manifests/fleet/external-secrets.yaml` | +5 lines |

## Done criteria

- [ ] All 4 components implemented
- [ ] Unit + integration tests pass locally
- [ ] gitops PR merged; ExternalSecret syncs `FLEET_DEEPSEEK_API_KEY` into fleet pod
- [ ] Fleet pod restarts cleanly with new env var
- [ ] Smoke test: `mcp__fleet__llm_complete` returns answers from all 5 rungs when forced via `prefer_model`
- [ ] Smoke test: `mcp__fleet__claim_issue` adds label on a sandbox issue, second call from different agent returns blocked
- [ ] Memory note added documenting the new MCP tools so SP-E knows they exist

## References

- Existing Router (task classifier, not work-LLM): `fleet/src/fleet/router.py`
- Existing skill registry: `fleet/src/fleet/skills.py` + `~/.hermes/skills/.hub/index.json` (memory: `reference_fleet_skills_integration`)
- Vault ExternalSecret pattern: `sb-gitops/prod/.../caipe/external-secrets.yaml`
- Memory: `feedback_check_memory_for_creds_first` (Vault check before claiming creds missing)
