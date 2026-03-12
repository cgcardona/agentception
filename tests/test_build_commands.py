from __future__ import annotations

"""Tests for build_complete_run pre-flight guards.

Covers the two invariant checks added to build_complete_run:
  1. At least one file_edit / write_file event must exist for the run.
  2. The run row must have a non-NULL pr_number.

All tests are fully isolated — no real DB connections are made.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_ID = "test-run-abc123"
_ISSUE = 42
_PR_URL = "https://github.com/cgcardona/agentception/pull/99"


def _make_scalar_one(value: object) -> MagicMock:
    """Return a mock whose .scalar_one() returns *value*."""
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


def _make_scalar_one_or_none(value: object) -> MagicMock:
    """Return a mock whose .scalar_one_or_none() returns *value*."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _make_session_cm(execute_side_effects: list[object]) -> MagicMock:
    """Build a mock async context manager for get_session().

    *execute_side_effects* is consumed in order for each ``session.execute``
    call made inside the ``async with get_session()`` block.
    """
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_side_effects)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_complete_run_refused_no_file_edits() -> None:
    """build_complete_run returns the file-edit refusal dict when count == 0."""
    from agentception.mcp.build_commands import build_complete_run

    # First get_session call: file-edit count query → 0
    session_cm_1 = _make_session_cm([_make_scalar_one(0)])

    with patch(
        "agentception.mcp.build_commands.get_session",
        side_effect=[session_cm_1],
    ):
        result = await build_complete_run(
            issue_number=_ISSUE,
            pr_url=_PR_URL,
            agent_run_id=_RUN_ID,
        )

    assert result == {
        "ok": False,
        "error": (
            "build_complete_run refused: no file edits recorded for this run. "
            "Write and commit your changes first."
        ),
    }


@pytest.mark.anyio
async def test_build_complete_run_refused_no_pr() -> None:
    """build_complete_run returns the PR-missing refusal dict when pr_number is None."""
    from agentception.mcp.build_commands import build_complete_run

    # First get_session call: file-edit count query → 1 (has edits)
    session_cm_1 = _make_session_cm([_make_scalar_one(1)])

    # Second get_session call: run row query → run with pr_number=None
    run_mock = MagicMock()
    run_mock.pr_number = None
    session_cm_2 = _make_session_cm([_make_scalar_one_or_none(run_mock)])

    with patch(
        "agentception.mcp.build_commands.get_session",
        side_effect=[session_cm_1, session_cm_2],
    ):
        result = await build_complete_run(
            issue_number=_ISSUE,
            pr_url=_PR_URL,
            agent_run_id=_RUN_ID,
        )

    assert result == {
        "ok": False,
        "error": (
            "build_complete_run refused: no PR found. "
            "Create and push a branch, then open a pull request first."
        ),
    }


@pytest.mark.anyio
async def test_build_complete_run_allowed_when_checks_pass() -> None:
    """build_complete_run proceeds to existing completion logic when both checks pass."""
    from agentception.mcp.build_commands import build_complete_run

    # First get_session call: file-edit count → 3 (has edits)
    session_cm_1 = _make_session_cm([_make_scalar_one(3)])

    # Second get_session call: run row → run with pr_number set
    run_mock = MagicMock()
    run_mock.pr_number = 99
    session_cm_2 = _make_session_cm([_make_scalar_one_or_none(run_mock)])

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            side_effect=[session_cm_1, session_cm_2],
        ),
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist,
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="developer",
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_teardown",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ),
        patch("asyncio.create_task"),
    ):
        result = await build_complete_run(
            issue_number=_ISSUE,
            pr_url=_PR_URL,
            agent_run_id=_RUN_ID,
        )

    # persist_agent_event must have been called — proof the handler proceeded
    mock_persist.assert_awaited_once()
    assert result == {"ok": True, "event": "done", "status": "completed"}
