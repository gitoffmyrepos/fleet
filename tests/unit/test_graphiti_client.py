import pytest
import respx
from httpx import Response

from fleet.graphiti_client import GraphitiClient


@pytest.fixture
def client() -> GraphitiClient:
    return GraphitiClient(url="http://fake/mcp", bearer="tok")


@respx.mock
async def test_add_episode_posts_with_auth(client: GraphitiClient) -> None:
    route = respx.post("http://fake/mcp/episodes").mock(
        return_value=Response(200, json={"id": "ep_1"})
    )
    eid = await client.add_episode(
        kind="test_kind",
        body={"foo": "bar"},
        parent_task_id=None,
    )
    assert eid == "ep_1"
    assert route.called
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer tok"
    payload = req.read()
    assert b'"kind":"test_kind"' in payload


@respx.mock
async def test_search_facts_returns_list(client: GraphitiClient) -> None:
    respx.get("http://fake/mcp/facts").mock(
        return_value=Response(200, json={"facts": [{"id": "ep_1", "kind": "k"}]})
    )
    facts = await client.search_facts(parent_task_id="t1")
    assert len(facts) == 1
    assert facts[0]["kind"] == "k"


@respx.mock
async def test_failure_raises(client: GraphitiClient) -> None:
    respx.post("http://fake/mcp/episodes").mock(return_value=Response(500))
    with pytest.raises(RuntimeError, match="graphiti"):
        await client.add_episode(kind="k", body={}, parent_task_id=None)
