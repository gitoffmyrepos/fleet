import os

import httpx
import pytest

from .host_drivers import HostUnavailable, via_claude, via_goose, via_openclaw

pytestmark = pytest.mark.e2e


HOSTS = {"claude": via_claude, "goose": via_goose, "openclaw": via_openclaw}


def _enabled_hosts() -> list[tuple[str, object]]:
    skip = set((os.environ.get("FLEET_E2E_SKIP_HOSTS", "")).split(","))
    return [(n, fn) for n, fn in HOSTS.items() if n not in skip]


@pytest.fixture(scope="session")
def fleet_env() -> tuple[str, str]:
    url = os.environ["FLEET_URL"]
    bearer = os.environ["FLEET_BEARER"]
    httpx.get(f"{url}/health").raise_for_status()
    return url, bearer


@pytest.mark.parametrize(
    "name,driver",
    _enabled_hosts(),
    ids=lambda x: x[0] if isinstance(x, tuple) else str(x),
)
def test_route_classification_matches_across_hosts(
    name: str, driver: object, fleet_env: tuple[str, str]
) -> None:
    url, bearer = fleet_env
    task = "audit all 73 microservices in parallel"
    try:
        out = driver(task, url, bearer)  # type: ignore[operator]
    except HostUnavailable as e:
        pytest.skip(str(e))
    assert out["kind"] == "swarm"


@pytest.mark.parametrize("name,driver", _enabled_hosts())
def test_dispatch_chain_shape(name: str, driver: object, fleet_env: tuple[str, str]) -> None:
    url, bearer = fleet_env
    try:
        out = driver("audit deterministic-parity", url, bearer)  # type: ignore[operator]
    except HostUnavailable as e:
        pytest.skip(str(e))
    task_id = out["task_id"]
    chain = httpx.post(
        f"{url}/mcp/tools/call",
        headers={"authorization": f"Bearer {bearer}", "content-type": "application/json"},
        json={"name": "explain", "arguments": {"task_id": task_id}},
        timeout=30,
    ).json()["result"]["chain"]
    kinds = [e["kind"] for e in chain]
    assert any(k == "fleet_route_decision" for k in kinds)
