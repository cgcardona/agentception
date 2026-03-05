from __future__ import annotations

"""Unit tests for ``get_initiatives()`` — both the config-driven (fnmatch)
path and the legacy ``phase-N`` heuristic fallback.

Run targeted:
    pytest agentception/tests/test_get_initiatives.py -v
"""

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

# asynccontextmanager and AsyncIterator are used by _mock_session_context below.

import pytest

from agentception.db.queries import get_initiatives


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(labels: list[str], state: str = "open") -> tuple[str, str]:
    """Return a (labels_json, state) tuple as the DB query returns."""
    return json.dumps(labels), state


def _mock_session_context(rows: list[tuple[str, str]]) -> MagicMock:
    """Build a mock async context manager whose ``execute`` returns *rows*."""
    result_mock = MagicMock()
    result_mock.all.return_value = rows

    session_mock = AsyncMock()
    session_mock.execute = AsyncMock(return_value=result_mock)

    @asynccontextmanager
    async def _ctx() -> AsyncIterator[AsyncMock]:
        yield session_mock

    return _ctx  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Config-driven path (initiative_patterns provided)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_initiatives_patterns_exact_match() -> None:
    """Labels that exactly match a pattern are returned as initiatives."""
    rows = [
        _make_row(["agentception", "ac-build/1-tree-ui"]),
        _make_row(["ac-plan"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives(
            "owner/repo", initiative_patterns=["agentception", "ac-plan", "ac-build"]
        )
    assert result == ["ac-plan", "agentception"]


@pytest.mark.anyio
async def test_get_initiatives_patterns_glob_match() -> None:
    """Glob patterns like ``ac-*`` match multiple initiative labels."""
    rows = [
        _make_row(["ac-build", "ac-build/1-tree-ui"]),
        _make_row(["ac-workflow", "ac-workflow/5-plan-step-v2"]),
        _make_row(["ac-reliability"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo", initiative_patterns=["ac-*"])
    # Sub-labels matching "ac-*" are also returned — callers can further filter
    # if needed, but the pattern contract is: whatever matches is an initiative.
    assert "ac-build" in result
    assert "ac-workflow" in result
    assert "ac-reliability" in result


@pytest.mark.anyio
async def test_get_initiatives_patterns_excludes_closed_only() -> None:
    """An initiative only appearing on closed issues is not returned."""
    rows = [
        _make_row(["ac-build"], state="closed"),
        _make_row(["ac-plan"], state="open"),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo", initiative_patterns=["ac-*"])
    assert "ac-plan" in result
    assert "ac-build" not in result


@pytest.mark.anyio
async def test_get_initiatives_patterns_returns_sorted() -> None:
    """Result is alphabetically sorted regardless of row order."""
    rows = [
        _make_row(["ac-workflow"]),
        _make_row(["ac-build"]),
        _make_row(["ac-plan"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives(
            "owner/repo", initiative_patterns=["ac-build", "ac-plan", "ac-workflow"]
        )
    assert result == ["ac-build", "ac-plan", "ac-workflow"]


@pytest.mark.anyio
async def test_get_initiatives_empty_patterns_uses_legacy_heuristic() -> None:
    """When patterns is empty, the phase-N heuristic fires instead."""
    rows = [
        # This issue has a phase-N label → sibling label becomes an initiative.
        _make_row(["my-project", "phase-0"]),
        # This issue has no phase-N label → ignored by legacy heuristic.
        _make_row(["other-project", "ac-build/1-tree-ui"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo", initiative_patterns=[])
    assert result == ["my-project"]
    assert "other-project" not in result


@pytest.mark.anyio
async def test_get_initiatives_none_patterns_uses_legacy_heuristic() -> None:
    """When patterns is None (default), the phase-N heuristic fires."""
    rows = [
        _make_row(["my-project", "phase-1"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo")
    assert result == ["my-project"]


# ---------------------------------------------------------------------------
# Legacy heuristic (no patterns)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_initiatives_legacy_blocklist_excluded() -> None:
    """Labels in _NON_INITIATIVE_LABELS are not surfaced even with phase-N present."""
    rows = [
        _make_row(["bug", "enhancement", "phase-0"]),
    ]
    with patch("agentception.db.queries.get_session", _mock_session_context(rows)):
        result = await get_initiatives("owner/repo")
    assert result == []


@pytest.mark.anyio
async def test_get_initiatives_db_error_returns_empty() -> None:
    """A DB error is caught and returns [] (non-fatal degradation)."""
    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("DB unavailable"))
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.db.queries.get_session", return_value=ctx_mock):
        result = await get_initiatives("owner/repo", initiative_patterns=["ac-*"])
    assert result == []
