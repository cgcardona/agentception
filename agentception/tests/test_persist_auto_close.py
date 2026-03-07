from __future__ import annotations

"""Regression tests for the merged-PR auto-close logic in db/persist.py.

Root cause: agents open PRs against the ``dev`` branch, not ``main``.
GitHub's native auto-close keyword ("Closes #N") only fires on merges into the
default branch.  Issues whose PRs were merged into ``dev`` stayed open forever.

Fix: ``_auto_close_pr_linked_issues`` detects two linkage signals—
  1. ``agent_runs.pr_number`` ↔ ``agent_runs.issue_number``
  2. ``pull_requests.closes_issue_number`` (parsed from PR body)
—and marks the issue ``closed`` in the DB, then fires ``gh issue close`` in the
background so GitHub stays in sync.

``_upsert_prs`` now also parses the first "Closes/Fixes/Resolves #N" keyword
from the PR body and stores it in ``closes_issue_number``.

Run:
    docker compose exec agentception pytest \
        agentception/tests/test_persist_auto_close.py -v
"""

import asyncio
import datetime
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import agentception.db.persist as _persist
from agentception.db.models import ACAgentRun, ACIssue, ACPullRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REPO = "cgcardona/agentception"
_UTC = datetime.timezone.utc


def _open_issue(number: int) -> ACIssue:
    return ACIssue(
        github_number=number,
        repo=_REPO,
        title=f"Issue #{number}",
        state="open",
        labels_json="[]",
        content_hash="aaa",
        first_seen_at=datetime.datetime.now(_UTC),
        last_synced_at=datetime.datetime.now(_UTC),
    )


def _merged_pr(pr_number: int, closes: int | None = None) -> ACPullRequest:
    return ACPullRequest(
        github_number=pr_number,
        repo=_REPO,
        title=f"PR #{pr_number}",
        state="merged",
        merged_at=datetime.datetime.now(_UTC),
        closes_issue_number=closes,
        labels_json="[]",
        content_hash="bbb",
        first_seen_at=datetime.datetime.now(_UTC),
        last_synced_at=datetime.datetime.now(_UTC),
    )


def _agent_run(issue_number: int, pr_number: int) -> ACAgentRun:
    return ACAgentRun(
        id=f"run-issue-{issue_number}",
        role="python-developer",
        status="done",
        issue_number=issue_number,
        pr_number=pr_number,
        spawned_at=datetime.datetime.now(_UTC),
    )


# ---------------------------------------------------------------------------
# _upsert_prs — closes_issue_number parsing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upsert_prs_parses_closes_keyword() -> None:
    """_upsert_prs extracts 'Closes #N' from PR body into closes_issue_number."""
    raw_pr: dict[str, object] = {
        "number": 201,
        "title": "fix: something",
        "state": "merged",
        "headRefName": "feat/fix",
        "labels": [],
        "mergedAt": None,
        "body": "## Summary\n\nCloses #99\n\nsome description",
    }

    session = MagicMock(spec=AsyncSession)
    # No existing PR row.
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=existing_result)
    session.add = MagicMock()

    await _persist._upsert_prs(session, [raw_pr], _REPO)

    added = session.add.call_args[0][0]
    assert isinstance(added, ACPullRequest)
    assert added.closes_issue_number == 99


@pytest.mark.anyio
async def test_upsert_prs_parses_fixes_keyword() -> None:
    """_upsert_prs recognises 'Fixes #N' as a closing keyword."""
    raw_pr: dict[str, object] = {
        "number": 202,
        "title": "fix: another",
        "state": "open",
        "headRefName": "feat/another",
        "labels": [],
        "mergedAt": None,
        "body": "Fixes #77",
    }

    session = MagicMock(spec=AsyncSession)
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=existing_result)
    session.add = MagicMock()

    await _persist._upsert_prs(session, [raw_pr], _REPO)

    added = session.add.call_args[0][0]
    assert added.closes_issue_number == 77


@pytest.mark.anyio
async def test_upsert_prs_no_closing_keyword_leaves_null() -> None:
    """_upsert_prs sets closes_issue_number=None when no keyword is present."""
    raw_pr: dict[str, object] = {
        "number": 203,
        "title": "chore: docs",
        "state": "open",
        "headRefName": "chore/docs",
        "labels": [],
        "mergedAt": None,
        "body": "Just updating the README.",
    }

    session = MagicMock(spec=AsyncSession)
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=existing_result)
    session.add = MagicMock()

    await _persist._upsert_prs(session, [raw_pr], _REPO)

    added = session.add.call_args[0][0]
    assert added.closes_issue_number is None


# ---------------------------------------------------------------------------
# _auto_close_pr_linked_issues — DB + GitHub close
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_auto_close_via_agent_runs_closes_issue_in_db() -> None:
    """Issues linked to merged PRs via agent_runs are marked closed in the DB."""
    issue = _open_issue(41)
    pr = _merged_pr(133)
    run = _agent_run(41, 133)

    # First execute: agent_runs → issues to close (method 1)
    run_result = MagicMock()
    run_result.all.return_value = [MagicMock(issue_number=41)]

    # Second execute: closes_issue_number → issues to close (method 2)
    pr_body_result = MagicMock()
    pr_body_result.all.return_value = []

    # Third execute: look up the ACIssue row for issue #41
    issue_result = MagicMock()
    issue_result.scalar_one_or_none.return_value = issue

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[run_result, pr_body_result, issue_result])

    with patch.object(_persist, "_gh_close_issue", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda c: asyncio.create_task(c)):
        await _persist._auto_close_pr_linked_issues(session, _REPO)
        await asyncio.sleep(0)  # yield so scheduled _gh_close_issue runs

    assert issue.state == "closed", "Issue state must be updated to closed in the DB"
    assert issue.closed_at is not None, "closed_at must be stamped"


@pytest.mark.anyio
async def test_auto_close_via_pr_body_closes_issue_in_db() -> None:
    """Issues linked via closes_issue_number (PR body) are marked closed."""
    issue = _open_issue(46)

    # Method 1 (agent_runs): no results
    run_result = MagicMock()
    run_result.all.return_value = []

    # Method 2 (PR body): PR #136 closes issue #46
    pr_body_result = MagicMock()
    pr_body_result.all.return_value = [MagicMock(closes_issue_number=46)]

    # Issue lookup
    issue_result = MagicMock()
    issue_result.scalar_one_or_none.return_value = issue

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[run_result, pr_body_result, issue_result])

    with patch.object(_persist, "_gh_close_issue", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda c: asyncio.create_task(c)):
        await _persist._auto_close_pr_linked_issues(session, _REPO)
        await asyncio.sleep(0)  # yield so scheduled _gh_close_issue runs

    assert issue.state == "closed"


@pytest.mark.anyio
async def test_auto_close_no_op_when_no_merged_prs() -> None:
    """_auto_close_pr_linked_issues is a no-op when nothing qualifies."""
    run_result = MagicMock()
    run_result.all.return_value = []

    pr_body_result = MagicMock()
    pr_body_result.all.return_value = []

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[run_result, pr_body_result])

    with patch("asyncio.ensure_future") as mock_future:
        await _persist._auto_close_pr_linked_issues(session, _REPO)

    mock_future.assert_not_called()


@pytest.mark.anyio
async def test_auto_close_updates_content_hash_to_prevent_reopen() -> None:
    """content_hash is updated to 'closed' value so the next upsert can't re-open."""
    issue = _open_issue(41)
    original_hash = issue.content_hash

    run_result = MagicMock()
    run_result.all.return_value = [MagicMock(issue_number=41)]

    pr_body_result = MagicMock()
    pr_body_result.all.return_value = []

    issue_result = MagicMock()
    issue_result.scalar_one_or_none.return_value = issue

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[run_result, pr_body_result, issue_result])

    with patch.object(_persist, "_gh_close_issue", new_callable=AsyncMock), \
         patch("asyncio.ensure_future", side_effect=lambda c: asyncio.create_task(c)):
        await _persist._auto_close_pr_linked_issues(session, _REPO)
        await asyncio.sleep(0)  # yield so scheduled _gh_close_issue runs

    assert issue.content_hash != original_hash, (
        "content_hash must be recomputed with state='closed' "
        "so the next open-issue upsert doesn't flip the issue back to open"
    )


# ---------------------------------------------------------------------------
# _parse_blocked_by — regression for depends_on_json backfill from body
# ---------------------------------------------------------------------------


def test_parse_blocked_by_single_dep() -> None:
    """Extracts a single blocker number from a '**Blocked by:** #N' line."""
    body = "Some description.\n\n---\n**Blocked by:** #175"
    assert _persist._parse_blocked_by(body) == [175]


def test_parse_blocked_by_multiple_deps() -> None:
    """Extracts multiple blocker numbers separated by commas."""
    body = "Description.\n\n---\n**Blocked by:** #175, #176"
    assert _persist._parse_blocked_by(body) == [175, 176]


def test_parse_blocked_by_no_match_returns_empty() -> None:
    """Returns [] when the body has no 'Blocked by' line."""
    assert _persist._parse_blocked_by("Just a plain description.") == []
    assert _persist._parse_blocked_by("") == []


def test_parse_blocked_by_does_not_match_partial() -> None:
    """Does not false-positive on partial matches like 'blocked by' (lowercase)."""
    body = "This is blocked by someone but not in the right format."
    assert _persist._parse_blocked_by(body) == []
