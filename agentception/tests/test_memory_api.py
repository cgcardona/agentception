"""Tests for GET /api/runs/{run_id}/memory.

Covers:
    - HTTP 404 when run_id does not exist in the runs table.
    - HTTP 200 with correct ``files_written`` payload when the run exists and
      has ``file_edit`` events seeded in ``ACAgentEvent``.

No real database is used; all SQLAlchemy calls are replaced by AsyncMock so
these tests run fully in-process without Postgres.

Run targeted:
    pytest agentception/tests/test_memory_api.py -v
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; lifespan runs once for the whole file."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Session-mock helpers
# ---------------------------------------------------------------------------


def _run_check_session(*, run_exists: bool) -> MagicMock:
    """Build a session mock for the run-existence check in get_run_memory.

    ``session.scalar()`` returns 1 when the run exists, 0 otherwise.
    """
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=1 if run_exists else 0)

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_event_row(
    *,
    row_id: int,
    run_id: str,
    path: str,
    diff: str,
    lines_omitted: int = 0,
    timestamp: datetime.datetime | None = None,
) -> MagicMock:
    """Build a mock ACAgentEvent row with a valid file_edit payload."""
    ts = timestamp or datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    payload = json.dumps(
        {
            "timestamp": ts.isoformat(),
            "path": path,
            "diff": diff,
            "lines_omitted": lines_omitted,
        }
    )
    row = MagicMock()
    row.id = row_id
    row.agent_run_id = run_id
    row.event_type = "file_edit"
    row.payload = payload
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_memory_endpoint_returns_404_for_nonexistent_run(
    client: TestClient,
) -> None:
    """GET /api/runs/nonexistent-run/memory returns HTTP 404."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_run_check_session(run_exists=False),
    ):
        response = client.get("/api/runs/nonexistent-run/memory")

    assert response.status_code == 404
    assert "nonexistent-run" in response.json()["detail"]


def test_memory_endpoint_returns_file_edit_events(
    client: TestClient,
) -> None:
    """GET /api/runs/{run_id}/memory returns HTTP 200 with both seeded events.

    Seeds two ACAgentEvent rows with event_type='file_edit' and valid payload
    JSON, then asserts:
    - HTTP 200
    - Both events appear in ``files_written``
    - All four fields (timestamp, path, diff, lines_omitted) are present on each
    """
    run_id = "issue-783-test"
    ts1 = datetime.datetime(2024, 6, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)
    ts2 = datetime.datetime(2024, 6, 1, 10, 5, 0, tzinfo=datetime.timezone.utc)

    row1 = _make_event_row(
        row_id=1,
        run_id=run_id,
        path="agentception/models/__init__.py",
        diff="--- a/agentception/models/__init__.py\n+++ b/agentception/models/__init__.py\n@@ -1 +1 @@\n-old\n+new",
        lines_omitted=0,
        timestamp=ts1,
    )
    row2 = _make_event_row(
        row_id=2,
        run_id=run_id,
        path="agentception/routes/api/runs.py",
        diff="--- a/agentception/routes/api/runs.py\n+++ b/agentception/routes/api/runs.py\n@@ -1 +1 @@\n-x\n+y",
        lines_omitted=5,
        timestamp=ts2,
    )

    # Mock the DB session for the run-existence check
    run_check_ctx = _run_check_session(run_exists=True)

    # Mock get_file_edit_events to return deserialized FileEditEvent objects
    from agentception.models import FileEditEvent

    fake_events = [
        FileEditEvent(
            timestamp=ts1,
            path="agentception/models/__init__.py",
            diff="--- a/agentception/models/__init__.py\n+++ b/agentception/models/__init__.py\n@@ -1 +1 @@\n-old\n+new",
            lines_omitted=0,
        ),
        FileEditEvent(
            timestamp=ts2,
            path="agentception/routes/api/runs.py",
            diff="--- a/agentception/routes/api/runs.py\n+++ b/agentception/routes/api/runs.py\n@@ -1 +1 @@\n-x\n+y",
            lines_omitted=5,
        ),
    ]

    with (
        patch(
            "agentception.routes.api.runs.get_session",
            return_value=run_check_ctx,
        ),
        patch(
            "agentception.routes.api.runs.get_file_edit_events",
            new=AsyncMock(return_value=fake_events),
        ),
    ):
        response = client.get(f"/api/runs/{run_id}/memory")

    assert response.status_code == 200
    body = response.json()
    assert "files_written" in body
    assert len(body["files_written"]) == 2

    for event in body["files_written"]:
        assert "timestamp" in event
        assert "path" in event
        assert "diff" in event
        assert "lines_omitted" in event

    paths = [e["path"] for e in body["files_written"]]
    assert "agentception/models/__init__.py" in paths
    assert "agentception/routes/api/runs.py" in paths

    # Verify lines_omitted is correctly propagated
    runs_event = next(e for e in body["files_written"] if "runs.py" in e["path"])
    assert runs_event["lines_omitted"] == 5
