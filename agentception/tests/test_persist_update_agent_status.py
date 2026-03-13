from __future__ import annotations

"""Unit tests for update_agent_status() in agentception/db/persist.py.

Covers:
- Happy path: status is updated and True is returned.
- Terminal guard: runs already in a terminal state are not overwritten.
- Not-found guard: missing run returns False without raising.
- Accepts AgentStatus enum value (str subclass) directly, not just plain str.

Run:
    docker compose exec agentception pytest \
        agentception/tests/test_persist_update_agent_status.py -v
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.models import ACAgentRun
from agentception.workflow.status import AgentStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(status: str = "implementing") -> MagicMock:
    run = MagicMock(spec=ACAgentRun)
    run.id = "adhoc-test-run"
    run.status = status
    run.last_activity_at = None
    return run


def _mock_session(run: MagicMock | None) -> MagicMock:
    scalar = MagicMock()
    scalar.scalar_one_or_none = MagicMock(return_value=run)
    execute = AsyncMock(return_value=scalar)
    session = AsyncMock()
    session.execute = execute
    session.commit = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUpdateAgentStatusHappyPath:
    @pytest.mark.anyio
    async def test_updates_status_and_returns_true(self) -> None:
        run = _make_run("implementing")
        session = _mock_session(run)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("agentception.db.persist.get_session", return_value=cm):
            from agentception.db.persist import update_agent_status

            result = await update_agent_status("adhoc-test-run", AgentStatus.STALLED)

        assert result is True
        # str(AgentStatus.STALLED) == "stalled" because AgentStatus inherits from str
        assert run.status == str(AgentStatus.STALLED)

    @pytest.mark.anyio
    async def test_accepts_plain_string(self) -> None:
        run = _make_run("implementing")
        session = _mock_session(run)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("agentception.db.persist.get_session", return_value=cm):
            from agentception.db.persist import update_agent_status

            result = await update_agent_status("adhoc-test-run", "stalled")

        assert result is True
        assert run.status == "stalled"

    @pytest.mark.anyio
    async def test_recovering_status_accepted(self) -> None:
        run = _make_run("stalled")
        session = _mock_session(run)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("agentception.db.persist.get_session", return_value=cm):
            from agentception.db.persist import update_agent_status

            result = await update_agent_status("adhoc-test-run", AgentStatus.RECOVERING)

        assert result is True
        assert run.status == str(AgentStatus.RECOVERING)


class TestUpdateAgentStatusTerminalGuard:
    """Terminal states must never be overwritten."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("terminal", ["completed", "cancelled", "stopped", "failed"])
    async def test_terminal_run_not_overwritten(self, terminal: str) -> None:
        run = _make_run(terminal)
        session = _mock_session(run)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("agentception.db.persist.get_session", return_value=cm):
            from agentception.db.persist import update_agent_status

            result = await update_agent_status("adhoc-test-run", AgentStatus.STALLED)

        assert result is False
        # Status must be unchanged — never overwrite a terminal state.
        assert run.status == terminal


class TestUpdateAgentStatusNotFound:
    @pytest.mark.anyio
    async def test_missing_run_returns_false(self) -> None:
        session = _mock_session(None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("agentception.db.persist.get_session", return_value=cm):
            from agentception.db.persist import update_agent_status

            result = await update_agent_status("nonexistent-run", AgentStatus.STALLED)

        assert result is False
