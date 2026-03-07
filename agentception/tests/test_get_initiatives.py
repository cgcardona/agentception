from __future__ import annotations

"""Unit tests for ``get_initiatives()`` — derives tabs from ``initiative_phases``
+ open-issues filter with no JSON configuration required.

Run targeted:
    pytest agentception/tests/test_get_initiatives.py -v
"""

import datetime
import json
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from typing import AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.queries import get_initiatives


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(offset_seconds: float = 0.0) -> datetime.datetime:
    return datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC) + datetime.timedelta(
        seconds=offset_seconds
    )


def _phase_rows(
    rows: list[tuple[str, datetime.datetime]],
) -> MagicMock:
    """Build a result mock for the initiative_phases query.

    Each tuple is ``(initiative, last_filed_dt)``.
    """
    result_mock = MagicMock()
    result_mock.all.return_value = [
        MagicMock(initiative=ini, last_filed=dt) for ini, dt in rows
    ]
    return result_mock


def _issue_rows(labels_list: list[list[str]]) -> MagicMock:
    """Build a result mock for the open-issues labels query."""
    result_mock = MagicMock()
    result_mock.all.return_value = [
        (json.dumps(lbls),) for lbls in labels_list
    ]
    return result_mock


def _mock_two_query_session(
    phase_result: MagicMock,
    issue_result: MagicMock,
) -> Callable[[], AbstractAsyncContextManager[AsyncMock]]:
    """Return a ``get_session`` factory that yields successive results per call."""
    call_count = 0

    @asynccontextmanager
    async def _ctx() -> AsyncIterator[AsyncMock]:
        nonlocal call_count
        session_mock = AsyncMock()
        if call_count == 0:
            session_mock.execute = AsyncMock(return_value=phase_result)
        else:
            session_mock.execute = AsyncMock(return_value=issue_result)
        call_count += 1
        yield session_mock

    return _ctx


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_initiatives_returns_filed_with_open_issues() -> None:
    """Initiative in initiative_phases with open phased issues → included."""
    phases = _phase_rows([("auth-rewrite", _utc(100))])
    issues = _issue_rows([["auth-rewrite", "auth-rewrite/0-foundation"]])

    with patch("agentception.db.queries.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == ["auth-rewrite"]


@pytest.mark.anyio
async def test_get_initiatives_excludes_when_all_issues_closed() -> None:
    """Initiative filed but all issues closed → excluded (tab auto-hides)."""
    phases = _phase_rows([("auth-rewrite", _utc(100))])
    # No open issues with an auth-rewrite/ prefix.
    issues = _issue_rows([])

    with patch("agentception.db.queries.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == []


@pytest.mark.anyio
async def test_get_initiatives_excludes_when_no_scoped_phase_label() -> None:
    """Open issues without a scoped phase label (e.g. only top-level label) are not counted."""
    phases = _phase_rows([("auth-rewrite", _utc(100))])
    # Issue carries the initiative label but no scoped phase label — won't show on board.
    issues = _issue_rows([["auth-rewrite"]])

    with patch("agentception.db.queries.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == []


@pytest.mark.anyio
async def test_get_initiatives_not_in_phases_never_appears() -> None:
    """An initiative with open issues but NOT in initiative_phases → excluded."""
    phases = _phase_rows([])  # nothing filed
    issues = _issue_rows([["mystery-initiative", "mystery-initiative/0-first"]])

    with patch("agentception.db.queries.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == []


@pytest.mark.anyio
async def test_get_initiatives_ordered_most_recently_filed_first() -> None:
    """Tabs are ordered by most-recently-filed batch DESC."""
    phases = _phase_rows([
        ("ac-plan", _utc(300)),    # most recent
        ("ac-build", _utc(200)),
        ("ac-workflow", _utc(100)),  # oldest
    ])
    issues = _issue_rows([
        ["ac-plan", "ac-plan/0-scaffold"],
        ["ac-build", "ac-build/0-scaffold"],
        ["ac-workflow", "ac-workflow/0-scaffold"],
    ])

    with patch("agentception.db.queries.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == ["ac-plan", "ac-build", "ac-workflow"]


@pytest.mark.anyio
async def test_get_initiatives_mixed_open_and_closed() -> None:
    """Only initiatives that still have at least one open phased issue are returned."""
    phases = _phase_rows([
        ("ac-plan", _utc(200)),
        ("ac-done", _utc(100)),
    ])
    issues = _issue_rows([
        # ac-plan has an open issue.
        ["ac-plan", "ac-plan/0-scaffold"],
        # ac-done has no open issues (all closed, not in this list).
    ])

    with patch("agentception.db.queries.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == ["ac-plan"]
    assert "ac-done" not in result


@pytest.mark.anyio
async def test_get_initiatives_empty_phases_returns_empty() -> None:
    """No filings at all → no tabs, regardless of open issues."""
    phases = _phase_rows([])
    issues = _issue_rows([["any-label", "any-label/0-phase"]])

    with patch("agentception.db.queries.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == []


@pytest.mark.anyio
async def test_get_initiatives_db_error_returns_empty() -> None:
    """A DB error is caught and returns [] (non-fatal degradation)."""
    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("DB unavailable"))
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.db.queries.get_session", return_value=ctx_mock):
        result = await get_initiatives("owner/repo")

    assert result == []
