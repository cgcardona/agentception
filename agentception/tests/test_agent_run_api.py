"""Tests for POST /api/runs/{run_id}/execute.

Covers:
  - 202 Accepted when run is pending_launch (claims + schedules loop)
  - 202 Accepted when run is implementing (skips claim, schedules loop)
  - 404 when run_id is not found
  - 409 when run is in a terminal/non-dispatchable status

No real database or agent loop is invoked — all DB and service calls are mocked.

Run targeted:
    pytest agentception/tests/test_agent_run_api.py -v
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.db.models import ACAgentRun


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def _mock_session(run: ACAgentRun | None) -> MagicMock:
    """Build a fake async session that returns *run* from scalar()."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=run)

    ctx: MagicMock = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_run(status: str, run_id: str = "run-42") -> ACAgentRun:
    run = MagicMock(spec=ACAgentRun)
    run.id = run_id
    run.status = status
    return run


# ---------------------------------------------------------------------------
# 202 — pending_launch
# ---------------------------------------------------------------------------


class TestExecuteAgentRunPendingLaunch:
    def test_returns_202_and_claims_run(self, client: TestClient) -> None:
        run = _make_run("pending_launch")
        session_ctx = _mock_session(run)

        with (
            patch("agentception.routes.api.agent_run.get_session", return_value=session_ctx),
            patch(
                "agentception.routes.api.agent_run.build_claim_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_claim,
            patch("agentception.routes.api.agent_run.run_agent_loop", new_callable=AsyncMock),
        ):
            resp = client.post("/api/runs/run-42/execute")

        assert resp.status_code == 202
        body = resp.json()
        assert body["ok"] is True
        assert body["run_id"] == "run-42"
        mock_claim.assert_called_once_with("run-42")

    def test_body_contains_run_id(self, client: TestClient) -> None:
        run = _make_run("pending_launch", "my-special-run")
        session_ctx = _mock_session(run)

        with (
            patch("agentception.routes.api.agent_run.get_session", return_value=session_ctx),
            patch(
                "agentception.routes.api.agent_run.build_claim_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
            patch("agentception.routes.api.agent_run.run_agent_loop", new_callable=AsyncMock),
        ):
            resp = client.post("/api/runs/my-special-run/execute")

        assert resp.json()["run_id"] == "my-special-run"


# ---------------------------------------------------------------------------
# 202 — already implementing
# ---------------------------------------------------------------------------


class TestExecuteAgentRunImplementing:
    def test_returns_202_without_claiming(self, client: TestClient) -> None:
        run = _make_run("implementing")
        session_ctx = _mock_session(run)

        with (
            patch("agentception.routes.api.agent_run.get_session", return_value=session_ctx),
            patch(
                "agentception.routes.api.agent_run.build_claim_run",
                new_callable=AsyncMock,
            ) as mock_claim,
            patch("agentception.routes.api.agent_run.run_agent_loop", new_callable=AsyncMock),
        ):
            resp = client.post("/api/runs/run-42/execute")

        assert resp.status_code == 202
        mock_claim.assert_not_called()


# ---------------------------------------------------------------------------
# 404 — run not found
# ---------------------------------------------------------------------------


class TestExecuteAgentRunNotFound:
    def test_returns_404(self, client: TestClient) -> None:
        session_ctx = _mock_session(None)

        with patch("agentception.routes.api.agent_run.get_session", return_value=session_ctx):
            resp = client.post("/api/runs/ghost-run/execute")

        assert resp.status_code == 404
        assert "ghost-run" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 409 — non-dispatchable status
# ---------------------------------------------------------------------------


class TestExecuteAgentRunBadStatus:
    @pytest.mark.parametrize("status", ["cancelled", "done", "reviewing", "blocked"])
    def test_returns_409_for_terminal_status(self, client: TestClient, status: str) -> None:
        run = _make_run(status)
        session_ctx = _mock_session(run)

        with patch("agentception.routes.api.agent_run.get_session", return_value=session_ctx):
            resp = client.post("/api/runs/run-42/execute")

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert status in detail

    def test_409_body_includes_current_status(self, client: TestClient) -> None:
        run = _make_run("done")
        session_ctx = _mock_session(run)

        with patch("agentception.routes.api.agent_run.get_session", return_value=session_ctx):
            resp = client.post("/api/runs/run-42/execute")

        assert "done" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Claim failure — 409
# ---------------------------------------------------------------------------


class TestExecuteAgentRunClaimFailure:
    def test_returns_409_when_claim_fails(self, client: TestClient) -> None:
        run = _make_run("pending_launch")
        session_ctx = _mock_session(run)

        with (
            patch("agentception.routes.api.agent_run.get_session", return_value=session_ctx),
            patch(
                "agentception.routes.api.agent_run.build_claim_run",
                new_callable=AsyncMock,
                return_value={"ok": False, "error": "already claimed"},
            ),
        ):
            resp = client.post("/api/runs/run-42/execute")

        assert resp.status_code == 409
        assert "already claimed" in resp.json()["detail"]
