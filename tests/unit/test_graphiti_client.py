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


@respx.mock
async def test_get_by_hash_200_returns_episode(client: GraphitiClient) -> None:
    respx.get("http://fake/mcp/episodes/by-hash").mock(
        return_value=Response(200, json={"id": "ep_x", "body": {"k": "v"}})
    )
    ep = await client.get_by_hash(task_hash="abcd")
    assert ep is not None
    assert ep["id"] == "ep_x"
    assert ep["body"]["k"] == "v"


@respx.mock
async def test_get_by_hash_404_returns_none(client: GraphitiClient) -> None:
    respx.get("http://fake/mcp/episodes/by-hash").mock(return_value=Response(404))
    ep = await client.get_by_hash(task_hash="missing")
    assert ep is None


@respx.mock
async def test_get_by_hash_500_raises(client: GraphitiClient) -> None:
    respx.get("http://fake/mcp/episodes/by-hash").mock(return_value=Response(500))
    with pytest.raises(RuntimeError, match="graphiti get_by_hash"):
        await client.get_by_hash(task_hash="boom")


@respx.mock
async def test_search_facts_failure_raises(client: GraphitiClient) -> None:
    respx.get("http://fake/mcp/facts").mock(return_value=Response(503))
    with pytest.raises(RuntimeError, match="graphiti search_facts"):
        await client.search_facts(parent_task_id="t1")


@respx.mock
async def test_search_facts_no_optional_params(client: GraphitiClient) -> None:
    route = respx.get("http://fake/mcp/facts").mock(return_value=Response(200, json={"facts": []}))
    facts = await client.search_facts()
    assert facts == []
    assert route.called
    req = route.calls[0].request
    assert "parent_task_id" not in req.url.params
    assert "kind_prefix" not in req.url.params
    assert req.url.params.get("limit") == "200"


@pytest.fixture
def client_no_auth() -> GraphitiClient:
    return GraphitiClient(url="http://fake/mcp", bearer="")


@respx.mock
async def test_no_bearer_omits_authorization_header(client_no_auth: GraphitiClient) -> None:
    route = respx.post("http://fake/mcp/episodes").mock(
        return_value=Response(200, json={"id": "ep_a"})
    )
    await client_no_auth.add_episode(kind="k", body={}, parent_task_id=None)
    req = route.calls[0].request
    assert "authorization" not in req.headers


@respx.mock
async def test_add_episode_missing_id_field_raises(client: GraphitiClient) -> None:
    respx.post("http://fake/mcp/episodes").mock(return_value=Response(200, json={}))
    with pytest.raises(RuntimeError, match="missing 'id'"):
        await client.add_episode(kind="k", body={}, parent_task_id=None)
