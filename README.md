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
