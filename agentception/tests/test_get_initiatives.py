from __future__ import annotations

"""Unit tests for ``get_initiatives()`` — both the config-driven (fnmatch)
path and the legacy ``phase-N`` heuristic fallback.

Run targeted:
    pytest agentception/tests/test_get_initiatives.py -v
"""

import json
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from typing import AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.queries import get_initiatives


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    labels: list[str], state: str = "open", phase_label: str | None = None
) -> tuple[str, str, str | None]:
    """Return a (labels_json, state, phase_label) tuple as the DB query now returns."""
    return json.dumps(labels), state, phase_label


def _mock_session_context(
    rows: list[tuple[str, str, str | None]],
) -> Callable[[], AbstractAsyncContextManager[AsyncMock]]:
    """Build a callable that, when called, returns an async context manager yielding a mock session."""
    result_mock = MagicMock()
    result_mock.all.return_value = rows

    session_mock = AsyncMock()
    session_mock.execute = AsyncMock(return_value=result_mock)

    @asynccontextmanager
    async def _ctx() -> AsyncIterator[AsyncMock]:
        yield session_mock

    return _ctx


# ---------------------------------------------------------------------------
# Config-driven path (initiative_patterns provided)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_initiatives_patterns_exact_match() -> None:
    """Labels that exactly match a pattern and have a scoped phase label are returned."""
    rows = [
        _make_row(["agentception", "agentception/phase-0"]),
        _make_row(["ac-plan", "ac-plan/phase-0"]),
        # This issue's initiative label matches but has no scoped phase label → excluded.
        _make_row(["ac-build"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives(
            "owner/repo", initiative_patterns=["agentception", "ac-plan", "ac-build"]
        )
    # Pattern order: agentception(0), ac-plan(1), ac-build(2)
    # ac-build issue has no scoped phase label → not shown.
    assert result == ["agentception", "ac-plan"]


@pytest.mark.anyio
async def test_get_initiatives_patterns_glob_match() -> None:
    """Glob patterns like ``ac-*`` match multiple initiative labels."""
    rows = [
        _make_row(["ac-build", "ac-build/phase-0"]),
        _make_row(["ac-workflow", "ac-workflow/phase-1"]),
        # ac-reliability has no scoped phase label → excluded from tabs.
        _make_row(["ac-reliability"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo", initiative_patterns=["ac-*"])
    assert "ac-build" in result
    assert "ac-workflow" in result
    assert "ac-reliability" not in result


@pytest.mark.anyio
async def test_get_initiatives_glob_never_matches_scoped_phase_labels() -> None:
    """``ac-*`` must not surface scoped phase labels (e.g. ``ac-build/phase-0``) as initiative tabs.

    Python's fnmatch ``*`` matches ``/``, so without the ``"/" not in lbl`` guard
    the pattern would match ``ac-build/phase-0`` and create a phantom tab.
    """
    rows = [
        _make_row(["ac-build", "ac-build/phase-0"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo", initiative_patterns=["ac-*"])
    assert result == ["ac-build"]
    assert "ac-build/phase-0" not in result


@pytest.mark.anyio
async def test_get_initiatives_patterns_excludes_closed_only() -> None:
    """An initiative only appearing on closed issues is not returned."""
    rows = [
        _make_row(["ac-build", "ac-build/phase-0"], state="closed"),
        _make_row(["ac-plan", "ac-plan/phase-0"], state="open"),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo", initiative_patterns=["ac-*"])
    assert "ac-plan" in result
    assert "ac-build" not in result


@pytest.mark.anyio
async def test_get_initiatives_patterns_returns_sorted() -> None:
    """Result order mirrors the position of each label's pattern in initiative_patterns."""
    rows = [
        _make_row(["ac-workflow", "ac-workflow/phase-0"]),
        _make_row(["ac-build", "ac-build/phase-0"]),
        _make_row(["ac-plan", "ac-plan/phase-0"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives(
            "owner/repo", initiative_patterns=["ac-build", "ac-plan", "ac-workflow"]
        )
    # Sorted by pattern position: ac-build(0) → ac-plan(1) → ac-workflow(2)
    assert result == ["ac-build", "ac-plan", "ac-workflow"]


@pytest.mark.anyio
async def test_get_initiatives_unphased_issues_excluded_from_tabs() -> None:
    """Issues without a scoped phase label are invisible in the board and must not create tabs."""
    rows = [
        # ac-ship has open issues but none with a scoped phase label → no tab.
        _make_row(["ac-ship"]),
        _make_row(["ac-ship"], state="open"),
        # ac-build has a proper scoped phase label → shows as tab.
        _make_row(["ac-build", "ac-build/phase-0"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives(
            "owner/repo", initiative_patterns=["ac-build", "ac-ship"]
        )
    assert "ac-build" in result
    assert "ac-ship" not in result


@pytest.mark.anyio
async def test_get_initiatives_empty_patterns_returns_empty() -> None:
    """When patterns is empty/None, no initiatives are returned (legacy heuristic removed)."""
    rows = [
        _make_row(["ac-build", "ac-build/phase-0"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result_empty = await get_initiatives("owner/repo", initiative_patterns=[])
        result_none = await get_initiatives("owner/repo")
    assert result_empty == []
    assert result_none == []


@pytest.mark.anyio
async def test_get_initiatives_db_error_returns_empty() -> None:
    """A DB error is caught and returns [] (non-fatal degradation)."""
    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("DB unavailable"))
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.db.queries.get_session", return_value=ctx_mock):
        result = await get_initiatives("owner/repo", initiative_patterns=["ac-*"])
    assert result == []
