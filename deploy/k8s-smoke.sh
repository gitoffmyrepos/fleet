#!/usr/bin/env bash
set -euo pipefail
NS="${NS:-memory-stack}"
URL="${FLEET_URL:-http://192.168.119.117:30801}"
TOKEN="${FLEET_BEARER:-}"

echo "→ /health"
curl -sf "$URL/health" | jq .

echo "→ /mcp/tools/list"
curl -sf "$URL/mcp/tools/list" -H "authorization: Bearer $TOKEN" | jq '.tools | length'

echo "→ /mcp/tools/call route (dry classify)"
curl -sf -X POST "$URL/mcp/tools/call" \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"name":"route","arguments":{"task":"audit all 73 services"}}' | jq .

echo "→ canned task 1: route swarm"
curl -sf -X POST "$URL/mcp/tools/call" \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"name":"route","arguments":{"task":"audit all microservices"}}' | jq '.result.kind'

echo "→ canned task 2: dispatch_subagent"
curl -sf -X POST "$URL/mcp/tools/call" \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"name":"dispatch_subagent","arguments":{"task":"hello"}}' | jq '.result.ok'

echo "→ canned task 3: status"
curl -sf -X POST "$URL/mcp/tools/call" \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"name":"status","arguments":{"limit":5}}' | jq '.result.circuits | length'

echo "→ /dashboard returns 200"
curl -sf "$URL/dashboard" -o /dev/null -w "%{http_code}\n"

echo "→ /status responds in <1s"
time curl -sf -X POST "$URL/mcp/tools/call" \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"name":"status","arguments":{"limit":1}}' >/dev/null

echo "ok"
