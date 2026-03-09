"""Unit tests for agentception.services.agent_loop.

All external I/O is mocked:
  - call_openrouter_with_tools  → controlled ToolResponse stubs
  - build_complete_run / build_cancel_run / log_run_step / log_run_error → AsyncMock
  - call_tool_async             → AsyncMock returning valid ACToolResult
  - _load_task                  → AsyncMock returning a minimal AgentTaskSpec (DB-backed)
  - settings.worktrees_dir      → redirected to tmp_path
  - settings.repo_dir           → redirected to tmp_path
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.mcp.types import ACToolContent, ACToolResult
from agentception.models import AgentTaskSpec
from agentception.services.llm import ToolCall, ToolCallFunction, ToolResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_spec(worktree: Path, issue_number: int = 42) -> AgentTaskSpec:
    """Return a minimal AgentTaskSpec that mirrors what _load_task_from_db returns."""
    return AgentTaskSpec(
        id="test-run-1",
        role="python-developer",
        tier="worker",
        cognitive_arch="Think step by step.",
        issue_number=issue_number,
        worktree=str(worktree),
    )


def _mcp_ok_result(text: str = "ok") -> ACToolResult:
    """Build a valid ACToolResult for use in mocks."""
    return ACToolResult(
        content=[ACToolContent(type="text", text=text)],
        isError=False,
    )


def _stop_response(content: str = "Task complete.") -> ToolResponse:
    return ToolResponse(stop_reason="stop", content=content, tool_calls=[])


def _tool_response(name: str, args: dict[str, object]) -> ToolResponse:
    tc = ToolCall(
        id="call_123",
        type="function",
        function=ToolCallFunction(name=name, arguments=json.dumps(args)),
    )
    return ToolResponse(stop_reason="tool_calls", content="", tool_calls=[tc])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunAgentLoop:
    @pytest.mark.anyio
    async def test_single_turn_stop(self, tmp_path: Path) -> None:
        """Agent loop completes in one turn when the model returns stop."""
        worktree = tmp_path / "test-run-1"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.call_openrouter_with_tools",
                new_callable=AsyncMock,
                return_value=_stop_response("All done."),
            ),
            patch(
                "agentception.services.agent_loop.build_complete_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_complete,
            patch(
                "agentception.services.agent_loop.log_run_step",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services.agent_loop import run_agent_loop

            await run_agent_loop("test-run-1")

        mock_complete.assert_called_once()
        call_kwargs = mock_complete.call_args.kwargs
        assert call_kwargs["issue_number"] == 42
        assert "All done." in call_kwargs["summary"]

    @pytest.mark.anyio
    async def test_tool_call_then_stop(self, tmp_path: Path) -> None:
        """Agent loop dispatches a tool call and continues to stop."""
        worktree = tmp_path / "test-run-1"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        tool_result = {"ok": True, "content": "file content here", "truncated": False}
        responses = [
            _tool_response("read_file", {"path": "README.md"}),
            _stop_response("Done after reading file."),
        ]

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.call_openrouter_with_tools",
                new_callable=AsyncMock,
                side_effect=responses,
            ),
            patch(
                "agentception.services.agent_loop.build_complete_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_complete,
            patch(
                "agentception.services.agent_loop.log_run_step",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services import agent_loop as al

            with patch.object(al, "read_file", return_value=tool_result):
                await al.run_agent_loop("test-run-1")

        mock_complete.assert_called_once()

    @pytest.mark.anyio
    async def test_mcp_tool_dispatched_to_call_tool_async(self, tmp_path: Path) -> None:
        """Non-local tool names are forwarded to call_tool_async."""
        worktree = tmp_path / "test-run-1"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        responses = [
            _tool_response("log_run_step", {"issue_number": 42, "step_name": "Starting"}),
            _stop_response("Done."),
        ]

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.call_openrouter_with_tools",
                new_callable=AsyncMock,
                side_effect=responses,
            ),
            patch(
                "agentception.services.agent_loop.build_complete_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
            patch(
                "agentception.services.agent_loop.log_run_step",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
            patch(
                "agentception.services.agent_loop.call_tool_async",
                new_callable=AsyncMock,
                return_value=_mcp_ok_result("step recorded"),
            ) as mock_mcp,
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services.agent_loop import run_agent_loop

            await run_agent_loop("test-run-1")

        mock_mcp.assert_called_once_with("log_run_step", {"issue_number": 42, "step_name": "Starting"})

    @pytest.mark.anyio
    async def test_iteration_limit_cancels_run(self, tmp_path: Path) -> None:
        """Exceeding max_iterations triggers log_run_error + build_cancel_run."""
        worktree = tmp_path / "test-run-1"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.call_openrouter_with_tools",
                new_callable=AsyncMock,
                return_value=_tool_response("log_run_step", {"issue_number": 42, "step_name": "x"}),
            ),
            patch(
                "agentception.services.agent_loop.build_cancel_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_cancel,
            patch(
                "agentception.services.agent_loop.log_run_error",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_error,
            patch(
                "agentception.services.agent_loop.log_run_step",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
            patch(
                "agentception.services.agent_loop.call_tool_async",
                new_callable=AsyncMock,
                return_value=_mcp_ok_result("step"),
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services.agent_loop import run_agent_loop

            await run_agent_loop("test-run-1", max_iterations=2)

        mock_cancel.assert_called_once_with("test-run-1")
        mock_error.assert_called_once()

    @pytest.mark.anyio
    async def test_missing_task_in_db_cancels_run(self, tmp_path: Path) -> None:
        """run_agent_loop cancels cleanly when the DB row is missing."""
        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "agentception.services.agent_loop.build_cancel_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_cancel,
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services.agent_loop import run_agent_loop

            await run_agent_loop("no-task-run")

        mock_cancel.assert_called_once_with("no-task-run")

    @pytest.mark.anyio
    async def test_llm_error_cancels_run(self, tmp_path: Path) -> None:
        """LLM exception triggers log_run_error + build_cancel_run."""
        worktree = tmp_path / "test-run-1"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.call_openrouter_with_tools",
                new_callable=AsyncMock,
                side_effect=RuntimeError("OpenRouter is down"),
            ),
            patch(
                "agentception.services.agent_loop.build_cancel_run",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_cancel,
            patch(
                "agentception.services.agent_loop.log_run_error",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as mock_error,
            patch(
                "agentception.services.agent_loop.log_run_step",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services.agent_loop import run_agent_loop

            await run_agent_loop("test-run-1")

        mock_cancel.assert_called_once_with("test-run-1")
        mock_error.assert_called_once()
        error_msg = str(mock_error.call_args)
        assert "OpenRouter" in error_msg


class TestBuildSystemPrompt:
    def test_assembles_all_parts(self) -> None:
        from agentception.services.agent_loop import _build_system_prompt

        result = _build_system_prompt("Role content here.", "Think carefully.")
        assert "Role content here." in result
        assert "Think carefully." in result
        assert "Docker container" in result

    def test_empty_role_prompt(self) -> None:
        from agentception.services.agent_loop import _build_system_prompt

        result = _build_system_prompt("", "arch context")
        assert "arch context" in result
        assert "Docker container" in result

    def test_empty_cognitive_arch(self) -> None:
        from agentception.services.agent_loop import _build_system_prompt

        result = _build_system_prompt("Role file.", "")
        assert "Role file." in result
        assert "Docker container" in result


class TestBuildToolDefinitions:
    def test_contains_local_tools(self) -> None:
        from agentception.services.agent_loop import _build_tool_definitions

        defs = _build_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "read_file" in names
        assert "write_file" in names
        assert "list_directory" in names
        assert "search_text" in names
        assert "run_command" in names

    def test_contains_mcp_tools(self) -> None:
        from agentception.services.agent_loop import _build_tool_definitions

        defs = _build_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        # MCP tools should be present
        assert "build_complete_run" in names or "log_run_step" in names

    def test_all_defs_have_required_fields(self) -> None:
        from agentception.services.agent_loop import _build_tool_definitions

        for td in _build_tool_definitions():
            assert td["type"] == "function"
            assert "name" in td["function"]
            assert "description" in td["function"]
            assert "parameters" in td["function"]


class TestDispatchLocalTool:
    @pytest.mark.anyio
    async def test_read_file_dispatch(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_local_tool

        p = tmp_path / "hello.txt"
        p.write_text("hi there")
        result = await _dispatch_local_tool("read_file", {"path": "hello.txt"}, tmp_path)
        assert result["ok"] is True
        assert "hi there" in str(result["content"])

    @pytest.mark.anyio
    async def test_write_file_dispatch(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_local_tool

        result = await _dispatch_local_tool(
            "write_file", {"path": "out.txt", "content": "written!"}, tmp_path
        )
        assert result["ok"] is True
        assert (tmp_path / "out.txt").read_text() == "written!"

    @pytest.mark.anyio
    async def test_list_directory_dispatch(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_local_tool

        (tmp_path / "file.py").write_text("x")
        result = await _dispatch_local_tool("list_directory", {"path": "."}, tmp_path)
        assert result["ok"] is True
        raw_entries = result["entries"]
        assert isinstance(raw_entries, list)
        assert "file.py" in raw_entries

    @pytest.mark.anyio
    async def test_run_command_dispatch(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_local_tool

        result = await _dispatch_local_tool(
            "run_command", {"command": "echo dispatch_ok"}, tmp_path
        )
        assert result["ok"] is True
        assert "dispatch_ok" in str(result["stdout"])

    @pytest.mark.anyio
    async def test_unknown_tool_returns_error(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_local_tool

        result = await _dispatch_local_tool("ghost_tool", {}, tmp_path)
        assert result["ok"] is False
        assert "unknown" in str(result["error"]).lower()

    @pytest.mark.anyio
    async def test_write_file_missing_path_returns_error(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_local_tool

        result = await _dispatch_local_tool("write_file", {"content": "x"}, tmp_path)
        assert result["ok"] is False

    @pytest.mark.anyio
    async def test_run_command_missing_command_returns_error(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_local_tool

        result = await _dispatch_local_tool("run_command", {}, tmp_path)
        assert result["ok"] is False


class TestDispatchToolCalls:
    @pytest.mark.anyio
    async def test_invalid_json_returns_error_message(self, tmp_path: Path) -> None:
        from agentception.services.agent_loop import _dispatch_single_tool

        bad_tc = ToolCall(
            id="call_bad",
            type="function",
            function=ToolCallFunction(name="read_file", arguments="not-valid-json"),
        )
        result = await _dispatch_single_tool(bad_tc, tmp_path, "run-1")
        assert result["ok"] is False
        assert "json" in str(result["error"]).lower()


class TestTpmRateGuard:
    """Unit tests for _tpm_record_and_get_sleep."""

    def setup_method(self) -> None:
        """Clear the module-level window before each test."""
        import agentception.services.agent_loop as al
        al._tpm_window.clear()

    def test_under_limit_returns_zero(self) -> None:
        """Below the token limit → no sleep required."""
        from agentception.services.agent_loop import _tpm_record_and_get_sleep

        sleep = _tpm_record_and_get_sleep(5_000)
        assert sleep == 0.0

    def test_over_limit_returns_positive_sleep(self) -> None:
        """Exceeding the 27K target → a positive sleep duration is returned."""
        from agentception.services.agent_loop import _tpm_record_and_get_sleep

        sleep = _tpm_record_and_get_sleep(28_000)
        assert sleep > 0.0

    def test_multiple_calls_accumulate(self) -> None:
        """Multiple calls within the window accumulate token counts."""
        from agentception.services.agent_loop import _tpm_record_and_get_sleep

        _tpm_record_and_get_sleep(10_000)
        _tpm_record_and_get_sleep(10_000)
        sleep = _tpm_record_and_get_sleep(10_000)
        assert sleep > 0.0
