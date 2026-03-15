from __future__ import annotations

"""Tests for agentception.db.queries.get_batch_summaries_for_initiative.

Pure-unit tests that mock the DB session so no live Postgres connection is
required.  We verify the importability, TypedDict contract, and that the
function returns an empty list gracefully when passed no issue numbers or
when the DB raises.

Run targeted:
    pytest agentception/tests/test_batch_summaries.py -v
"""

import pytest

from agentception.db.queries import (
    BatchSummaryRow,
    get_batch_summaries_for_initiative,
)


# ── Importability and type-contract tests ─────────────────────────────────────


def test_batch_summary_row_importable() -> None:
    """BatchSummaryRow TypedDict is importable from agentception.db.queries."""
    assert BatchSummaryRow is not None


def test_get_batch_summaries_for_initiative_importable() -> None:
    """get_batch_summaries_for_initiative function is importable and callable."""
    assert callable(get_batch_summaries_for_initiative)


def test_batch_summary_row_constructor() -> None:
    """BatchSummaryRow can be constructed as a plain dict with the correct keys."""
    row: BatchSummaryRow = {
        "batch_id": "batch-abc",
        "spawned_at": "2026-01-01T00:00:00",
        "total_count": 5,
        "active_count": 2,
    }
    assert row["batch_id"] == "batch-abc"
    assert row["total_count"] == 5
    assert row["active_count"] == 2
    assert "spawned_at" in row


# ── Behaviour: empty issue_numbers → returns [] immediately ──────────────────


@pytest.mark.anyio
async def test_get_batch_summaries_returns_empty_for_no_issue_numbers() -> None:
    """get_batch_summaries_for_initiative returns [] without hitting the DB when
    issue_numbers is empty."""
    result = await get_batch_summaries_for_initiative("owner/repo", [])
    assert result == []


# ── Behaviour: DB error → returns [] non-fatally ─────────────────────────────


@pytest.mark.anyio
async def test_get_batch_summaries_returns_empty_on_db_error() -> None:
    """get_batch_summaries_for_initiative returns [] on DB failure (non-fatal)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    class _FailingSession:
        async def __aenter__(self) -> "_FailingSession":
            return self

        async def __aexit__(self, *_: str | int | bool | float | None) -> None:
            pass

        async def execute(self, *_: str | int | bool | float | None) -> None:
            raise RuntimeError("DB unavailable")

    with patch(
        "agentception.db.queries.runs.get_session",
        return_value=_FailingSession(),
    ):
        result = await get_batch_summaries_for_initiative("owner/repo", [42, 43])

    assert result == []
