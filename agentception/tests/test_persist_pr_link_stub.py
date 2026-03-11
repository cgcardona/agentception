from __future__ import annotations

"""Regression tests for persist_pr_link_and_recompute stub ACPullRequest insertion.

INV-RUN-PR-1 fires when agent_runs.pr_number references a PR not yet in
pull_requests.  The root cause: the poller is the only path that inserts
ACPullRequest rows, so between agent completion and the next poller tick the
invariant fires on every poller check.

Fix: persist_pr_link_and_recompute now upserts a stub ACPullRequest row so
INV-RUN-PR-1 is satisfied immediately.  The poller overwrites the stub with
real GitHub data on its next tick via the normal content-hash diff path.

Run:
    docker compose exec agentception pytest \
        agentception/tests/test_persist_pr_link_stub.py -v
"""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agentception.db.models import ACPRIssueLink, ACPullRequest
from agentception.db.persist import persist_pr_link_and_recompute


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scalar_none() -> MagicMock:
    """Return a mock execute-result whose scalar_one_or_none() returns None."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=None)
    return r


def _mock_session_factory(
    existing_link: ACPRIssueLink | None = None,
    existing_pr: ACPullRequest | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a mock async session that returns the given existing rows.

    Returns (session_mock, context_manager_mock) so callers can inspect
    session.add calls.
    """
    link_result = MagicMock()
    link_result.scalar_one_or_none = MagicMock(return_value=existing_link)

    pr_result = MagicMock()
    pr_result.scalar_one_or_none = MagicMock(return_value=existing_pr)

    # First execute → link query; second execute → PR query.
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[link_result, pr_result])
    session.add = MagicMock()
    session.commit = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return session, ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stub_pr_inserted_when_no_existing_pr() -> None:
    """A stub ACPullRequest row is added when no PR row exists yet."""
    session, ctx = _mock_session_factory(existing_link=None, existing_pr=None)

    with (
        patch("agentception.db.persist.get_session", return_value=ctx),
        patch(
            "agentception.db.persist._recompute_workflow_state", new_callable=AsyncMock
        ),
    ):
        await persist_pr_link_and_recompute(
            pr_number=525, issue_number=444, gh_repo="owner/repo"
        )

    added_types = [type(call_args[0][0]) for call_args in session.add.call_args_list]
    assert ACPullRequest in added_types, (
        "Expected a stub ACPullRequest to be added but it was not"
    )


@pytest.mark.anyio
async def test_stub_pr_not_duplicated_when_pr_already_exists() -> None:
    """If ACPullRequest already exists (poller ran first), no duplicate is inserted."""
    existing_pr = MagicMock(spec=ACPullRequest)
    session, ctx = _mock_session_factory(existing_link=None, existing_pr=existing_pr)

    with (
        patch("agentception.db.persist.get_session", return_value=ctx),
        patch(
            "agentception.db.persist._recompute_workflow_state", new_callable=AsyncMock
        ),
    ):
        await persist_pr_link_and_recompute(
            pr_number=525, issue_number=444, gh_repo="owner/repo"
        )

    added_types = [type(call_args[0][0]) for call_args in session.add.call_args_list]
    assert ACPullRequest not in added_types, (
        "Expected no ACPullRequest insert when row already exists"
    )


@pytest.mark.anyio
async def test_stub_pr_has_correct_fields() -> None:
    """The stub row links the correct PR number, repo, and closes_issue_number."""
    session, ctx = _mock_session_factory(existing_link=None, existing_pr=None)

    with (
        patch("agentception.db.persist.get_session", return_value=ctx),
        patch(
            "agentception.db.persist._recompute_workflow_state", new_callable=AsyncMock
        ),
    ):
        await persist_pr_link_and_recompute(
            pr_number=525, issue_number=444, gh_repo="owner/repo"
        )

    pr_calls = [
        call_args[0][0]
        for call_args in session.add.call_args_list
        if isinstance(call_args[0][0], ACPullRequest)
    ]
    assert len(pr_calls) == 1
    stub = pr_calls[0]
    assert stub.github_number == 525
    assert stub.repo == "owner/repo"
    assert stub.closes_issue_number == 444
    assert stub.state == "open"
    assert stub.base_ref == "dev"
