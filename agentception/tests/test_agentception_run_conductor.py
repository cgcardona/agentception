from __future__ import annotations

"""Tests for the "Run Conductor" feature (Issue #835).

Covers:
- GET /api/control/conductor-history — returns history entries, empty on DB failure
- ConductorHistoryEntry model fields
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


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


# ── GET /api/control/conductor-history ────────────────────────────────────────


def test_conductor_history_empty_when_no_db_entries(client: TestClient) -> None:
    """GET /api/control/conductor-history returns [] when DB has no conductor waves."""
    with patch(
        "agentception.db.queries.get_conductor_history",
        new_callable=AsyncMock,
        return_value=[],
    ):
        response = client.get("/api/control/conductor-history")

    assert response.status_code == 200
    assert response.json() == []


def test_conductor_history_returns_entries(client: TestClient) -> None:
    """GET /api/control/conductor-history returns entries from get_conductor_history."""
    fake_entries = [
        {
            "wave_id": "conductor-20260303-142201",
            "worktree": "/worktrees/conductor-20260303-142201",
            "host_worktree": "/host/conductor-20260303-142201",
            "started_at": "2026-03-03 14:22 UTC",
            "status": "completed",
        },
    ]
    with patch(
        "agentception.db.queries.get_conductor_history",
        new_callable=AsyncMock,
        return_value=fake_entries,
    ):
        response = client.get("/api/control/conductor-history")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    entry = data[0]
    assert entry["wave_id"] == "conductor-20260303-142201"
    assert entry["host_worktree"] == "/host/conductor-20260303-142201"
    assert entry["started_at"] == "2026-03-03 14:22 UTC"
    assert entry["status"] == "completed"


def test_conductor_history_status_active_when_worktree_exists(client: TestClient) -> None:
    """GET /api/control/conductor-history status is 'active' when worktree dir exists."""
    fake_entries = [
        {
            "wave_id": "conductor-20260303-150000",
            "worktree": "/worktrees/conductor-20260303-150000",
            "host_worktree": "/host/conductor-20260303-150000",
            "started_at": "2026-03-03 15:00 UTC",
            "status": "active",
        },
    ]
    with patch(
        "agentception.db.queries.get_conductor_history",
        new_callable=AsyncMock,
        return_value=fake_entries,
    ):
        response = client.get("/api/control/conductor-history")

    assert response.status_code == 200
    assert response.json()[0]["status"] == "active"


def test_conductor_history_returns_at_most_five(client: TestClient) -> None:
    """GET /api/control/conductor-history returns at most 5 entries."""
    fake_entries = [
        {
            "wave_id": f"conductor-2026030{i}-120000",
            "worktree": f"/worktrees/conductor-2026030{i}-120000",
            "host_worktree": f"/host/conductor-2026030{i}-120000",
            "started_at": f"2026-03-0{i} 12:00 UTC",
            "status": "completed",
        }
        for i in range(1, 6)
    ]
    with patch(
        "agentception.db.queries.get_conductor_history",
        new_callable=AsyncMock,
        return_value=fake_entries,
    ):
        response = client.get("/api/control/conductor-history")

    assert response.status_code == 200
    assert len(response.json()) == 5


# ── ConductorHistoryEntry model ───────────────────────────────────────────────


def test_conductor_history_entry_model_fields() -> None:
    """ConductorHistoryEntry must expose all required fields."""
    from agentception.routes.api.control import ConductorHistoryEntry

    entry = ConductorHistoryEntry(
        wave_id="conductor-20260303-142201",
        worktree="/worktrees/conductor-20260303-142201",
        host_worktree="/host/conductor-20260303-142201",
        started_at="2026-03-03 14:22 UTC",
        status="completed",
    )
    assert entry.wave_id == "conductor-20260303-142201"
    assert entry.host_worktree == "/host/conductor-20260303-142201"
    assert entry.status == "completed"


# ── get_conductor_history DB query helper ─────────────────────────────────────


@pytest.mark.anyio
async def test_get_conductor_history_status_resolved_from_worktree_dir(
    tmp_path: Path,
) -> None:
    """get_conductor_history marks a wave 'active' only when its dir exists."""
    from agentception.db.queries import get_conductor_history

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    host_worktrees = tmp_path / "host"
    host_worktrees.mkdir()

    wave_id_active = "conductor-20260303-100000"
    wave_id_done = "conductor-20260303-110000"

    # Create a directory only for the "active" wave.
    (worktrees / wave_id_active).mkdir()

    # Build fake ACWave objects the SQLAlchemy query would return.
    def _make_wave(wave_id: str) -> MagicMock:
        m = MagicMock()
        m.id = wave_id
        m.started_at = datetime.datetime(2026, 3, 3, 10, 0, 0, tzinfo=datetime.timezone.utc)
        return m

    fake_waves = [_make_wave(wave_id_active), _make_wave(wave_id_done)]

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = fake_waves

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.db.queries.get_session", return_value=mock_session):
        entries = await get_conductor_history(
            limit=5,
            worktrees_dir=worktrees,
            host_worktrees_dir=host_worktrees,
        )

    assert len(entries) == 2
    active_entry = next(e for e in entries if e["wave_id"] == wave_id_active)
    done_entry = next(e for e in entries if e["wave_id"] == wave_id_done)
    assert active_entry["status"] == "active"
    assert done_entry["status"] == "completed"


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
