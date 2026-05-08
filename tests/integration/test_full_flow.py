import httpx
import pytest

pytestmark = pytest.mark.integration


def call(url: str, headers: dict, name: str, args: dict) -> dict:
    r = httpx.post(
        f"{url}/mcp/tools/call",
        headers=headers,
        json={"name": name, "arguments": args},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["result"]


def test_route_swarm(fleet_url: str, headers: dict) -> None:
    out = call(fleet_url, headers, "route", {"task": "audit all 73 microservices in parallel"})
    assert out["kind"] == "swarm"


def test_dispatch_swarm_caches_on_second_call(fleet_url: str, headers: dict) -> None:
    args = {"task": "audit svcs deterministic", "agents": 5}
    first = call(fleet_url, headers, "dispatch_swarm", args)
    assert first["cache_hit"] is False
    assert first["ok"] is True
    second = call(fleet_url, headers, "dispatch_swarm", args)
    assert second["cache_hit"] is True


def test_dispatch_phase_plan_returns_phase_dir(fleet_url: str, headers: dict) -> None:
    out = call(fleet_url, headers, "dispatch_phase", {"task": "add SSE event", "stage": "plan"})
    assert out["ok"] is True
    assert out["summary"]["phase_dir"] is not None


def test_dispatch_verify_returns_verdict(fleet_url: str, headers: dict) -> None:
    out = call(fleet_url, headers, "dispatch_verify", {"task": "the auth flow"})
    assert out["summary"]["verdict"] == "PASS"


def test_status_includes_circuits(fleet_url: str, headers: dict) -> None:
    out = call(fleet_url, headers, "status", {"limit": 5})
    assert "circuits" in out


def test_explain_returns_chain(fleet_url: str, headers: dict) -> None:
    routed = call(fleet_url, headers, "route", {"task": "explain something"})
    out = call(fleet_url, headers, "explain", {"task_id": routed["task_id"]})
    assert "chain" in out


def test_circuit_close_succeeds_for_known_upstream(fleet_url: str, headers: dict) -> None:
    call(fleet_url, headers, "dispatch_subagent", {"task": "noop"})
    out = call(fleet_url, headers, "circuit_close", {"name": "superpowers"})
    assert out["closed"] is True


def test_unknown_tool_returns_400(fleet_url: str, headers: dict) -> None:
    r = httpx.post(
        f"{fleet_url}/mcp/tools/call",
        headers=headers,
        json={"name": "nope", "arguments": {}},
        timeout=10,
    )
    assert r.status_code == 400


def test_dashboard_returns_200(fleet_url: str) -> None:
    r = httpx.get(f"{fleet_url}/dashboard", timeout=10)
    assert r.status_code == 200
    assert "Fleet" in r.text
