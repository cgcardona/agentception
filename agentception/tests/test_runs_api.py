"""Tests for agentception/routes/api/runs.py.

Covers all three UI-facing run lifecycle endpoints:

    POST /api/runs/{run_id}/message  — append operator message to agent transcript
    POST /api/runs/{run_id}/cancel   — abort a pending_launch run before dispatch
    POST /api/runs/{run_id}/stop     — mark a live run as done from the inspector

No real database is used; all SQLAlchemy calls are replaced by AsyncMock so
these tests run fully in-process without Postgres.

Run targeted:
    pytest agentception/tests/test_runs_api.py -v
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Module-scoped test client; lifespan runs once for the whole file."""
    with TestClient(app) as c:
        yield c


# ── Session-mock helpers ───────────────────────────────────────────────────────
#
# Each helper builds a fake async context manager that yields a mock session
# pre-configured for the specific endpoint under test.


def _message_session(*, run_exists: bool, max_seq: int | None = 5) -> MagicMock:
    """Build a session mock for the POST /message endpoint.

    ``session.scalar()`` is called twice:
      1. ``func.count()`` existence check  → 1 (exists) or 0 (not found)
      2. ``func.coalesce(func.max(...))``  → max_seq
    """
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[
        1 if run_exists else 0,  # run existence count
        max_seq,                 # max sequence_index (or None for first message)
    ])
    session.add = MagicMock()
    session.commit = AsyncMock()

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _run_session(*, run_status: str | None) -> MagicMock:
    """Build a session mock for cancel/stop endpoints.

    ``session.execute()`` returns a result whose ``scalar_one_or_none()`` is
    either a run mock (with the given status) or None.
    """
    if run_status is not None:
        run = MagicMock()
        run.status = run_status
    else:
        run = None

    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _error_session() -> MagicMock:
    """Build a session mock that raises RuntimeError on ``__aenter__``."""
    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ── POST /api/runs/{run_id}/message ───────────────────────────────────────────


def test_message_returns_204_on_success(client: TestClient) -> None:
    """POST /api/runs/{id}/message returns 204 when the run exists and content is valid."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_message_session(run_exists=True, max_seq=2),
    ):
        response = client.post(
            "/api/runs/issue-42/message",
            json={"content": "Please stop after the next step."},
        )
    assert response.status_code == 204


def test_message_returns_404_when_run_not_found(client: TestClient) -> None:
    """POST /api/runs/{id}/message returns 404 when the run_id is unknown."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_message_session(run_exists=False),
    ):
        response = client.post(
            "/api/runs/ghost-99/message",
            json={"content": "hello"},
        )
    assert response.status_code == 404
    assert "ghost-99" in response.json()["detail"]


def test_message_returns_422_on_empty_content(client: TestClient) -> None:
    """POST /api/runs/{id}/message returns 422 when content is empty."""
    response = client.post(
        "/api/runs/issue-42/message",
        json={"content": ""},
    )
    assert response.status_code == 422


def test_message_returns_422_on_whitespace_content(client: TestClient) -> None:
    """POST /api/runs/{id}/message returns 422 when content is whitespace-only."""
    response = client.post(
        "/api/runs/issue-42/message",
        json={"content": "   \t\n  "},
    )
    assert response.status_code == 422


def test_message_returns_500_on_db_error(client: TestClient) -> None:
    """POST /api/runs/{id}/message returns 500 on unexpected DB failure."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_error_session(),
    ):
        response = client.post(
            "/api/runs/issue-42/message",
            json={"content": "This will fail"},
        )
    assert response.status_code == 500


def test_message_increments_sequence_correctly(client: TestClient) -> None:
    """POST /api/runs/{id}/message stores a message with sequence_index = max + 1."""
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[1, 4])  # exists=1, max_seq=4
    session.add = MagicMock()
    session.commit = AsyncMock()

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.routes.api.runs.get_session", return_value=ctx):
        response = client.post(
            "/api/runs/issue-42/message",
            json={"content": "Check in"},
        )
    assert response.status_code == 204
    # Verify session.add was called once with an object having sequence_index=5
    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.sequence_index == 5


def test_message_sequence_zero_when_no_prior_messages(client: TestClient) -> None:
    """POST /api/runs/{id}/message sets sequence_index=0 when no messages exist yet."""
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[1, None])  # exists=1, max_seq=None
    session.add = MagicMock()
    session.commit = AsyncMock()

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.routes.api.runs.get_session", return_value=ctx):
        response = client.post(
            "/api/runs/issue-42/message",
            json={"content": "First message"},
        )
    assert response.status_code == 204
    session.add.assert_called_once()
    assert session.add.call_args[0][0].sequence_index == 0


# ── POST /api/runs/{run_id}/cancel ────────────────────────────────────────────


def test_cancel_returns_204_on_success(client: TestClient) -> None:
    """POST /api/runs/{id}/cancel returns 204 when the run is in pending_launch."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_run_session(run_status="pending_launch"),
    ):
        response = client.post("/api/runs/issue-55/cancel")
    assert response.status_code == 204


def test_cancel_returns_404_when_run_not_found(client: TestClient) -> None:
    """POST /api/runs/{id}/cancel returns 404 when the run_id is unknown."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_run_session(run_status=None),
    ):
        response = client.post("/api/runs/ghost-99/cancel")
    assert response.status_code == 404
    assert "ghost-99" in response.json()["detail"]


def test_cancel_returns_409_when_run_is_implementing(client: TestClient) -> None:
    """POST /api/runs/{id}/cancel returns 409 when the run is already implementing."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_run_session(run_status="implementing"),
    ):
        response = client.post("/api/runs/issue-55/cancel")
    assert response.status_code == 409
    assert "implementing" in response.json()["detail"]


def test_cancel_returns_409_when_run_is_done(client: TestClient) -> None:
    """POST /api/runs/{id}/cancel returns 409 when the run is already done."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_run_session(run_status="done"),
    ):
        response = client.post("/api/runs/issue-55/cancel")
    assert response.status_code == 409
    assert "done" in response.json()["detail"]


def test_cancel_returns_500_on_db_error(client: TestClient) -> None:
    """POST /api/runs/{id}/cancel returns 500 on unexpected DB failure."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_error_session(),
    ):
        response = client.post("/api/runs/issue-55/cancel")
    assert response.status_code == 500


def test_cancel_transitions_run_to_cancelled(client: TestClient) -> None:
    """POST /api/runs/{id}/cancel sets run.status to 'cancelled' before commit."""
    run = MagicMock()
    run.status = "pending_launch"

    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.routes.api.runs.get_session", return_value=ctx):
        response = client.post("/api/runs/issue-42/cancel")

    assert response.status_code == 204
    assert run.status == "cancelled"
    session.commit.assert_called_once()


# ── POST /api/runs/{run_id}/stop ──────────────────────────────────────────────


def test_stop_returns_204_on_success(client: TestClient) -> None:
    """POST /api/runs/{id}/stop returns 204 for any existing run."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_run_session(run_status="implementing"),
    ):
        response = client.post("/api/runs/issue-77/stop")
    assert response.status_code == 204


def test_stop_returns_404_when_run_not_found(client: TestClient) -> None:
    """POST /api/runs/{id}/stop returns 404 when the run_id is unknown."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_run_session(run_status=None),
    ):
        response = client.post("/api/runs/ghost-99/stop")
    assert response.status_code == 404
    assert "ghost-99" in response.json()["detail"]


def test_stop_returns_500_on_db_error(client: TestClient) -> None:
    """POST /api/runs/{id}/stop returns 500 on unexpected DB failure."""
    with patch(
        "agentception.routes.api.runs.get_session",
        return_value=_error_session(),
    ):
        response = client.post("/api/runs/issue-77/stop")
    assert response.status_code == 500


def test_stop_transitions_run_to_done(client: TestClient) -> None:
    """POST /api/runs/{id}/stop sets run.status to 'done' regardless of current status."""
    run = MagicMock()
    run.status = "implementing"

    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.routes.api.runs.get_session", return_value=ctx):
        response = client.post("/api/runs/issue-77/stop")

    assert response.status_code == 204
    assert run.status == "done"
    session.commit.assert_called_once()
