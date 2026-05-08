import json

import pytest
import respx
from httpx import Response

from fleet.graphiti_client import CACHE_GROUP_PREFIX, FLEET_GROUP, GraphitiClient


@pytest.fixture
def client() -> GraphitiClient:
    return GraphitiClient(url="http://fake/mcp", bearer="tok")


def _ok_envelope(inner: dict) -> dict:
    """Mirror the real Graphiti MCP shape: result.structuredContent.result."""
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "content": [{"type": "text", "text": "ignored"}],
            "structuredContent": {"result": inner},
            "isError": False,
        },
    }


def _legacy_envelope(inner: dict) -> dict:
    """Older MCP servers put the typed result under `result.result` directly."""
    return {"jsonrpc": "2.0", "id": "1", "result": {"result": inner}}


@respx.mock
async def test_add_episode_calls_add_memory_with_auth(client: GraphitiClient) -> None:
    route = respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"message": "queued"}))
    )
    eid = await client.add_episode(
        kind="fleet_dispatch_started",
        body={"task": "audit"},
        parent_task_id="task_xyz",
        correlation_id="abc123",
    )
    assert eid == "abc123"
    assert route.called
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer tok"
    payload = json.loads(req.content)
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "add_memory"
    args = payload["params"]["arguments"]
    assert args["name"] == "fleet_dispatch_started"
    assert args["group_id"] == FLEET_GROUP
    # uuid is intentionally NOT passed (Graphiti treats supplied uuid as
    # "update existing node" which causes "node not found"). Fleet's
    # correlation id lives in episode_body.fleet_id instead.
    assert "uuid" not in args
    body = json.loads(args["episode_body"])
    assert body["fleet_id"] == "abc123"
    assert body["parent_task_id"] == "task_xyz"
    assert body["body"] == {"task": "audit"}


@respx.mock
async def test_cache_entry_uses_per_hash_group(client: GraphitiClient) -> None:
    route = respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"message": "queued"}))
    )
    await client.add_episode(
        kind="fleet_cache_entry",
        body={"task_hash": "deadbeef" * 8, "kind": "swarm", "summary": {}, "stored_at": 0.0},
        parent_task_id=None,
    )
    args = json.loads(route.calls[0].request.content)["params"]["arguments"]
    assert args["group_id"] == f"{CACHE_GROUP_PREFIX}:{'deadbeef' * 8}"


@respx.mock
async def test_search_facts_filters_by_kind_prefix(client: GraphitiClient) -> None:
    eps = [
        {
            "uuid": "e1",
            "name": "fleet_dispatch_started",
            "content": json.dumps(
                {"kind": "fleet_dispatch_started", "body": {"x": 1}, "parent_task_id": "t1"}
            ),
        },
        {
            "uuid": "e2",
            "name": "fleet_route_decision",
            "content": json.dumps(
                {"kind": "fleet_route_decision", "body": {"y": 2}, "parent_task_id": "t1"}
            ),
        },
    ]
    respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"episodes": eps}))
    )
    facts = await client.search_facts(kind_prefix="fleet_dispatch", limit=10)
    assert len(facts) == 1
    assert facts[0]["kind"] == "fleet_dispatch_started"


@respx.mock
async def test_search_facts_filters_by_parent_task_id(client: GraphitiClient) -> None:
    eps = [
        {
            "uuid": "e1",
            "content": json.dumps({"kind": "k", "body": {}, "parent_task_id": "t1"}),
        },
        {
            "uuid": "e2",
            "content": json.dumps({"kind": "k", "body": {}, "parent_task_id": "t2"}),
        },
    ]
    respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"episodes": eps}))
    )
    facts = await client.search_facts(parent_task_id="t1")
    assert len(facts) == 1
    assert facts[0]["parent_task_id"] == "t1"


@respx.mock
async def test_get_by_hash_returns_episode(client: GraphitiClient) -> None:
    ep = {
        "uuid": "e1",
        "content": json.dumps(
            {"body": {"task_hash": "h", "kind": "swarm"}, "kind": "fleet_cache_entry"}
        ),
    }
    respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"episodes": [ep]}))
    )
    out = await client.get_by_hash(task_hash="h")
    assert out is not None
    assert out["id"] == "e1"
    assert out["body"]["task_hash"] == "h"


@respx.mock
async def test_get_by_hash_returns_none_on_empty(client: GraphitiClient) -> None:
    respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"episodes": []}))
    )
    out = await client.get_by_hash(task_hash="missing")
    assert out is None


@respx.mock
async def test_5xx_raises(client: GraphitiClient) -> None:
    respx.post("http://fake/mcp").mock(return_value=Response(500))
    with pytest.raises(RuntimeError, match="graphiti"):
        await client.add_episode(kind="k", body={}, parent_task_id=None)


@respx.mock
async def test_jsonrpc_error_raises(client: GraphitiClient) -> None:
    respx.post("http://fake/mcp").mock(
        return_value=Response(
            200,
            json={"jsonrpc": "2.0", "id": "1", "error": {"code": -32000, "message": "boom"}},
        )
    )
    with pytest.raises(RuntimeError, match="jsonrpc error"):
        await client.add_episode(kind="k", body={}, parent_task_id=None)


@respx.mock
async def test_sse_response_is_parsed(client: GraphitiClient) -> None:
    sse_body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":"1","result":{"content":[],'
        '"structuredContent":{"result":{"message":"queued"}},"isError":false}}\n\n'
    )
    respx.post("http://fake/mcp").mock(return_value=Response(200, text=sse_body))
    eid = await client.add_episode(kind="k", body={}, parent_task_id=None, correlation_id="xyz")
    assert eid == "xyz"


@respx.mock
async def test_legacy_envelope_still_parses(client: GraphitiClient) -> None:
    """Older MCP servers without structuredContent must still work."""
    eps = [{"uuid": "e1", "content": json.dumps({"kind": "k", "body": {}, "parent_task_id": None})}]
    respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_legacy_envelope({"episodes": eps}))
    )
    facts = await client.search_facts()
    assert len(facts) == 1


@respx.mock
async def test_no_bearer_omits_authorization_header() -> None:
    c = GraphitiClient(url="http://fake/mcp", bearer="")
    route = respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"message": "queued"}))
    )
    await c.add_episode(kind="k", body={}, parent_task_id=None)
    req = route.calls[0].request
    assert "authorization" not in req.headers


@respx.mock
async def test_search_facts_no_optional_params(client: GraphitiClient) -> None:
    eps = [{"uuid": "e1", "content": json.dumps({"kind": "k", "body": {}, "parent_task_id": None})}]
    respx.post("http://fake/mcp").mock(
        return_value=Response(200, json=_ok_envelope({"episodes": eps}))
    )
    facts = await client.search_facts()
    assert len(facts) == 1
