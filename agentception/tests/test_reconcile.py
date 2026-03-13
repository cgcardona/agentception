from __future__ import annotations

"""Tests for agentception.reconcile — stale-run reconciliation."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db.models import ACAgentRun
from agentception.reconcile import reconcile_stale_runs

_UTC = datetime.timezone.utc


def _make_run(
    run_id: str = "issue-999",
    status: str = "implementing",
    issue_number: int | None = None,
    branch: str | None = None,
    last_activity_at: datetime.datetime | None = None,
) -> MagicMock:
    """Build a minimal ACAgentRun-like mock for testing."""
    run = MagicMock(spec=ACAgentRun)
    run.id = run_id
    run.status = status
    run.issue_number = issue_number
    run.branch = branch
    run.last_activity_at = last_activity_at or (
        datetime.datetime.now(_UTC) - datetime.timedelta(minutes=30)
    )
    return run


def _make_session(candidates: list[MagicMock]) -> MagicMock:
    """Return a mock AsyncSession whose execute() returns *candidates*."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = candidates

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# test_skips_recent_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_skips_recent_run() -> None:
    """A run with last_activity_at = utcnow() must not be mutated.

    The query filters by cutoff time; we simulate this by returning an empty
    candidate list (the DB would exclude the recent run).
    """
    # The session returns no candidates — simulating the WHERE clause filtering
    # out the recent run.
    session = _make_session([])

    reconciled = await reconcile_stale_runs(session, stale_threshold_minutes=10)

    assert reconciled == []
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# test_reconciles_on_closed_issue
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciles_on_closed_issue(caplog: pytest.LogCaptureFixture) -> None:
    """A stale run whose linked issue is closed must be set to completed."""
    run = _make_run(run_id="issue-100", issue_number=100)
    session = _make_session([run])

    with patch(
        "agentception.reconcile.get_issue",
        new=AsyncMock(return_value={"state": "closed", "number": 100}),
    ), patch(
        "agentception.reconcile.is_branch_merged_into",
        new=AsyncMock(return_value=False),
    ):
        import logging

        with caplog.at_level(logging.INFO, logger="agentception.reconcile"):
            reconciled = await reconcile_stale_runs(session, stale_threshold_minutes=10)

    assert reconciled == ["issue-100"]
    assert run.status == "completed"
    session.commit.assert_called_once()
    assert "signal=issue_closed" in caplog.text


# ---------------------------------------------------------------------------
# test_reconciles_on_merged_pr
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reconciles_on_merged_pr(caplog: pytest.LogCaptureFixture) -> None:
    """A stale run whose branch is merged into dev must be set to completed."""
    run = _make_run(run_id="issue-200", branch="feat/issue-200")
    session = _make_session([run])

    with patch(
        "agentception.reconcile.get_issue",
        new=AsyncMock(return_value={"state": "open", "number": None}),
    ), patch(
        "agentception.reconcile.is_branch_merged_into",
        new=AsyncMock(return_value=True),
    ):
        import logging

        with caplog.at_level(logging.INFO, logger="agentception.reconcile"):
            reconciled = await reconcile_stale_runs(session, stale_threshold_minutes=10)

    assert reconciled == ["issue-200"]
    assert run.status == "completed"
    session.commit.assert_called_once()
    assert "signal=pr_merged" in caplog.text


# ---------------------------------------------------------------------------
# test_skips_run_with_no_signals
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_skips_run_with_no_signals() -> None:
    """A stale run with no issue_number and no branch must not be mutated."""
    run = _make_run(run_id="issue-300", issue_number=None, branch=None)
    session = _make_session([run])

    with patch(
        "agentception.reconcile.get_issue",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ), patch(
        "agentception.reconcile.is_branch_merged_into",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        reconciled = await reconcile_stale_runs(session, stale_threshold_minutes=10)

    assert reconciled == []
    assert run.status == "implementing"
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# test_partial_github_failure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_partial_github_failure() -> None:
    """A GitHub API error on the second run must not prevent the first from committing.

    The first run has a closed issue and should be committed.
    The second run raises an exception from get_issue — it should be skipped
    but the first run's commit must already have happened.
    """
    run1 = _make_run(run_id="issue-401", issue_number=401)
    run2 = _make_run(run_id="issue-402", issue_number=402)
    session = _make_session([run1, run2])

    call_count = 0

    async def _get_issue_side_effect(number: int) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if number == 401:
            return {"state": "closed", "number": 401}
        raise RuntimeError("GitHub API timeout")

    with patch(
        "agentception.reconcile.get_issue",
        new=AsyncMock(side_effect=_get_issue_side_effect),
    ), patch(
        "agentception.reconcile.is_branch_merged_into",
        new=AsyncMock(return_value=False),
    ):
        reconciled = await reconcile_stale_runs(session, stale_threshold_minutes=10)

    # Only run1 should be reconciled.
    assert reconciled == ["issue-401"]
    assert run1.status == "completed"
    # run2 had a GitHub error — no signal fired, so it stays implementing.
    assert run2.status == "implementing"
    # commit was called exactly once (for run1).
    assert session.commit.call_count == 1
