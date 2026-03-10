"""Unit tests for agentception.services.agent_loop.

All external I/O is mocked:
  - call_anthropic_with_tools   → controlled ToolResponse stubs
  - build_complete_run / build_cancel_run / log_run_step / log_run_error → AsyncMock
  - call_tool_async             → AsyncMock returning valid ACToolResult
  - _load_task                  → AsyncMock returning a minimal AgentTaskSpec from DB
  - GitHubMCPClient             → MagicMock returning empty tool list
  - settings.worktrees_dir      → redirected to tmp_path
  - settings.repo_dir           → redirected to tmp_path
"""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.mcp.types import ACToolContent, ACToolResult
from agentception.models import AgentTaskSpec
from agentception.services.llm import (
    ToolCall,
    ToolCallFunction,
    ToolDefinition,
    ToolFunction,
    ToolResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_spec(worktree: Path, issue_number: int = 42) -> AgentTaskSpec:
    """Return a minimal AgentTaskSpec that mirrors what _load_task_from_db returns."""
    return AgentTaskSpec(
        id="test-run-1",
        role="developer",
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
# Helpers
# ---------------------------------------------------------------------------


def _mock_github_client() -> MagicMock:
    """Return a MagicMock GitHubMCPClient with an empty tool list."""
    client = MagicMock()
    client.list_tools = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value="")
    client.close = AsyncMock()
    return client


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
                "agentception.services.agent_loop.call_anthropic_with_tools",
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
            patch(
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
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
                "agentception.services.agent_loop.call_anthropic_with_tools",
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
            patch(
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
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
                "agentception.services.agent_loop.call_anthropic_with_tools",
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
            patch(
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
            ),
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
                "agentception.services.agent_loop.call_anthropic_with_tools",
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
            patch(
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
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
                "agentception.services.agent_loop.call_anthropic_with_tools",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Anthropic API is down"),
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
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services.agent_loop import run_agent_loop

            await run_agent_loop("test-run-1")

        mock_cancel.assert_called_once_with("test-run-1")
        mock_error.assert_called_once()
        error_msg = str(mock_error.call_args)
        assert "Anthropic" in error_msg


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


class TestEnforceTurnDelay:
    """Unit tests for _enforce_turn_delay (proactive inter-turn pacing)."""

    def setup_method(self) -> None:
        """Reset the last-call timestamp so tests start clean."""
        import agentception.services.agent_loop as al
        al._last_llm_call_at = 0.0

    @pytest.mark.anyio
    async def test_first_call_is_instant(self) -> None:
        """With no prior call the delay is effectively zero."""
        import time
        import agentception.services.agent_loop as al
        al._last_llm_call_at = 0.0  # simulate never called
        t0 = time.monotonic()
        from agentception.services.agent_loop import _enforce_turn_delay
        await _enforce_turn_delay()
        assert time.monotonic() - t0 < 1.0

    @pytest.mark.anyio
    async def test_recent_call_waits_remainder(self) -> None:
        """A call made 2s ago should wait ~2s (4s target - 2s elapsed)."""
        import time
        import agentception.services.agent_loop as al
        al._last_llm_call_at = time.monotonic() - 2.0
        t0 = time.monotonic()
        from agentception.services.agent_loop import _enforce_turn_delay
        await _enforce_turn_delay()
        elapsed = time.monotonic() - t0
        assert 1.5 < elapsed < 3.0  # ~2s wait, with tolerance

    @pytest.mark.anyio
    async def test_old_call_skips_wait(self) -> None:
        """A call made 15s ago (> 4s target) incurs no extra wait."""
        import time
        import agentception.services.agent_loop as al
        al._last_llm_call_at = time.monotonic() - 15.0
        t0 = time.monotonic()
        from agentception.services.agent_loop import _enforce_turn_delay
        await _enforce_turn_delay()
        assert time.monotonic() - t0 < 1.0

    def test_record_llm_call_updates_timestamp(self) -> None:
        """_record_llm_call stamps _last_llm_call_at so the next delay is measured correctly."""
        import time
        import agentception.services.agent_loop as al
        from agentception.services.agent_loop import _record_llm_call
        al._last_llm_call_at = 0.0
        before = time.monotonic()
        _record_llm_call()
        assert al._last_llm_call_at >= before

    @pytest.mark.anyio
    async def test_retry_backoff_does_not_eat_next_window(self) -> None:
        """Delay is measured from after the LLM call, not before — retries don't collapse the gap."""
        import time
        import agentception.services.agent_loop as al
        from agentception.services.agent_loop import _enforce_turn_delay, _record_llm_call

        # Simulate: _record_llm_call() called just now (LLM call just completed)
        _record_llm_call()
        t0 = time.monotonic()
        await _enforce_turn_delay()
        # Should wait close to _MIN_TURN_DELAY_SECS, not skip due to stale timestamp
        elapsed = time.monotonic() - t0
        assert elapsed >= 3.0  # within 1s tolerance of the 4s target


class TestLLMSSLRetry:
    """Regression tests: transient LLM errors are retried correctly."""

    _TOOLS: list[ToolDefinition] = [
        ToolDefinition(
            type="function",
            function=ToolFunction(
                name="noop",
                description="no-op",
                parameters={"type": "object", "properties": {}},
            ),
        )
    ]
    _MESSAGES: list[dict[str, object]] = [{"role": "user", "content": "hello"}]

    @pytest.mark.anyio
    async def test_ssl_error_is_retried_call_anthropic_with_tools(self) -> None:
        """call_anthropic_with_tools retries on ssl.SSLError instead of crashing."""
        import httpx

        from agentception.services.llm import call_anthropic_with_tools

        call_count = 0

        async def _flaky_post(
            url: str, *, json: object, headers: dict[str, str]
        ) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC")
            resp_data = {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
            resp = httpx.Response(200, json=resp_data)
            resp.request = httpx.Request("POST", url)
            return resp

        mock_client = MagicMock()
        mock_client.post = _flaky_post

        with patch(
            "agentception.services.llm._get_client", return_value=mock_client
        ), patch("agentception.services.llm.asyncio.sleep", new_callable=AsyncMock):
            result = await call_anthropic_with_tools(
                messages=self._MESSAGES,
                system="sys",
                tools=self._TOOLS,
            )

        assert result["content"] == "done"
        assert call_count == 2

    @pytest.mark.anyio
    async def test_ssl_error_exhausts_retries_and_raises(self) -> None:
        """call_anthropic_with_tools raises after all retries are exhausted on persistent ssl.SSLError."""
        import httpx

        from agentception.services.llm import call_anthropic_with_tools

        async def _always_fails(
            url: str, *, json: object, headers: dict[str, str]
        ) -> httpx.Response:
            raise ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC")

        mock_client = MagicMock()
        mock_client.post = _always_fails

        with patch(
            "agentception.services.llm._get_client", return_value=mock_client
        ), patch("agentception.services.llm.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ssl.SSLError):
                await call_anthropic_with_tools(
                    messages=self._MESSAGES,
                    system="sys",
                    tools=self._TOOLS,
                )

    @pytest.mark.anyio
    async def test_429_uses_long_backoff_not_short(self) -> None:
        """429 responses sleep at least _RATE_LIMIT_BACKOFF_SECS, not the 2s used for transient errors."""
        import httpx

        from agentception.services.llm import (
            _RATE_LIMIT_BACKOFF_SECS,
            call_anthropic_with_tools,
        )

        call_count = 0

        async def _flaky_post(
            url: str, *, json: object, headers: dict[str, str]
        ) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = httpx.Response(429, json={"error": "rate_limited"})
                resp.request = httpx.Request("POST", url)
                return resp
            resp_data = {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
            resp = httpx.Response(200, json=resp_data)
            resp.request = httpx.Request("POST", url)
            return resp

        mock_client = MagicMock()
        mock_client.post = _flaky_post
        sleep_calls: list[float] = []

        async def _capture_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with patch(
            "agentception.services.llm._get_client", return_value=mock_client
        ), patch("agentception.services.llm.asyncio.sleep", side_effect=_capture_sleep):
            result = await call_anthropic_with_tools(
                messages=self._MESSAGES,
                system="sys",
                tools=self._TOOLS,
            )

        assert result["content"] == "ok"
        assert call_count == 2
        # The 429 sleep must be >= _RATE_LIMIT_BACKOFF_SECS (not the 2s used for SSL/timeout)
        assert len(sleep_calls) == 1
        assert sleep_calls[0] >= _RATE_LIMIT_BACKOFF_SECS


# ---------------------------------------------------------------------------
# Regression: loop exits when run is already in a terminal state
# ---------------------------------------------------------------------------


class TestTerminalStateGuard:
    """Regression: agent_loop must exit when the DB run status is terminal.

    An agent can call build_cancel_run (or build_complete_run) as an MCP
    tool during its turn.  This transitions the DB status to a terminal state
    but the loop itself has no other signal to stop.  Without this guard the
    loop continues executing in a worktree that the reaper may tear down at
    any moment.
    """

    @pytest.mark.anyio
    async def test_loop_exits_when_run_is_terminal(self, tmp_path: Path) -> None:
        """Loop exits immediately at the status check if the run is terminal."""
        worktree = tmp_path / "test-run-cancel"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree, issue_number=99)

        # Simulate a run that is already cancelled in the DB (e.g. the agent
        # called build_cancel_run as a tool in the previous turn).
        terminal_row: dict[str, object] = {"status": "cancelled"}

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.get_run_by_id",
                new_callable=AsyncMock,
                return_value=terminal_row,
            ),
            patch(
                "agentception.services.agent_loop.call_anthropic_with_tools",
                new_callable=AsyncMock,
                return_value=_stop_response("should not be called"),
            ) as mock_llm,
            patch(
                "agentception.services.agent_loop.build_cancel_run",
                new_callable=AsyncMock,
            ) as mock_cancel,
            patch(
                "agentception.services.agent_loop.log_run_step",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ),
            patch(
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services.agent_loop import run_agent_loop

            await run_agent_loop("test-run-cancel", max_iterations=10)

        # The LLM must NOT have been called — loop should exit before that.
        mock_llm.assert_not_called()
        # The loop exits gracefully without re-cancelling.
        mock_cancel.assert_not_called()


# ---------------------------------------------------------------------------
# Loop guard — runtime enforcement for write-first behaviour
# ---------------------------------------------------------------------------


class TestLoopGuard:
    """Runtime loop guard injects an override when the agent writes no code.

    The guard fires when iteration > _LOOP_GUARD_THRESHOLD AND
    iterations_since_write >= _LOOP_GUARD_THRESHOLD.  A write tool call
    (replace_in_file / write_file / insert_after_in_file / git_commit_and_push)
    resets iterations_since_write to 0.
    """

    @pytest.mark.anyio
    async def test_loop_guard_narrows_tool_list_after_threshold(
        self, tmp_path: Path
    ) -> None:
        """Loop guard removes read-only tools from the active tool palette.

        When fired the model receives a narrowed tool list — read_file,
        search_codebase, list_directory, etc. are absent.  The model cannot
        call them even if it wants to; only write tools remain available.
        This is the primary mechanical enforcement; the text injection is
        secondary.
        """
        from agentception.services.agent_loop import (
            _GUARD_PERMITTED_TOOL_NAMES,
            _LOOP_GUARD_THRESHOLD,
            run_agent_loop,
        )

        worktree = tmp_path / "test-run-guard"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        # Enough read-only iterations to cross the guard threshold, then stop.
        n_reads = _LOOP_GUARD_THRESHOLD + 1
        read_responses = [
            _tool_response("read_file", {"path": "agentception/models.py"})
            for _ in range(n_reads)
        ]
        all_responses = read_responses + [_stop_response("Done.")]

        # Capture both the tools offered to the model and any extra_system_blocks.
        captured_tools: list[list[ToolDefinition]] = []
        captured_extra: list[list[dict[str, object]] | None] = []

        async def fake_llm(
            *args: object,
            tools: list[ToolDefinition] | None = None,
            extra_system_blocks: list[dict[str, object]] | None = None,
            **kwargs: object,
        ) -> ToolResponse:
            captured_tools.append(list(tools or []))
            captured_extra.append(extra_system_blocks)
            return all_responses[len(captured_tools) - 1]

        file_result: dict[str, object] = {"ok": True, "content": "# stub", "truncated": False}

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.get_run_by_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "agentception.services.agent_loop.call_anthropic_with_tools",
                side_effect=fake_llm,
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
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services import agent_loop as al

            with patch.object(al, "read_file", return_value=file_result):
                await run_agent_loop("test-run-guard")

        # Guard fires on the (THRESHOLD+1)-th LLM call (index = THRESHOLD).
        assert len(captured_tools) > _LOOP_GUARD_THRESHOLD, (
            "Expected at least THRESHOLD+1 LLM calls"
        )
        guard_call_tools = captured_tools[_LOOP_GUARD_THRESHOLD]
        guard_tool_names = {t["function"]["name"] for t in guard_call_tools}

        # Every tool in the narrowed palette must be in the permitted set.
        leaked = guard_tool_names - _GUARD_PERMITTED_TOOL_NAMES
        assert not leaked, (
            f"Loop guard must restrict to permitted tools; non-permitted: {leaked}"
        )

        # The extra_system_blocks must contain the LOOP GUARD explanation.
        guard_extra = captured_extra[_LOOP_GUARD_THRESHOLD]
        assert guard_extra is not None
        all_text = " ".join(
            str(b["text"]) for b in guard_extra if isinstance(b.get("text"), str)
        )
        assert "LOOP GUARD" in all_text

        # Pre-guard iterations must have the full tool list (read tools present).
        pre_guard_tools = captured_tools[0]
        pre_guard_names = {t["function"]["name"] for t in pre_guard_tools}
        assert "read_file" in pre_guard_names, (
            "read_file must be available before the guard fires"
        )
        assert "run_command" in pre_guard_names, (
            "run_command must be available before the guard fires"
        )

    @pytest.mark.anyio
    async def test_write_tool_resets_loop_guard_counter(self, tmp_path: Path) -> None:
        """A write tool call resets iterations_since_write; guard must NOT fire."""
        from agentception.services.agent_loop import _LOOP_GUARD_THRESHOLD, run_agent_loop

        worktree = tmp_path / "test-run-write-reset"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        # Pattern: reads up to just below threshold, then a write, then stop.
        # Guard must never fire because the write resets the counter.
        n_reads = _LOOP_GUARD_THRESHOLD - 1
        read_responses = [
            _tool_response("read_file", {"path": "agentception/models.py"})
            for _ in range(n_reads)
        ]
        all_responses = (
            read_responses
            + [_tool_response("write_file", {"path": "agentception/new.py", "content": "# x"})]
            + [_stop_response("Done.")]
        )

        captured_extra: list[list[dict[str, object]] | None] = []

        async def fake_llm(
            *args: object,
            extra_system_blocks: list[dict[str, object]] | None = None,
            **kwargs: object,
        ) -> ToolResponse:
            captured_extra.append(extra_system_blocks)
            return all_responses[len(captured_extra) - 1]

        file_result: dict[str, object] = {"ok": True, "content": "# stub", "truncated": False}
        write_result: dict[str, object] = {"ok": True}

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.get_run_by_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "agentception.services.agent_loop.call_anthropic_with_tools",
                side_effect=fake_llm,
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
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services import agent_loop as al

            with (
                patch.object(al, "read_file", return_value=file_result),
                patch.object(al, "write_file", return_value=write_result),
            ):
                await run_agent_loop("test-run-write-reset")

        # No call should have the LOOP GUARD text — the write reset the counter.
        for blocks in captured_extra:
            if blocks is None:
                continue
            all_text = " ".join(
                str(b["text"]) for b in blocks if isinstance(b.get("text"), str)
            )
            assert "LOOP GUARD" not in all_text, (
                "Loop guard must NOT fire when a write tool was called within the threshold"
            )

    @pytest.mark.anyio
    async def test_symbol_absence_injects_override_on_repeated_search(
        self, tmp_path: Path
    ) -> None:
        """Symbol-absence override fires once when the same search query repeats."""
        from agentception.services.agent_loop import run_agent_loop

        worktree = tmp_path / "test-run-sym"
        worktree.mkdir()
        task_spec = _make_task_spec(worktree)

        repeated_query = "TaskFile model class"
        all_responses = [
            _tool_response("search_codebase", {"query": repeated_query}),
            _tool_response("search_codebase", {"query": repeated_query}),
            _stop_response("Done."),
        ]

        captured_extra: list[list[dict[str, object]] | None] = []

        async def fake_llm(
            *args: object,
            extra_system_blocks: list[dict[str, object]] | None = None,
            **kwargs: object,
        ) -> ToolResponse:
            captured_extra.append(extra_system_blocks)
            return all_responses[len(captured_extra) - 1]

        search_result: dict[str, object] = {"ok": True, "results": []}

        with (
            patch("agentception.services.agent_loop.settings") as mock_settings,
            patch(
                "agentception.services.agent_loop._load_task",
                new_callable=AsyncMock,
                return_value=task_spec,
            ),
            patch(
                "agentception.services.agent_loop.get_run_by_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "agentception.services.agent_loop.call_anthropic_with_tools",
                side_effect=fake_llm,
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
                "agentception.services.agent_loop.GitHubMCPClient",
                return_value=_mock_github_client(),
            ),
        ):
            mock_settings.worktrees_dir = tmp_path
            mock_settings.repo_dir = tmp_path

            from agentception.services import agent_loop as al

            with patch.object(al, "search_codebase", return_value=search_result):
                await run_agent_loop("test-run-sym")

        # After the second search (iteration 2), the third LLM call (index 2)
        # should receive the symbol-absence override.
        found_absence = False
        for blocks in captured_extra:
            if blocks is None:
                continue
            all_text = " ".join(
                str(b["text"]) for b in blocks if isinstance(b.get("text"), str)
            )
            if "SYMBOL ABSENCE" in all_text:
                assert repeated_query in all_text, (
                    "Symbol absence message must include the repeated query term"
                )
                found_absence = True
                break
        assert found_absence, (
            "Symbol absence override must fire after the same query is searched twice"
        )


# ---------------------------------------------------------------------------
# Type-aware truncation — _build_tool_id_map + _truncate_tool_results
# ---------------------------------------------------------------------------


def test_build_tool_id_map_extracts_tool_names() -> None:
    """_build_tool_id_map should produce id → name for every tool_call in history."""
    from agentception.services.agent_loop import _build_tool_id_map

    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "tc_2", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": "file content"},
        {"role": "tool", "tool_call_id": "tc_2", "content": "cmd output"},
    ]
    mapping = _build_tool_id_map(messages)
    assert mapping == {"tc_1": "read_file", "tc_2": "run_command"}


def test_build_tool_id_map_ignores_non_assistant_messages() -> None:
    from agentception.services.agent_loop import _build_tool_id_map

    messages: list[dict[str, object]] = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
    ]
    assert _build_tool_id_map(messages) == {}


def test_truncate_applies_generous_limit_for_read_file() -> None:
    """read_file results are truncated at 12 000 chars (not the old 3 000 limit)."""
    from agentception.services.agent_loop import _truncate_tool_results

    big_content = "x" * 20_000
    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": big_content},
    ]
    result = _truncate_tool_results(messages)
    tool_msg = next(m for m in result if m.get("role") == "tool")
    content = tool_msg["content"]
    assert isinstance(content, str)
    assert "truncated" in content
    # Should preserve the first 12 000 chars and truncate the rest.
    assert content.startswith("x" * 12_000)
    # Must not reach the old 3 000 limit pattern (confirm generous limit applied).
    assert len(content) > 3_000


def test_truncate_applies_tight_limit_for_unknown_tool() -> None:
    """Unknown tool names use the default 3 000-char cap."""
    from agentception.services.agent_loop import _truncate_tool_results

    big_content = "y" * 4_000
    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_x", "type": "function", "function": {"name": "some_mcp_tool", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_x", "content": big_content},
    ]
    result = _truncate_tool_results(messages)
    tool_msg = next(m for m in result if m.get("role") == "tool")
    content = tool_msg["content"]
    assert isinstance(content, str)
    assert content.startswith("y" * 3_000)
    assert "truncated" in content


def test_truncate_does_not_modify_short_content() -> None:
    """Results under the per-tool limit are passed through unchanged."""
    from agentception.services.agent_loop import _truncate_tool_results

    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": "short content"},
    ]
    result = _truncate_tool_results(messages)
    tool_msg = next(m for m in result if m.get("role") == "tool")
    assert tool_msg["content"] == "short content"


def test_truncate_search_codebase_limit() -> None:
    """search_codebase results get 8 000-char limit (between read_file and default)."""
    from agentception.services.agent_loop import _truncate_tool_results

    big_content = "s" * 9_000
    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_s", "type": "function", "function": {"name": "search_codebase", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_s", "content": big_content},
    ]
    result = _truncate_tool_results(messages)
    tool_msg = next(m for m in result if m.get("role") == "tool")
    content = tool_msg["content"]
    assert isinstance(content, str)
    assert content.startswith("s" * 8_000)
    assert "truncated" in content
