"""SP-F (2026-05-24) — GitHub Issues coordination primitives for SP-E.

SP-E (Openclaw + Hermes parallel issue-workers) needs three primitives:

1. claim_issue(repo, number, agent) — atomically add label
   `claimed-by-<agent>` IFF no other claimed-by-* label is present.
   Uses ETag-conditional GET → PATCH to make the check-then-set
   atomic over GitHub's REST API.
2. release_issue(repo, number, agent) — remove the claim label.
   Idempotent.
3. peer_review_request(pr_url, reviewer_agent) — post a templated
   comment on a PR pinging the other agent to review.

list_claimable() is a convenience helper for the SP-E poll loop.

Auth: uses Settings.github_token (wired via ExternalSecret fleet-git-creds).
If empty, all methods return errors gracefully (don't crash).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from fleet.config import Settings
from fleet.telemetry import Telemetry

# Hard-coded for safety: only these agent names are allowed in the label
# claim convention. Prevents typos from polluting GitHub labels.
ALLOWED_AGENTS = {"openclaw", "hermes"}

# Issues with this label are excluded from claim_listing (manual-only).
SKIP_LABEL = "do-not-auto-fix"

# Claim retry budget on 412 (ETag preconditions failed = lost the race).
CLAIM_RETRY_BUDGET = 3


@dataclass
class ClaimResult:
    ok: bool
    blocked_by_agent: str | None = None
    error: str | None = None


@dataclass
class Issue:
    number: int
    title: str
    labels: list[str]
    url: str


class CoordinationError(Exception):
    """Non-retryable error from the GitHub API (auth, repo missing, etc.)."""


class Coordinator:
    """Async GitHub-Issues coordinator for SP-E parallel workers."""

    GITHUB_API = "https://api.github.com"

    def __init__(
        self,
        *,
        settings: Settings,
        telemetry: Telemetry | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = settings.github_token
        self._t = telemetry
        # If caller injects a client, use it (tests). Otherwise lazy-create.
        self._client = http_client

    def _headers(self, etag: str | None = None) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if etag:
            h["If-Match"] = etag
        return h

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def claim_issue(self, repo: str, number: int, agent: str) -> ClaimResult:
        """Atomically add `claimed-by-<agent>` IFF not already claimed."""
        if agent not in ALLOWED_AGENTS:
            return ClaimResult(ok=False, error=f"agent '{agent}' not in {ALLOWED_AGENTS}")
        if not self._token:
            return ClaimResult(ok=False, error="github_token missing in fleet config")

        for _attempt in range(CLAIM_RETRY_BUDGET):
            client = await self._http()
            issue_url = f"{self.GITHUB_API}/repos/{repo}/issues/{number}"

            # 1. GET the issue, capture ETag.
            r = await client.get(issue_url, headers=self._headers())
            if r.status_code == 404:
                return ClaimResult(ok=False, error=f"issue {repo}#{number} not found")
            if r.status_code != 200:
                return ClaimResult(ok=False, error=f"GET issue: {r.status_code} {r.text[:120]}")
            etag = r.headers.get("ETag", "")
            data = r.json()
            labels = [lbl["name"] for lbl in data.get("labels", [])]

            # 2. Check existing claim.
            blocked_by = next(
                (
                    lbl.removeprefix("claimed-by-")
                    for lbl in labels
                    if lbl.startswith("claimed-by-")
                ),
                None,
            )
            if blocked_by:
                if blocked_by == agent:
                    # Already claimed by us — idempotent success.
                    return ClaimResult(ok=True)
                return ClaimResult(ok=False, blocked_by_agent=blocked_by)

            # 3. Try to add the label. POST to /labels is additive.
            # We use the dedicated labels endpoint (NOT the full PATCH on
            # /issues/N) because labels endpoint accepts If-Match too via
            # the v3 API and is purely additive (no risk of clobbering
            # other fields).
            labels_url = f"{issue_url}/labels"
            r2 = await client.post(
                labels_url,
                headers=self._headers(etag=etag),
                json={"labels": [f"claimed-by-{agent}"]},
            )
            if r2.status_code == 200 or r2.status_code == 201:
                await self._fire("claim", agent, repo, number, "ok")
                return ClaimResult(ok=True)
            if r2.status_code == 412:
                # Lost the race; re-fetch + retry.
                continue
            await self._fire("claim", agent, repo, number, f"err_{r2.status_code}")
            return ClaimResult(
                ok=False,
                error=f"POST labels: {r2.status_code} {r2.text[:120]}",
            )

        # Retry budget exhausted — re-fetch one more time to report who beat us.
        client = await self._http()
        r = await client.get(
            f"{self.GITHUB_API}/repos/{repo}/issues/{number}", headers=self._headers()
        )
        if r.status_code == 200:
            labels = [lbl["name"] for lbl in r.json().get("labels", [])]
            blocked_by = next(
                (
                    lbl.removeprefix("claimed-by-")
                    for lbl in labels
                    if lbl.startswith("claimed-by-")
                ),
                "unknown",
            )
            return ClaimResult(ok=False, blocked_by_agent=blocked_by)
        return ClaimResult(ok=False, error="retry budget exhausted")

    async def release_issue(self, repo: str, number: int, agent: str) -> None:
        """Remove `claimed-by-<agent>` from issue. Idempotent (404 OK)."""
        if agent not in ALLOWED_AGENTS:
            raise CoordinationError(f"agent '{agent}' not in {ALLOWED_AGENTS}")
        if not self._token:
            raise CoordinationError("github_token missing")
        client = await self._http()
        url = f"{self.GITHUB_API}/repos/{repo}/issues/{number}/labels/claimed-by-{agent}"
        r = await client.delete(url, headers=self._headers())
        # 200 = removed; 404 = wasn't there (idempotent OK).
        if r.status_code not in (200, 204, 404):
            raise CoordinationError(f"DELETE label: {r.status_code} {r.text[:120]}")
        await self._fire("release", agent, repo, number, "ok")

    async def peer_review_request(self, pr_url: str, reviewer_agent: str) -> None:
        """Post a comment on a PR asking reviewer_agent to review.

        pr_url: https://github.com/owner/repo/pull/N
        """
        if reviewer_agent not in ALLOWED_AGENTS:
            raise CoordinationError(f"agent '{reviewer_agent}' not in {ALLOWED_AGENTS}")
        if not self._token:
            raise CoordinationError("github_token missing")

        # Parse owner/repo/N from the URL.
        try:
            parts = pr_url.rstrip("/").split("/")
            owner, repo_name, _pull, num_str = parts[-4], parts[-3], parts[-2], parts[-1]
            number = int(num_str)
        except (ValueError, IndexError) as e:
            raise CoordinationError(f"bad pr_url: {pr_url}") from e

        body = (
            f"Peer-review request for `{reviewer_agent}`.\n\n"
            f"This PR was opened by the other agent in the SP-E pair. "
            f"Please review when you next poll. "
            f"Apply the standard QC checklist (tests pass, no secrets, "
            f"matches issue intent) and either approve or comment-back "
            f"with required changes. "
            f"_Posted by Fleet `peer_review_request` tool._"
        )

        client = await self._http()
        # PR comments use the issues comments endpoint (PRs are issues).
        url = f"{self.GITHUB_API}/repos/{owner}/{repo_name}/issues/{number}/comments"
        r = await client.post(url, headers=self._headers(), json={"body": body})
        if r.status_code not in (201, 200):
            raise CoordinationError(f"POST comment: {r.status_code} {r.text[:120]}")
        await self._fire("peer_review", reviewer_agent, f"{owner}/{repo_name}", number, "ok")

    async def list_claimable(self, repo: str, agent: str) -> list[Issue]:
        """Open issues without any claimed-by-* label and without
        SKIP_LABEL. Limited to first 50 to keep the call cheap."""
        if not self._token:
            raise CoordinationError("github_token missing")
        client = await self._http()
        url = f"{self.GITHUB_API}/repos/{repo}/issues"
        r = await client.get(
            url,
            headers=self._headers(),
            params={"state": "open", "per_page": 50},
        )
        if r.status_code != 200:
            raise CoordinationError(f"list issues: {r.status_code} {r.text[:120]}")
        out: list[Issue] = []
        for raw in r.json():
            # Skip PRs (GitHub returns them from /issues endpoint too).
            if "pull_request" in raw:
                continue
            labels = [lbl["name"] for lbl in raw.get("labels", [])]
            if any(lbl.startswith("claimed-by-") for lbl in labels):
                continue
            if SKIP_LABEL in labels:
                continue
            out.append(
                Issue(
                    number=raw["number"],
                    title=raw["title"],
                    labels=labels,
                    url=raw["html_url"],
                )
            )
        return out

    async def _fire(
        self, action: str, agent: str, repo: str, number: int | str, outcome: str
    ) -> None:
        if self._t is None:
            return
        await self._t.event(
            task_id=f"coord-{action}-{repo}-{number}",
            kind="fleet_coordination",
            body={
                "action": action,
                "agent": agent,
                "repo": repo,
                "number": number,
                "outcome": outcome,
            },
        )

    async def aclose(self) -> None:
        """Close the http client if we own it."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
