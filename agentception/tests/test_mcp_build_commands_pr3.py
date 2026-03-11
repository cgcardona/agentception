from __future__ import annotations

"""Integration + regression tests for PR 3 new build commands.

Tests:
- build_block_run:   implementing → blocked
- build_resume_run:  blocked/stopped → implementing; idempotent restart
- build_cancel_run:  any active → cancelled; rejects terminal
- build_stop_run:    any active → stopped; rejects terminal

All tests go through the MCP layer (call_tool_async) to verify end-to-end
dispatch in addition to the unit tests in test_persist_pending_launch_guard.py.

Regression tests named per spec:
- test_build_resume_run_idempotent_same_agent_run_id
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from agentception.mcp.server import call_tool_async


# ---------------------------------------------------------------------------
# build_block_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_block_run_success_via_mcp() -> None:
    """build_block_run MCP tool returns ok=true when state transition succeeds."""
    with patch(
        "agentception.mcp.build_commands.block_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await call_tool_async("build_block_run", {"run_id": "issue-42"})

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["run_id"] == "issue-42"
    assert payload["status"] == "blocked"


@pytest.mark.anyio
async def test_build_block_run_rejects_wrong_state() -> None:
    """build_block_run returns isError=True when run is not in implementing state."""
    with patch(
        "agentception.mcp.build_commands.block_agent_run",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await call_tool_async("build_block_run", {"run_id": "issue-42"})

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "reason" in payload


@pytest.mark.anyio
async def test_build_block_run_missing_run_id_returns_error() -> None:
    """build_block_run returns isError=True when run_id is missing."""
    result = await call_tool_async("build_block_run", {})
    assert result["isError"] is True


def test_build_block_run_in_tools_list() -> None:
    """build_block_run is present in the TOOLS registry."""
    from agentception.mcp.server import TOOLS
    names = [t["name"] for t in TOOLS]
    assert "build_block_run" in names


# ---------------------------------------------------------------------------
# build_resume_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_resume_run_success_via_mcp() -> None:
    """build_resume_run MCP tool returns ok=true when state transition succeeds."""
    with patch(
        "agentception.mcp.build_commands.resume_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await call_tool_async(
            "build_resume_run",
            {"run_id": "issue-42", "agent_run_id": "issue-42"},
        )

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["run_id"] == "issue-42"
    assert payload["status"] == "implementing"


@pytest.mark.anyio
async def test_build_resume_run_idempotent_same_agent_run_id() -> None:
    """Regression: build_resume_run with same agent_run_id while already implementing returns ok.

    This is the restart-safe behaviour — an agent that crashes and restarts
    calls build_resume_run on startup. If the run is already implementing with
    the same run ID, the call must succeed so the agent can continue work.
    """
    with patch(
        "agentception.mcp.build_commands.resume_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await call_tool_async(
            "build_resume_run",
            {"run_id": "issue-42", "agent_run_id": "issue-42"},
        )

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True


@pytest.mark.anyio
async def test_build_resume_run_rejects_non_resumable_state() -> None:
    """build_resume_run returns isError=True when run is not resumable."""
    with patch(
        "agentception.mcp.build_commands.resume_agent_run",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await call_tool_async(
            "build_resume_run",
            {"run_id": "issue-42", "agent_run_id": "issue-42"},
        )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "reason" in payload


@pytest.mark.anyio
async def test_build_resume_run_missing_args_returns_error() -> None:
    """build_resume_run returns isError=True when required args are missing."""
    result = await call_tool_async("build_resume_run", {"run_id": "issue-42"})
    assert result["isError"] is True

    result2 = await call_tool_async("build_resume_run", {})
    assert result2["isError"] is True


def test_build_resume_run_in_tools_list() -> None:
    """build_resume_run is present in the TOOLS registry."""
    from agentception.mcp.server import TOOLS
    names = [t["name"] for t in TOOLS]
    assert "build_resume_run" in names


# ---------------------------------------------------------------------------
# build_cancel_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_cancel_run_success_via_mcp() -> None:
    """build_cancel_run MCP tool returns ok=true when transition succeeds."""
    with patch(
        "agentception.mcp.build_commands.cancel_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await call_tool_async("build_cancel_run", {"run_id": "issue-42"})

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["status"] == "cancelled"


@pytest.mark.anyio
async def test_build_cancel_run_rejects_terminal_state() -> None:
    """build_cancel_run returns isError=True when run is already terminal."""
    with patch(
        "agentception.mcp.build_commands.cancel_agent_run",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await call_tool_async("build_cancel_run", {"run_id": "issue-42"})

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False


@pytest.mark.anyio
async def test_build_cancel_run_missing_run_id_returns_error() -> None:
    """build_cancel_run returns isError=True when run_id is missing."""
    result = await call_tool_async("build_cancel_run", {})
    assert result["isError"] is True


def test_build_cancel_run_in_tools_list() -> None:
    """build_cancel_run is present in the TOOLS registry."""
    from agentception.mcp.server import TOOLS
    names = [t["name"] for t in TOOLS]
    assert "build_cancel_run" in names


# ---------------------------------------------------------------------------
# build_stop_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_stop_run_success_via_mcp() -> None:
    """build_stop_run MCP tool returns ok=true when transition succeeds."""
    with patch(
        "agentception.mcp.build_commands.stop_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await call_tool_async("build_stop_run", {"run_id": "issue-42"})

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["status"] == "stopped"


@pytest.mark.anyio
async def test_build_stop_run_rejects_terminal_state() -> None:
    """build_stop_run returns isError=True when run is already terminal."""
    with patch(
        "agentception.mcp.build_commands.stop_agent_run",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await call_tool_async("build_stop_run", {"run_id": "issue-42"})

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False


@pytest.mark.anyio
async def test_build_stop_run_missing_run_id_returns_error() -> None:
    """build_stop_run returns isError=True when run_id is missing."""
    result = await call_tool_async("build_stop_run", {})
    assert result["isError"] is True


def test_build_stop_run_in_tools_list() -> None:
    """build_stop_run is present in the TOOLS registry."""
    from agentception.mcp.server import TOOLS
    names = [t["name"] for t in TOOLS]
    assert "build_stop_run" in names


# ---------------------------------------------------------------------------
# Integration: claim → block → resume flow
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_claim_block_resume_flow() -> None:
    """Integration: claim → block → resume transitions succeed in sequence."""
    with patch(
        "agentception.mcp.build_commands.acknowledge_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        claim_result = await call_tool_async("build_claim_run", {"run_id": "issue-99"})

    assert json.loads(claim_result["content"][0]["text"])["ok"] is True

    with patch(
        "agentception.mcp.build_commands.block_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        block_result = await call_tool_async("build_block_run", {"run_id": "issue-99"})

    assert json.loads(block_result["content"][0]["text"])["status"] == "blocked"

    with patch(
        "agentception.mcp.build_commands.resume_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        resume_result = await call_tool_async(
            "build_resume_run",
            {"run_id": "issue-99", "agent_run_id": "issue-99"},
        )

    assert json.loads(resume_result["content"][0]["text"])["status"] == "implementing"


@pytest.mark.anyio
async def test_claim_stop_resume_flow() -> None:
    """Integration: claim → stop → resume transitions succeed in sequence."""
    with patch(
        "agentception.mcp.build_commands.acknowledge_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        await call_tool_async("build_claim_run", {"run_id": "issue-100"})

    with patch(
        "agentception.mcp.build_commands.stop_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        stop_result = await call_tool_async("build_stop_run", {"run_id": "issue-100"})

    assert json.loads(stop_result["content"][0]["text"])["status"] == "stopped"

    with patch(
        "agentception.mcp.build_commands.resume_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        resume_result = await call_tool_async(
            "build_resume_run",
            {"run_id": "issue-100", "agent_run_id": "issue-100"},
        )

    assert json.loads(resume_result["content"][0]["text"])["status"] == "implementing"


# ---------------------------------------------------------------------------
# Regression: pr-reviewer must not trigger a second reviewer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_complete_run_reviewer_does_not_redispatch_reviewer() -> None:
    """When a pr-reviewer calls build_complete_run, no auto-reviewer is dispatched.

    Regression for the infinite reviewer loop: reviewer merges PR → calls
    build_complete_run → auto_dispatch_reviewer → second reviewer → …
    """
    with (
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
        patch(
            "agentception.mcp.build_commands.complete_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.build_commands.get_agent_run_role",
            new_callable=AsyncMock,
            return_value="pr-reviewer",
        ) as mock_role,
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.create_task"),
    ):
        result = await call_tool_async(
            "build_complete_run",
            {
                "issue_number": 449,
                "pr_url": "https://github.com/owner/repo/pull/553",
                "agent_run_id": "issue-449",
            },
        )

    assert json.loads(result["content"][0]["text"])["ok"] is True
    mock_role.assert_awaited_once_with("issue-449")
    mock_dispatch.assert_not_called()


@pytest.mark.anyio
async def test_build_complete_run_implementer_does_redispatch_reviewer() -> None:
    """When a developer calls build_complete_run, reviewer IS dispatched."""
    with (
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
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
        patch("asyncio.create_task") as mock_create_task,
    ):
        result = await call_tool_async(
            "build_complete_run",
            {
                "issue_number": 42,
                "pr_url": "https://github.com/owner/repo/pull/100",
                "agent_run_id": "issue-42",
            },
        )

    assert json.loads(result["content"][0]["text"])["ok"] is True
    mock_create_task.assert_called_once()


@pytest.mark.anyio
async def test_build_complete_run_releases_worktree_before_reviewer() -> None:
    """Executor worktree is released before the reviewer is dispatched.

    Regression for the "branch already used by worktree" failure: if the
    executor's worktree still holds feat/issue-N when the reviewer tries to
    create its own worktree for the same branch, git rejects the second
    worktree with a fatal error.  build_complete_run must call
    release_worktree (remove dir + prune refs) first.
    """
    with (
        patch(
            "agentception.mcp.build_commands.persist_agent_event",
            new_callable=AsyncMock,
        ),
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
            return_value={"worktree_path": "/worktrees/issue-99", "branch": "feat/issue-99"},
        ),
        patch(
            "agentception.mcp.build_commands.release_worktree",
            new_callable=AsyncMock,
        ) as mock_release,
        patch(
            "agentception.mcp.build_commands.auto_dispatch_reviewer",
            new_callable=AsyncMock,
        ),
        patch("asyncio.create_task"),
    ):
        result = await call_tool_async(
            "build_complete_run",
            {
                "issue_number": 99,
                "pr_url": "https://github.com/owner/repo/pull/200",
                "agent_run_id": "issue-99",
            },
        )

    assert json.loads(result["content"][0]["text"])["ok"] is True
    mock_release.assert_awaited_once_with(
        worktree_path="/worktrees/issue-99",
        repo_dir=str(__import__("agentception.config", fromlist=["settings"]).settings.repo_dir),
    )
