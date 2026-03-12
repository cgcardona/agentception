from __future__ import annotations

"""Tests for build_complete_run pre-flight guards.

Coverage:
- Refused when no file-edit events are recorded for the run.
- Refused when file-edit events exist but pr_number is NULL on the run row.
- Allowed (reaches existing completion logic) when both conditions are satisfied.

Run targeted:
    pytest tests/test_build_commands.py -v
"""

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


def _make_preflight_session(
    file_edit_count: int,
    pr_number: int | None,
) -> object:
    """Return a mock async context-manager for get_session.

    Simulates the two sequential queries executed by the pre-flight guards:
    1. COUNT of ACAgentEvent rows with event_type in ('file_edit', 'write_file').
    2. SELECT of ACAgentRun row to check pr_number IS NOT NULL.
    """
    mock_session = MagicMock()

    count_result = MagicMock()
    count_result.scalar_one.return_value = file_edit_count

    run_row = MagicMock()
    run_row.pr_number = pr_number
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run_row

    mock_session.execute = AsyncMock(side_effect=[count_result, run_result])

    from collections.abc import AsyncGenerator

    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    return _ctx


@pytest.mark.anyio
async def test_build_complete_run_refused_no_file_edits() -> None:
    """build_complete_run returns a refusal dict when no file-edit events exist.

    Invariant: an agent that has not written any files cannot declare completion.
    The handler must return a structured error dict (not raise) so the MCP
    framework serialises it back to the agent as a retryable tool response.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "dev-issue-100-noedits"

    with patch(
        "agentception.mcp.build_commands.get_session",
        new=_make_preflight_session(file_edit_count=0, pr_number=None),
    ):
        result = await build_complete_run(
            issue_number=100,
            pr_url="https://github.com/cgcardona/agentception/pull/500",
            agent_run_id=agent_run_id,
        )

    assert result["ok"] is False
    assert result["error"] == (
        "build_complete_run refused: no file edits recorded for this run. "
        "Write and commit your changes first."
    )


@pytest.mark.anyio
async def test_build_complete_run_refused_no_pr() -> None:
    """build_complete_run returns a refusal dict when pr_number is NULL on the run row.

    Invariant: a PR must exist before the reviewer can be dispatched.  The
    handler must return a structured error dict (not raise) so the agent can
    open the PR and retry.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "dev-issue-101-nopr"

    with patch(
        "agentception.mcp.build_commands.get_session",
        new=_make_preflight_session(file_edit_count=3, pr_number=None),
    ):
        result = await build_complete_run(
            issue_number=101,
            pr_url="https://github.com/cgcardona/agentception/pull/501",
            agent_run_id=agent_run_id,
        )

    assert result["ok"] is False
    assert result["error"] == (
        "build_complete_run refused: no PR found. "
        "Create and push a branch, then open a pull request first."
    )


@pytest.mark.anyio
async def test_build_complete_run_allowed_when_checks_pass() -> None:
    """build_complete_run proceeds to existing completion logic when both guards pass.

    When file-edit count > 0 AND pr_number IS NOT NULL, the handler must reach
    the persist_agent_event call — the first side-effectful operation in the
    existing completion path.
    """
    from agentception.mcp.build_commands import build_complete_run

    agent_run_id = "dev-issue-102-ok"

    with (
        patch(
            "agentception.mcp.build_commands.get_session",
            new=_make_preflight_session(file_edit_count=5, pr_number=502),
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
            "agentception.mcp.build_commands.asyncio.create_task",
        ),
    ):
        result = await build_complete_run(
            issue_number=102,
            pr_url="https://github.com/cgcardona/agentception/pull/502",
            agent_run_id=agent_run_id,
        )

    # The handler must have reached the existing completion path.
    mock_persist.assert_called_once()
    assert result["ok"] is True
    assert result["status"] == "completed"
