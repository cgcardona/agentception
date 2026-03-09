from __future__ import annotations

"""Tests for the "Run Conductor" feature (Issue #835).

Covers:
- get_conductor_history() DB query helper (status resolution)
- Overview route exposes active_org in template context

Run targeted:
    pytest agentception/tests/test_agentception_run_conductor.py -v
"""

import datetime
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


# ── get_conductor_history DB query helper ─────────────────────────────────────


def _make_wave(wave_id: str) -> MagicMock:
    m = MagicMock()
    m.id = wave_id
    m.started_at = datetime.datetime(2026, 3, 3, 10, 0, 0, tzinfo=datetime.timezone.utc)
    return m


def _mock_session_returning(rows: list[tuple[MagicMock, str | None]]) -> AsyncMock:
    """Return an async context-manager session whose execute returns *rows*."""
    mock_result = MagicMock()
    mock_result.all.return_value = rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.anyio
async def test_get_conductor_history_status_resolved_from_db(
    tmp_path: Path,
) -> None:
    """get_conductor_history derives wave status from the latest DB run, not the filesystem."""
    from agentception.db.queries import get_conductor_history

    wave_id_active = "conductor-20260303-100000"
    wave_id_done = "conductor-20260303-110000"

    rows: list[tuple[MagicMock, str | None]] = [
        (_make_wave(wave_id_active), "implementing"),  # active run → "active"
        (_make_wave(wave_id_done), "completed"),        # terminal run → "completed"
    ]

    with patch(
        "agentception.db.queries.get_session",
        return_value=_mock_session_returning(rows),
    ):
        entries = await get_conductor_history(
            limit=5,
            worktrees_dir=tmp_path,
            host_worktrees_dir=tmp_path,
        )

    assert len(entries) == 2
    active_entry = next(e for e in entries if e["wave_id"] == wave_id_active)
    done_entry = next(e for e in entries if e["wave_id"] == wave_id_done)
    assert active_entry["status"] == "active"
    assert done_entry["status"] == "completed"


@pytest.mark.anyio
async def test_get_conductor_history_reviewing_is_active(tmp_path: Path) -> None:
    """A wave whose latest run is 'reviewing' is also considered active."""
    from agentception.db.queries import get_conductor_history

    rows: list[tuple[MagicMock, str | None]] = [(_make_wave("conductor-review"), "reviewing")]

    with patch(
        "agentception.db.queries.get_session",
        return_value=_mock_session_returning(rows),
    ):
        entries = await get_conductor_history(limit=5, worktrees_dir=tmp_path, host_worktrees_dir=tmp_path)

    assert entries[0]["status"] == "active"


@pytest.mark.anyio
async def test_get_conductor_history_no_run_is_completed(tmp_path: Path) -> None:
    """A wave with no associated run (LEFT JOIN → None) is marked completed."""
    from agentception.db.queries import get_conductor_history

    rows: list[tuple[MagicMock, str | None]] = [(_make_wave("conductor-orphan"), None)]

    with patch(
        "agentception.db.queries.get_session",
        return_value=_mock_session_returning(rows),
    ):
        entries = await get_conductor_history(limit=5, worktrees_dir=tmp_path, host_worktrees_dir=tmp_path)

    assert entries[0]["status"] == "completed"


@pytest.mark.anyio
async def test_get_conductor_history_no_fs_access(tmp_path: Path) -> None:
    """get_conductor_history must never call Path.exists — status comes from DB."""
    from agentception.db.queries import get_conductor_history

    rows: list[tuple[MagicMock, str | None]] = [(_make_wave("conductor-20260303-100000"), "implementing")]

    with patch(
        "agentception.db.queries.get_session",
        return_value=_mock_session_returning(rows),
    ), patch("pathlib.Path.exists") as mock_exists:
        await get_conductor_history(limit=5, worktrees_dir=tmp_path, host_worktrees_dir=tmp_path)

    mock_exists.assert_not_called()


@pytest.mark.anyio
async def test_get_conductor_history_returns_empty_on_db_error(
    tmp_path: Path,
) -> None:
    """get_conductor_history returns [] when the DB session raises an exception."""
    from agentception.db.queries import get_conductor_history

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError("DB unavailable"))
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.db.queries.get_session", return_value=mock_session):
        entries = await get_conductor_history(
            limit=5,
            worktrees_dir=tmp_path,
            host_worktrees_dir=tmp_path,
        )

    assert entries == []


# ── Overview route — active_org exposure ──────────────────────────────────────


def test_overview_page_renders_without_active_org(client: TestClient) -> None:
    """GET / should render successfully even when active_org is absent from config."""
    # Patch Path.exists so the config path appears missing, forcing active_org = None.
    from pathlib import Path as _Path

    original_exists = _Path.exists

    def _fake_exists(self: _Path) -> bool:
        if "pipeline-config.json" in str(self):
            return False
        return original_exists(self)

    with patch.object(_Path, "exists", _fake_exists):
        response = client.get("/overview")
    # Accept 200 or a redirect — the important thing is no 500.
    assert response.status_code in (200, 302, 307)


def test_overview_exposes_run_conductor_button(client: TestClient) -> None:
    """GET /overview must include the 'Run Conductor' button markup in the response."""
    response = client.get("/overview")
    assert response.status_code == 200
    assert "Run Conductor" in response.text
    assert "open-run-conductor-modal" in response.text
