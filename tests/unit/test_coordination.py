"""SP-F (2026-05-24) — unit tests for fleet.coordination.Coordinator.

Mocks httpx to test claim race, release idempotency, peer-review.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from fleet.config import Settings
from fleet.coordination import CoordinationError, Coordinator


def _mock_response(
    status: int, json_data: dict | list | None = None, etag: str = '"abc"'
) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.headers = {"ETag": etag}
    r.json = MagicMock(return_value=json_data if json_data is not None else {})
    r.text = str(json_data) if json_data else ""
    return r


def _make_coordinator(client: MagicMock) -> Coordinator:
    settings = Settings(github_token="test-token")
    return Coordinator(settings=settings, http_client=client)


@pytest.mark.asyncio
async def test_claim_issue_success_no_existing_claim() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_mock_response(200, {"labels": [{"name": "bug"}]}))
    client.post = AsyncMock(return_value=_mock_response(200))
    coord = _make_coordinator(client)
    result = await coord.claim_issue("owner/repo", 42, "hermes")
    assert result.ok is True
    assert result.blocked_by_agent is None
    # Verify the POST used the label endpoint with claimed-by-hermes label.
    post_args = client.post.call_args
    assert "/repos/owner/repo/issues/42/labels" in post_args[0][0]
    assert post_args[1]["json"]["labels"] == ["claimed-by-hermes"]


@pytest.mark.asyncio
async def test_claim_issue_blocked_by_other_agent() -> None:
    client = MagicMock()
    client.get = AsyncMock(
        return_value=_mock_response(200, {"labels": [{"name": "claimed-by-openclaw"}]})
    )
    client.post = AsyncMock()
    coord = _make_coordinator(client)
    result = await coord.claim_issue("owner/repo", 42, "hermes")
    assert result.ok is False
    assert result.blocked_by_agent == "openclaw"
    # POST never called when blocked.
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_claim_issue_idempotent_when_already_claimed_by_us() -> None:
    client = MagicMock()
    client.get = AsyncMock(
        return_value=_mock_response(200, {"labels": [{"name": "claimed-by-hermes"}]})
    )
    client.post = AsyncMock()
    coord = _make_coordinator(client)
    result = await coord.claim_issue("owner/repo", 42, "hermes")
    assert result.ok is True
    assert result.blocked_by_agent is None
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_claim_issue_invalid_agent_rejected() -> None:
    client = MagicMock()
    coord = _make_coordinator(client)
    result = await coord.claim_issue("owner/repo", 42, "rogue-agent")
    assert result.ok is False
    assert "rogue-agent" in (result.error or "")
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_claim_issue_missing_token() -> None:
    settings = Settings(github_token="")
    coord = Coordinator(settings=settings, http_client=MagicMock())
    result = await coord.claim_issue("owner/repo", 42, "hermes")
    assert result.ok is False
    assert "github_token missing" in (result.error or "")


@pytest.mark.asyncio
async def test_release_issue_success() -> None:
    client = MagicMock()
    client.delete = AsyncMock(return_value=_mock_response(200))
    coord = _make_coordinator(client)
    await coord.release_issue("owner/repo", 42, "hermes")
    del_args = client.delete.call_args
    assert "/labels/claimed-by-hermes" in del_args[0][0]


@pytest.mark.asyncio
async def test_release_issue_idempotent_on_404() -> None:
    """If label isn't there, DELETE returns 404 — that's OK."""
    client = MagicMock()
    client.delete = AsyncMock(return_value=_mock_response(404))
    coord = _make_coordinator(client)
    # Should NOT raise.
    await coord.release_issue("owner/repo", 42, "hermes")


@pytest.mark.asyncio
async def test_peer_review_request_posts_comment() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_mock_response(201))
    coord = _make_coordinator(client)
    await coord.peer_review_request(
        "https://github.com/gitoffmyrepos/fleet/pull/123",
        reviewer_agent="openclaw",
    )
    post_args = client.post.call_args
    assert "/repos/gitoffmyrepos/fleet/issues/123/comments" in post_args[0][0]
    # Comment body mentions the reviewer agent.
    assert "openclaw" in post_args[1]["json"]["body"]


@pytest.mark.asyncio
async def test_peer_review_request_bad_url_raises() -> None:
    client = MagicMock()
    coord = _make_coordinator(client)
    with pytest.raises(CoordinationError):
        await coord.peer_review_request("not-a-url", reviewer_agent="hermes")


@pytest.mark.asyncio
async def test_list_claimable_filters_correctly() -> None:
    """Excludes PRs, claimed-by-* labels, and do-not-auto-fix label."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value=_mock_response(
            200,
            [
                # 1: clean issue — keep
                {"number": 1, "title": "issue 1", "labels": [], "html_url": "u1"},
                # 2: PR — drop
                {"number": 2, "title": "PR", "pull_request": {}, "labels": [], "html_url": "u2"},
                # 3: claimed by openclaw — drop
                {
                    "number": 3,
                    "title": "claimed",
                    "labels": [{"name": "claimed-by-openclaw"}],
                    "html_url": "u3",
                },
                # 4: do-not-auto-fix — drop
                {
                    "number": 4,
                    "title": "skip",
                    "labels": [{"name": "do-not-auto-fix"}],
                    "html_url": "u4",
                },
                # 5: clean issue with labels — keep
                {"number": 5, "title": "issue 5", "labels": [{"name": "bug"}], "html_url": "u5"},
            ],
        )
    )
    coord = _make_coordinator(client)
    issues = await coord.list_claimable("owner/repo", "hermes")
    assert [i.number for i in issues] == [1, 5]
