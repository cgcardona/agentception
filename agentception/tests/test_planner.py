from __future__ import annotations

"""Unit tests for the tool-equipped planner (agentception/services/planner.py).

All LLM calls and Qdrant searches are mocked so tests run offline with no
external dependencies.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.models import ExecutionPlan
from agentception.services.code_indexer import SearchMatch
from agentception.services.planner import _format_search_results, _validate_operations, generate_execution_plan


# ---------------------------------------------------------------------------
# _validate_operations
# ---------------------------------------------------------------------------


class TestValidateOperations:
    def test_valid_replace_op(self) -> None:
        ops = [{"tool": "replace_in_file", "file": "a.py", "old_string": "x", "new_string": "y"}]
        result = _validate_operations(ops, "issue-1", 1)
        assert result is not None
        assert isinstance(result, ExecutionPlan)
        assert len(result.operations) == 1

    def test_valid_write_file_op(self) -> None:
        ops = [{"tool": "write_file", "file": "new.py", "content": "print('hi')"}]
        result = _validate_operations(ops, "issue-1", 1)
        assert result is not None
        assert result.operations[0].tool == "write_file"

    def test_non_list_returns_none(self) -> None:
        assert _validate_operations("not a list", "r", 1) is None

    def test_empty_list_returns_none(self) -> None:
        assert _validate_operations([], "r", 1) is None

    def test_invalid_op_skipped(self) -> None:
        ops = [
            {"tool": "INVALID_TOOL", "file": "a.py"},
            {"tool": "write_file", "file": "b.py", "content": "x"},
        ]
        result = _validate_operations(ops, "r", 1)
        assert result is not None
        assert len(result.operations) == 1
        assert result.operations[0].file == "b.py"

    def test_run_id_and_issue_set(self) -> None:
        ops = [{"tool": "write_file", "file": "f.py", "content": "x"}]
        result = _validate_operations(ops, "issue-42", 42)
        assert result is not None
        assert result.run_id == "issue-42"
        assert result.issue_number == 42


# ---------------------------------------------------------------------------
# _format_search_results
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    def test_empty_returns_no_results(self) -> None:
        assert "No results" in _format_search_results([])

    def test_formats_file_and_content(self) -> None:
        results: list[SearchMatch] = [
            SearchMatch(file="foo.py", score=0.9, chunk="def foo(): pass", start_line=1, end_line=1)
        ]
        out = _format_search_results(results)
        assert "foo.py" in out
        assert "def foo(): pass" in out
        assert "0.90" in out

    def test_multiple_results(self) -> None:
        results: list[SearchMatch] = [
            SearchMatch(file="a.py", score=0.8, chunk="a", start_line=1, end_line=1),
            SearchMatch(file="b.py", score=0.7, chunk="b", start_line=1, end_line=1),
        ]
        out = _format_search_results(results)
        assert "a.py" in out
        assert "b.py" in out


# ---------------------------------------------------------------------------
# generate_execution_plan — full loop integration (mocked LLM + Qdrant)
# ---------------------------------------------------------------------------


def _make_tool_response(
    tool_name: str,
    args: dict[str, object],
    tc_id: str = "tc1",
) -> dict[str, object]:
    """Build a fake ToolResponse that calls a single tool."""
    return {
        "stop_reason": "tool_calls",
        "content": "",
        "tool_calls": [
            {
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args),
                },
            }
        ],
    }


def _make_stop_response() -> dict[str, object]:
    return {"stop_reason": "stop", "content": "done", "tool_calls": []}


@pytest.mark.anyio
async def test_submit_plan_terminates_loop(tmp_path: Path) -> None:
    """submit_plan should terminate the loop and return a valid ExecutionPlan."""
    ops = [{"tool": "write_file", "file": "new.py", "content": "x = 1"}]
    submit_response = _make_tool_response("submit_plan", {"operations": ops})

    with patch(
        "agentception.services.planner.call_anthropic_with_tools",
        new_callable=AsyncMock,
        return_value=submit_response,
    ) as mock_llm:
        result = await generate_execution_plan(
            "issue-1", 1, "Test issue", "body", tmp_path, []
        )

    assert result is not None
    assert len(result.operations) == 1
    assert mock_llm.call_count == 1  # terminated after first turn


@pytest.mark.anyio
async def test_search_codebase_called_before_submit(tmp_path: Path) -> None:
    """search_codebase results should appear in message history before submit_plan."""
    ops = [{"tool": "write_file", "file": "f.py", "content": "pass"}]
    search_response = _make_tool_response(
        "search_codebase", {"query": "some function"}, tc_id="tc1"
    )
    submit_response = _make_tool_response("submit_plan", {"operations": ops}, tc_id="tc2")

    llm_calls = [search_response, submit_response]

    with (
        patch(
            "agentception.services.planner.call_anthropic_with_tools",
            new_callable=AsyncMock,
            side_effect=llm_calls,
        ),
        patch(
            "agentception.services.planner.search_codebase",
            new_callable=AsyncMock,
            return_value=[SearchMatch(file="a.py", score=0.9, chunk="def foo(): pass", start_line=1, end_line=1)],
        ),
    ):
        result = await generate_execution_plan(
            "issue-2", 2, "Search then plan", "body", tmp_path, []
        )

    assert result is not None


@pytest.mark.anyio
async def test_read_file_lines_uses_worktree_path(tmp_path: Path) -> None:
    """read_file_lines should resolve paths relative to worktree_path."""
    (tmp_path / "src.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    ops = [{"tool": "replace_in_file", "file": "src.py", "old_string": "line1", "new_string": "LINE1"}]

    read_response = _make_tool_response(
        "read_file_lines",
        {"path": "src.py", "start_line": 1, "end_line": 2},
        tc_id="tc1",
    )
    submit_response = _make_tool_response("submit_plan", {"operations": ops}, tc_id="tc2")

    with patch(
        "agentception.services.planner.call_anthropic_with_tools",
        new_callable=AsyncMock,
        side_effect=[read_response, submit_response],
    ):
        result = await generate_execution_plan(
            "issue-3", 3, "Read then plan", "body", tmp_path, []
        )

    assert result is not None
    assert result.operations[0].old_string == "line1"


@pytest.mark.anyio
async def test_returns_none_when_submit_plan_never_called(tmp_path: Path) -> None:
    """If the loop exhausts max turns without submit_plan, return None."""
    stop_response = _make_stop_response()

    with patch(
        "agentception.services.planner.call_anthropic_with_tools",
        new_callable=AsyncMock,
        return_value=stop_response,
    ):
        result = await generate_execution_plan(
            "issue-4", 4, "No submit", "body", tmp_path, []
        )

    assert result is None


@pytest.mark.anyio
async def test_returns_none_on_llm_error(tmp_path: Path) -> None:
    """An LLM exception on any turn should return None (fallback to developer)."""
    with patch(
        "agentception.services.planner.call_anthropic_with_tools",
        new_callable=AsyncMock,
        side_effect=RuntimeError("API down"),
    ):
        result = await generate_execution_plan(
            "issue-5", 5, "LLM error", "body", tmp_path, []
        )

    assert result is None


@pytest.mark.anyio
async def test_search_failure_does_not_crash_loop(tmp_path: Path) -> None:
    """A Qdrant error during search_codebase should be caught; loop continues."""
    ops = [{"tool": "write_file", "file": "f.py", "content": "pass"}]
    search_response = _make_tool_response("search_codebase", {"query": "q"}, tc_id="tc1")
    submit_response = _make_tool_response("submit_plan", {"operations": ops}, tc_id="tc2")

    with (
        patch(
            "agentception.services.planner.call_anthropic_with_tools",
            new_callable=AsyncMock,
            side_effect=[search_response, submit_response],
        ),
        patch(
            "agentception.services.planner.search_codebase",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Qdrant unavailable"),
        ),
    ):
        result = await generate_execution_plan(
            "issue-6", 6, "Search error", "body", tmp_path, []
        )

    assert result is not None  # loop continued past the error and submit_plan succeeded


@pytest.mark.anyio
async def test_invalid_submit_plan_ops_returns_none(tmp_path: Path) -> None:
    """submit_plan with all-invalid operations should return None."""
    submit_response = _make_tool_response(
        "submit_plan",
        {"operations": [{"tool": "INVALID", "file": "x.py"}]},
    )

    with patch(
        "agentception.services.planner.call_anthropic_with_tools",
        new_callable=AsyncMock,
        return_value=submit_response,
    ):
        result = await generate_execution_plan(
            "issue-7", 7, "Bad ops", "body", tmp_path, []
        )

    assert result is None
