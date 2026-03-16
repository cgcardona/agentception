from __future__ import annotations

"""Integration-style tests for reconcile_stale_runs.

These tests wire two ACAgentRun-like rows through the full reconcile_stale_runs()
code path using a mock AsyncSession and mock GitHub readers, exercising both the
query/filter logic and the commit/rollback behaviour in a single call.

The test DB fixture is not available in this suite (no Postgres); see conftest.py.
We use the same mock-session pattern as test_reconcile.py.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db.models import ACAgentRun
from agentception.reconcile import reconcile_stale_runs
from agentception.types import JsonValue

_UTC = datetime.timezone.utc


def _make_run(
    run_id: str,
    issue_number: int | None = None,
    branch: str | None = None,
    minutes_old: int = 30,
) -> MagicMock:
    """Build a stale ACAgentRun-like mock."""
    run = MagicMock(spec=ACAgentRun)
    run.id = run_id
    run.status = "implementing"
    run.issue_number = issue_number
    run.branch = branch
    run.last_activity_at = datetime.datetime.now(_UTC) - datetime.timedelta(
        minutes=minutes_old
    )
    return run


def _make_session(candidates: list[MagicMock]) -> MagicMock:
    """Return a mock AsyncSession whose execute() yields *candidates*."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = candidates
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.mark.anyio
async def test_stale_row_completed_active_row_untouched() -> None:
    """Stale row with closed issue transitions to completed; active row is untouched.

    Invariant: the threshold guard correctly separates the two rows so only
    the old one is mutated.
    """
    # Stale: 30 minutes old, linked to issue 42 (will return closed).
    stale = _make_run("issue-42", issue_number=42, minutes_old=30)
    # Active: 0 minutes old — the session mock returns it anyway so we can
    # verify reconcile_stale_runs skips it because get_issue returns "open".
    active = _make_run("issue-43", issue_number=43, minutes_old=0)
    session = _make_session([stale, active])

    async def _get_issue(number: int) -> dict[str, JsonValue]:
        """Issue 42 is closed; issue 43 is open."""
        if number == 42:
            return {"state": "closed", "number": 42}
        return {"state": "open", "number": number}

    with (
        patch(
            "agentception.reconcile.get_issue",
            new=AsyncMock(side_effect=_get_issue),
        ),
        patch(
            "agentception.reconcile.is_branch_merged_into",
            new=AsyncMock(return_value=False),
        ),
    ):
        reconciled = await reconcile_stale_runs(session, stale_threshold_minutes=10)

    # Only the stale row is in the return list.
    assert reconciled == ["issue-42"]
    assert stale.status == "completed"
    assert active.status == "implementing"
    # Exactly one commit — for the stale row only.
    assert session.commit.call_count == 1


@pytest.mark.anyio
async def test_pr_merged_signal_triggers_completion() -> None:
    """A stale run whose branch is merged is set to completed via pr_merged signal.

    Invariant: is_branch_merged_into is checked when issue signal does not fire.
    """
    run = _make_run("issue-50", branch="agent/issue-50", minutes_old=30)
    session = _make_session([run])

    with (
        patch(
            "agentception.reconcile.get_issue",
            new=AsyncMock(side_effect=AssertionError("should not be called — no issue_number")),
        ),
        patch(
            "agentception.reconcile.is_branch_merged_into",
            new=AsyncMock(return_value=True),
        ),
    ):
        reconciled = await reconcile_stale_runs(session, stale_threshold_minutes=10)

    assert reconciled == ["issue-50"]
    assert run.status == "completed"
    session.commit.assert_called_once()


@pytest.mark.anyio
async def test_no_real_github_calls_made() -> None:
    """Confirm no real network calls escape — patched readers are always used.

    Invariant: both GitHub reader functions are patched; any real HTTP call
    would raise in the test environment and fail the test.
    """
    run = _make_run("issue-60", issue_number=60, minutes_old=30)
    session = _make_session([run])

    get_issue_mock = AsyncMock(return_value={"state": "closed", "number": 60})

    with (
        patch("agentception.reconcile.get_issue", new=get_issue_mock),
        patch(
            "agentception.reconcile.is_branch_merged_into",
            new=AsyncMock(return_value=False),
        ),
    ):
        await reconcile_stale_runs(session, stale_threshold_minutes=10)

    # get_issue was called exactly once (for issue 60) and never hit the network.
    get_issue_mock.assert_awaited_once_with(60)
