from __future__ import annotations

"""Tests for agentception/intelligence/guards.py (AC-403, AC-404).

Coverage:
- detect_out_of_order_prs() returns empty list when all PRs are in order
- detect_out_of_order_prs() returns PRViolation when phase label mismatches
- detect_out_of_order_prs() skips PRs with no 'Closes #N' reference
- close_pr() is called via the /api/intelligence/pr-violations/{n}/close route
- detect_stale_claims() flags issues with agent:wip but no worktree
- detect_stale_claims() ignores issues whose worktree exists
- clear_stale_claim endpoint removes the agent:wip label
- stale claims from guards propagate into PipelineState.alerts via detect_alerts()

Run targeted:
    pytest agentception/tests/test_agentception_guards.py -v
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentception.app import app
from agentception.intelligence.guards import PRViolation, detect_out_of_order_prs, detect_stale_claims
from agentception.models import StaleClaim, TaskFile
from agentception.poller import GitHubBoard, detect_alerts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pr(
    number: int,
    title: str = "feat: something",
    body: str = "",
    labels: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build a minimal PR dict matching the shape returned by get_open_prs_with_body."""
    return {
        "number": number,
        "title": title,
        "headRefName": f"feat/issue-{number}",
        "labels": labels or [],
        "body": body,
    }


def _make_issue(number: int, label: str) -> dict[str, object]:
    """Build a minimal issue dict matching the shape returned by get_issue."""
    return {
        "number": number,
        "state": "OPEN",
        "title": f"Issue #{number}",
        "labels": [label],
    }


# ---------------------------------------------------------------------------
# detect_out_of_order_prs() — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_detect_no_violations_when_all_in_order() -> None:
    """All PRs link to issues in the active phase — no violations returned."""
    active = "agentception/4-intelligence"
    prs = [
        _make_pr(100, body="Closes #200"),
        _make_pr(101, body="Closes #201"),
    ]
    issue_200 = _make_issue(200, active)
    issue_201 = _make_issue(201, active)

    def _fake_get_issue(number: int) -> dict[str, object]:
        return {200: issue_200, 201: issue_201}[number]

    with (
        patch(
            "agentception.intelligence.guards.get_active_label",
            new_callable=AsyncMock,
            return_value=active,
        ),
        patch(
            "agentception.intelligence.guards.get_open_prs_with_body",
            new_callable=AsyncMock,
            return_value=prs,
        ),
        patch(
            "agentception.intelligence.guards.get_issue",
            side_effect=_fake_get_issue,
        ),
    ):
        violations = await detect_out_of_order_prs()

    assert violations == [], f"Expected no violations, got: {violations}"


@pytest.mark.anyio
async def test_detect_violation_wrong_phase_label() -> None:
    """A PR linking to an issue from a past phase produces a PRViolation."""
    active = "agentception/4-intelligence"
    old_label = "agentception/0-scaffold"
    prs = [
        _make_pr(77, title="feat: old work", body="Closes #608"),
    ]
    issue_608 = _make_issue(608, old_label)

    with (
        patch(
            "agentception.intelligence.guards.get_active_label",
            new_callable=AsyncMock,
            return_value=active,
        ),
        patch(
            "agentception.intelligence.guards.get_open_prs_with_body",
            new_callable=AsyncMock,
            return_value=prs,
        ),
        patch(
            "agentception.intelligence.guards.get_issue",
            new_callable=AsyncMock,
            return_value=issue_608,
        ),
    ):
        violations = await detect_out_of_order_prs()

    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, PRViolation)
    assert v.pr_number == 77
    assert v.pr_title == "feat: old work"
    assert v.expected_label == active
    assert v.actual_label == old_label
    assert v.linked_issue == 608


@pytest.mark.anyio
async def test_detect_no_linked_issue_skips() -> None:
    """PRs without a 'Closes #N' reference are silently skipped."""
    active = "agentception/4-intelligence"
    prs = [
        _make_pr(50, body="This PR has no closes reference."),
        _make_pr(51, body=""),  # empty body
    ]

    with (
        patch(
            "agentception.intelligence.guards.get_active_label",
            new_callable=AsyncMock,
            return_value=active,
        ),
        patch(
            "agentception.intelligence.guards.get_open_prs_with_body",
            new_callable=AsyncMock,
            return_value=prs,
        ),
        patch(
            "agentception.intelligence.guards.get_issue",
            new_callable=AsyncMock,
        ) as mock_get_issue,
    ):
        violations = await detect_out_of_order_prs()

    # get_issue should never be called since no PR has a Closes reference.
    mock_get_issue.assert_not_called()
    assert violations == []


def test_close_violating_pr_calls_gh() -> None:
    """The close endpoint calls close_pr() with the correct PR number and message."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        with patch(
            "agentception.routes.api.intelligence.close_pr",
            new_callable=AsyncMock,
        ) as mock_close:
            response = client.post("/api/intelligence/pr-violations/42/close")

    assert response.status_code == 200
    assert response.json() == {"closed": 42}
    mock_close.assert_awaited_once_with(
        42,
        "Closed by AgentCeption: out-of-order PR violation.",
    )


# ---------------------------------------------------------------------------
# detect_stale_claims() — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_detect_stale_claim_missing_worktree(tmp_path: Path) -> None:
    """detect_stale_claims() should flag an issue whose expected worktree is absent."""
    wip_issues = [{"number": 100, "title": "Fix the thing"}]
    # tmp_path/issue-100 does NOT exist → stale claim expected
    claims = await detect_stale_claims(wip_issues, tmp_path)

    assert len(claims) == 1
    assert claims[0].issue_number == 100
    assert claims[0].issue_title == "Fix the thing"
    assert claims[0].worktree_path == str(tmp_path / "issue-100")


@pytest.mark.anyio
async def test_detect_no_stale_when_worktree_exists(tmp_path: Path) -> None:
    """detect_stale_claims() must not flag issues whose worktree directory exists."""
    # Create the worktree directory so the issue is considered live.
    (tmp_path / "issue-200").mkdir()

    wip_issues = [{"number": 200, "title": "Already working"}]
    claims = await detect_stale_claims(wip_issues, tmp_path)

    assert claims == []


@pytest.mark.anyio
async def test_detect_stale_claims_returns_sorted(tmp_path: Path) -> None:
    """detect_stale_claims() returns results sorted ascending by issue number."""
    # Neither worktree exists — both should be stale.
    wip_issues = [
        {"number": 300, "title": "Third"},
        {"number": 100, "title": "First"},
        {"number": 200, "title": "Second"},
    ]
    claims = await detect_stale_claims(wip_issues, tmp_path)

    assert [c.issue_number for c in claims] == [100, 200, 300]


@pytest.mark.anyio
async def test_detect_stale_claims_skips_non_int_number(tmp_path: Path) -> None:
    """detect_stale_claims() should skip issues where number is not an int."""
    wip_issues: list[dict[str, object]] = [{"number": "not-a-number", "title": "Bad issue"}]
    claims = await detect_stale_claims(wip_issues, tmp_path)

    assert claims == []


# ---------------------------------------------------------------------------
# detect_alerts() integration — stale claims appear in alerts
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_claim_shows_in_alerts(tmp_path: Path) -> None:
    """Stale claims from guards.detect_stale_claims() must appear in PipelineState alerts.

    Verifies the full path: detect_alerts() calls detect_stale_claims() and
    converts each StaleClaim into a human-readable string in the alerts list.
    The structured stale_claims list is also returned alongside alerts.
    """
    board = GitHubBoard(
        active_label="agentception/4-intelligence",
        open_issues=[],
        open_prs=[],
        wip_issues=[{"number": 42, "title": "Stale issue"}],
    )
    # No worktrees — issue 42 has no live worktree → stale claim expected.
    worktrees: list[TaskFile] = []

    with (
        patch("agentception.poller.settings") as poller_mock,
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        poller_mock.worktrees_dir = tmp_path
        alerts, stale_claims = await detect_alerts(worktrees, board)

    assert any("Stale claim on #42" in a for a in alerts), (
        f"Expected 'Stale claim on #42' in alerts, got: {alerts}"
    )
    assert len(stale_claims) == 1
    assert stale_claims[0].issue_number == 42
    assert stale_claims[0].issue_title == "Stale issue"


# ---------------------------------------------------------------------------
# clear_stale_claim endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_clear_stale_claim_removes_label() -> None:
    """POST /api/intelligence/stale-claims/{n}/clear should call clear_wip_label."""
    with patch(
        "agentception.routes.intelligence.clear_wip_label",
        new_callable=AsyncMock,
    ) as mock_clear:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/intelligence/stale-claims/99/clear")

    assert response.status_code == 200
    assert response.json() == {"cleared": 99}
    mock_clear.assert_awaited_once_with(99)


@pytest.mark.anyio
async def test_clear_stale_claim_returns_500_on_gh_failure() -> None:
    """POST /api/intelligence/stale-claims/{n}/clear should return 500 when gh fails."""
    with patch(
        "agentception.routes.intelligence.clear_wip_label",
        new_callable=AsyncMock,
        side_effect=RuntimeError("gh auth error"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/intelligence/stale-claims/55/clear")

    assert response.status_code == 500
    assert "gh auth error" in response.json()["detail"]
