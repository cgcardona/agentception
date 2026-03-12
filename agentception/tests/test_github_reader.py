from __future__ import annotations

"""Tests for agentception/readers/github.py — focused on get_closed_issues."""

import pytest
from unittest.mock import AsyncMock, patch

from agentception.readers.github import get_closed_issues, _cache_invalidate


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    """Ensure each test starts with a cold cache."""
    _cache_invalidate()


@pytest.mark.anyio
async def test_get_closed_issues_default_limit() -> None:
    """Default limit is 1000 — 150 items returned by the API must all come back.

    Regression guard: the old limit=100 default would have truncated the result
    to 100 items.  This test fails if the default is ever lowered below 150.
    """
    # Build 150 fake closed issues (no pull_request key → all pass the filter).
    fake_issues = [{"number": i, "title": f"issue {i}", "state": "closed"} for i in range(150)]

    with patch(
        "agentception.readers.github._api_get_all",
        new_callable=AsyncMock,
        return_value=fake_issues,
    ) as mock_get_all:
        result = await get_closed_issues()

    assert len(result) == 150, (
        f"Expected 150 issues but got {len(result)} — "
        "default limit may have been lowered below 150"
    )
    # Confirm the call was made with the new default.
    mock_get_all.assert_called_once()
    _args, kwargs = mock_get_all.call_args
    assert kwargs.get("limit", _args[3] if len(_args) > 3 else None) == 1000


@pytest.mark.anyio
async def test_get_closed_issues_explicit_limit_respected() -> None:
    """Callers that pass limit=N receive at most N results."""
    fake_issues = [{"number": i, "state": "closed"} for i in range(50)]

    with patch(
        "agentception.readers.github._api_get_all",
        new_callable=AsyncMock,
        return_value=fake_issues,
    ) as mock_get_all:
        result = await get_closed_issues(limit=50)

    assert len(result) == 50
    mock_get_all.assert_called_once()
    _args, kwargs = mock_get_all.call_args
    assert kwargs.get("limit", _args[3] if len(_args) > 3 else None) == 50


@pytest.mark.anyio
async def test_get_closed_issues_filters_pull_requests() -> None:
    """Items with a ``pull_request`` key are excluded from the result."""
    fake_items = [
        {"number": 1, "state": "closed"},
        {"number": 2, "state": "closed", "pull_request": {"url": "https://..."}},
        {"number": 3, "state": "closed"},
    ]

    with patch(
        "agentception.readers.github._api_get_all",
        new_callable=AsyncMock,
        return_value=fake_items,
    ):
        result = await get_closed_issues()

    assert len(result) == 2
    assert all("pull_request" not in item for item in result)
