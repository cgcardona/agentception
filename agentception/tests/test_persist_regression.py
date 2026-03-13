from __future__ import annotations

"""Regression tests for the DB persistence layer.

These tests lock in two specific behaviours that were broken before the
phase-0 reader fixes:

1. ``_upsert_issues`` must update ``ACIssue.state`` to ``"closed"`` when a
   previously-open issue is upserted with ``state="closed"``.

2. ``get_initiatives()`` must exclude initiative slugs whose only issues are
   closed — stale ``state="open"`` rows must not keep a tab alive.
"""

import datetime
import json
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from agentception.db.base import Base
from agentception.db.models import ACIssue, ACInitiativePhase
from agentception.db.persist import _upsert_issues
from agentception.db.queries import get_initiatives


# ---------------------------------------------------------------------------
# Helpers shared by both tests
# ---------------------------------------------------------------------------


def _utc(offset_seconds: int = 0) -> datetime.datetime:
    """Return a UTC datetime offset by *offset_seconds* from the epoch."""
    return datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
        seconds=offset_seconds
    )


async def _make_in_memory_session() -> tuple[AsyncSession, AsyncEngine]:
    """Create a fresh in-memory SQLite engine + session with all tables.

    Returns ``(session, engine)`` so the caller can dispose the engine after
    the test.  Using ``aiosqlite`` keeps the test self-contained — no Postgres
    required.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    return session, engine


# ---------------------------------------------------------------------------
# Test 1: stale open row is updated to closed after upsert
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_issue_clears_after_close() -> None:
    """ACIssue.state must become 'closed' after upserting a previously-open issue with state='closed'.

    Regression guard: before the phase-0 fix, _upsert_issues was never called
    with closed-issue records for bulk-closed issues, leaving stale state='open'
    rows in the DB and keeping initiative tabs alive indefinitely.
    """
    session, engine = await _make_in_memory_session()
    repo = "owner/repo"
    issue_number = 42

    try:
        # Step 1: insert an open issue directly into the DB.
        now = datetime.datetime.now(datetime.timezone.utc)
        open_issue = ACIssue(
            github_number=issue_number,
            repo=repo,
            title="My open issue",
            body=None,
            state="open",
            phase_label=None,
            labels_json='["mcp-audit-remediation/0-foundation"]',
            depends_on_json="[]",
            content_hash="aaaaaa",
            closed_at=None,
            first_seen_at=now,
            last_synced_at=now,
        )
        session.add(open_issue)
        await session.commit()

        # Step 2: upsert the same issue with state="closed".
        closed_record: dict[str, object] = {
            "number": issue_number,
            "title": "My open issue",
            "state": "closed",
            "labels": ["mcp-audit-remediation/0-foundation"],
            "closedAt": "2024-06-01T12:00:00Z",
        }
        await _upsert_issues(session, [closed_record], None, repo)
        await session.commit()

        # Step 3: re-fetch and assert state is now "closed".
        from sqlalchemy import select

        result = await session.execute(
            select(ACIssue).where(
                ACIssue.github_number == issue_number,
                ACIssue.repo == repo,
            )
        )
        row = result.scalar_one()
        assert row.state == "closed", (
            f"Expected state='closed' after upsert, got state={row.state!r}"
        )
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Test 2: get_initiatives excludes slugs whose only issues are closed
# ---------------------------------------------------------------------------


def _phase_rows(pairs: list[tuple[str, datetime.datetime]]) -> MagicMock:
    """Build a mock execute result for the initiative_phases query."""
    rows = []
    for initiative, ts in pairs:
        row = MagicMock()
        row.initiative = initiative
        row.last_filed = ts
        rows.append(row)
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    return result


def _issue_rows(label_lists: list[list[str]]) -> MagicMock:
    """Build a mock execute result for the open-issues labels query.

    Each entry in *label_lists* is the list of labels for one open issue.
    The mock returns rows whose first element is the JSON-encoded label list,
    matching the ``select(ACIssue.labels_json)`` shape used by get_initiatives.
    """
    rows = [(json.dumps(labels),) for labels in label_lists]
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    return result


def _mock_two_query_session(
    phase_result: MagicMock,
    issue_result: MagicMock,
) -> object:
    """Return a ``get_session`` replacement that serves two sequential queries.

    The first ``async with get_session()`` call returns a session that yields
    *phase_result*; the second yields *issue_result*.  This mirrors the two
    ``async with get_session()`` blocks inside ``get_initiatives``.
    """
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


@pytest.mark.anyio
async def test_get_initiatives_excludes_closed() -> None:
    """get_initiatives() must return [] when all issues for an initiative are closed.

    Regression guard: if ACIssue rows are stale (state='open' when the issue is
    actually closed on GitHub), get_initiatives would incorrectly include the
    initiative slug in the tab bar.  This test verifies the query correctly
    excludes initiatives whose only issues are closed (i.e. not returned by the
    open-issues query).
    """
    initiative_slug = "mcp-audit-remediation"
    # The initiative exists in initiative_phases (it was filed).
    phases = _phase_rows([(initiative_slug, _utc(100))])
    # But there are NO open issues with a scoped label like
    # "mcp-audit-remediation/0-foundation" — all issues are closed.
    issues = _issue_rows([])

    with patch("agentception.db.queries.board.get_session", _mock_two_query_session(phases, issues)):
        result = await get_initiatives("owner/repo")

    assert result == [], (
        f"Expected get_initiatives to return [] when all issues are closed, "
        f"got {result!r}"
    )
    assert initiative_slug not in result
