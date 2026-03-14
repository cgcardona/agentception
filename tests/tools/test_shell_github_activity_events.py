"""Tests for activity event emission from shell_tools and GitHub MCP (issue #942).

Verifies that run_command emits shell_start and shell_done, git_commit_and_push
emits git_push, the GitHub MCP dispatch path emits github_tool, and that a
persist failure never propagates to the caller.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.tools.shell_tools import git_commit_and_push, run_command


_RUN_ID = "issue-942"


# ---------------------------------------------------------------------------
# shell_start + shell_done
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_shell_start_and_done_emitted() -> None:
    """run_command with run_id and session emits shell_start then shell_done."""
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()

    with patch(
        "agentception.tools.shell_tools.activity_events.persist_activity_event"
    ) as mock_persist:
        result = await run_command(
            "echo ok",
            run_id=_RUN_ID,
            session=mock_session,
        )

    assert result.get("ok") is True
    assert result.get("exit_code") == 0
    assert mock_persist.call_count == 2

    first_call = mock_persist.call_args_list[0]
    assert first_call[0][2] == "shell_start"
    payload_start = first_call[0][3]
    assert "cmd_preview" in payload_start
    assert "ok" in payload_start["cmd_preview"]
    assert payload_start["cwd"] == ""

    second_call = mock_persist.call_args_list[1]
    assert second_call[0][2] == "shell_done"
    payload_done = second_call[0][3]
    assert payload_done["exit_code"] == 0
    assert "stdout_bytes" in payload_done
    assert "stderr_bytes" in payload_done
    mock_session.flush.assert_called()


# ---------------------------------------------------------------------------
# github_tool
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_github_tool_event_emitted(tmp_path: Path) -> None:
    """Dispatch path for a GitHub MCP tool calls persist with subtype github_tool."""
    import json

    from agentception.services.agent_loop import _dispatch_single_tool
    from agentception.services.llm import ToolCall, ToolCallFunction

    tc = ToolCall(
        id="call_gh",
        type="function",
        function=ToolCallFunction(
            name="create_pull_request",
            arguments=json.dumps({"title": "Test", "body": "Body", "head": "feat/x"}),
        ),
    )
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_github = AsyncMock()
    mock_github.call_tool = AsyncMock(return_value="PR #1 created")

    with patch(
        "agentception.services.agent_loop.persist_activity_event"
    ) as mock_persist:
        result = await _dispatch_single_tool(
            tc,
            tmp_path,
            _RUN_ID,
            session=mock_session,
            github_client=mock_github,
            github_tool_names=frozenset({"create_pull_request"}),
        )

    assert result.get("ok") is True
    # tool_invoked first, then github_tool
    assert mock_persist.call_count >= 2
    github_calls = [c for c in mock_persist.call_args_list if c[0][2] == "github_tool"]
    assert len(github_calls) == 1
    payload = github_calls[0][0][3]
    assert payload["tool_name"] == "create_pull_request"
    assert "arg_preview" in payload


# ---------------------------------------------------------------------------
# persist failure must not break shell
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_persist_failure_does_not_break_shell() -> None:
    """When persist_activity_event raises, run_command still succeeds."""
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()

    with patch(
        "agentception.tools.shell_tools.activity_events.persist_activity_event",
        side_effect=Exception("db down"),
    ):
        result = await run_command(
            "echo ok",
            run_id=_RUN_ID,
            session=mock_session,
        )

    assert result.get("ok") is True
    assert result.get("exit_code") == 0
    assert "stdout" in result


# ---------------------------------------------------------------------------
# no event when run_id or session missing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_shell_events_when_run_id_none() -> None:
    """When run_id is None, no shell_start/shell_done persist calls."""
    mock_session = AsyncMock()

    with patch(
        "agentception.tools.shell_tools.activity_events.persist_activity_event"
    ) as mock_persist:
        result = await run_command("echo ok", session=mock_session)

    assert result.get("ok") is True
    mock_persist.assert_not_called()


@pytest.mark.anyio
async def test_no_shell_events_when_session_none() -> None:
    """When session is None, no shell_start/shell_done persist calls."""
    with patch(
        "agentception.tools.shell_tools.activity_events.persist_activity_event"
    ) as mock_persist:
        result = await run_command("echo ok", run_id=_RUN_ID)

    assert result.get("ok") is True
    mock_persist.assert_not_called()


# ---------------------------------------------------------------------------
# git_push
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_git_push_event_emitted(tmp_path: Path) -> None:
    """git_commit_and_push with run_id and session emits git_push after success."""
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    # _git is called: rev-parse HEAD (branch), add, commit, push, rev-parse HEAD (sha).
    # Return current branch "feat/942" so checkout is skipped, then success for rest.
    async def fake_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "feat/942", "")
        if args == ["rev-parse", "HEAD"]:
            return (0, "abc123", "")
        return (0, "", "")

    with (
        patch(
            "agentception.tools.shell_tools.activity_events.persist_activity_event"
        ) as mock_persist,
        patch("agentception.tools.shell_tools._git", side_effect=fake_git),
    ):
        result = await git_commit_and_push(
            "feat/942",
            "msg",
            ["any-path"],
            tmp_path,
            run_id=_RUN_ID,
            session=mock_session,
        )

    assert result.get("ok") is True
    git_push_calls = [c for c in mock_persist.call_args_list if c[0][2] == "git_push"]
    assert len(git_push_calls) == 1
    assert git_push_calls[0][0][3]["branch"] == "feat/942"
