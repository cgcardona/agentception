from __future__ import annotations

"""Tests for PR 4 MCP query tools.

Covers all 8 new tools via the MCP layer (call_tool_async):
  - query_run
  - query_children
  - query_run_events
  - query_agent_task
  - query_active_runs
  - query_run_tree
  - query_dispatcher_state
  - query_system_health

Each tool is tested for:
  - Success path (ok=True, correct payload shape)
  - Missing/invalid argument handling (isError=True)
  - TOOLS registry presence
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.mcp.server import TOOLS, call_tool_async
from agentception.mcp.types import ACToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(result: ACToolResult) -> dict[str, object]:
    """Decode the first content item of an ACToolResult as JSON."""
    raw: str = result["content"][0]["text"]
    out: dict[str, object] = json.loads(raw)
    return out


# ---------------------------------------------------------------------------
# query_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_run_returns_run_on_success() -> None:
    """query_run MCP tool returns ok=True with run metadata when run exists."""
    mock_row = {
        "run_id": "issue-42",
        "status": "implementing",
        "role": "python-developer",
        "issue_number": 42,
        "pr_number": None,
        "branch": "ac/issue-42",
        "worktree_path": "/worktrees/issue-42",
        "batch_id": "batch-abc",
        "tier": "engineer",
        "org_domain": "engineering",
        "parent_run_id": "issue-10",
        "spawned_at": "2026-03-01T00:00:00+00:00",
        "last_activity_at": None,
        "completed_at": None,
    }
    with patch("agentception.mcp.query_tools.get_run_by_id", new_callable=AsyncMock, return_value=mock_row):
        result = await call_tool_async("query_run", {"run_id": "issue-42"})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    run = payload["run"]
    assert isinstance(run, dict)
    assert run["run_id"] == "issue-42"
    assert run["status"] == "implementing"


@pytest.mark.anyio
async def test_query_run_returns_error_when_not_found() -> None:
    """query_run returns isError=True when the run does not exist."""
    with patch("agentception.mcp.query_tools.get_run_by_id", new_callable=AsyncMock, return_value=None):
        result = await call_tool_async("query_run", {"run_id": "issue-999"})

    assert result["isError"] is True
    assert _payload(result)["ok"] is False


@pytest.mark.anyio
async def test_query_run_missing_run_id_returns_error() -> None:
    """query_run returns isError=True when run_id argument is absent."""
    result = await call_tool_async("query_run", {})
    assert result["isError"] is True


def test_query_run_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_run" in names


# ---------------------------------------------------------------------------
# query_children
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_children_returns_list_on_success() -> None:
    """query_children MCP tool returns ok=True with children list."""
    mock_children = [
        {
            "run_id": "issue-100",
            "status": "implementing",
            "role": "python-developer",
            "issue_number": 100,
            "pr_number": None,
            "branch": None,
            "worktree_path": None,
            "batch_id": "batch-abc",
            "tier": "engineer",
            "org_domain": "engineering",
            "parent_run_id": "issue-42",
            "spawned_at": "2026-03-01T01:00:00+00:00",
            "last_activity_at": None,
            "completed_at": None,
        }
    ]
    with patch(
        "agentception.mcp.query_tools.get_children_by_parent_id",
        new_callable=AsyncMock,
        return_value=mock_children,
    ):
        result = await call_tool_async("query_children", {"run_id": "issue-42"})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    children = payload["children"]
    assert isinstance(children, list)
    assert children[0]["run_id"] == "issue-100"


@pytest.mark.anyio
async def test_query_children_returns_empty_list_when_none() -> None:
    """query_children returns ok=True with empty list when no children exist."""
    with patch(
        "agentception.mcp.query_tools.get_children_by_parent_id",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await call_tool_async("query_children", {"run_id": "issue-42"})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["count"] == 0
    assert payload["children"] == []


@pytest.mark.anyio
async def test_query_children_missing_run_id_returns_error() -> None:
    """query_children returns isError=True when run_id is absent."""
    result = await call_tool_async("query_children", {})
    assert result["isError"] is True


def test_query_children_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_children" in names


# ---------------------------------------------------------------------------
# query_run_events
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_run_events_returns_events() -> None:
    """query_run_events returns ok=True with events list."""
    mock_events = [
        {"id": 1, "event_type": "step_start", "payload": "{}", "recorded_at": "2026-03-01T00:01:00+00:00"},
        {"id": 2, "event_type": "blocker", "payload": '{"msg":"waiting"}', "recorded_at": "2026-03-01T00:02:00+00:00"},
    ]
    with patch(
        "agentception.mcp.query_tools.get_agent_events_tail",
        new_callable=AsyncMock,
        return_value=mock_events,
    ):
        result = await call_tool_async("query_run_events", {"run_id": "issue-42", "after_id": 0})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 2


@pytest.mark.anyio
async def test_query_run_events_passes_after_id() -> None:
    """query_run_events passes after_id correctly to the DB function."""
    with patch(
        "agentception.mcp.query_tools.get_agent_events_tail",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_fn:
        await call_tool_async("query_run_events", {"run_id": "issue-42", "after_id": 5})

    mock_fn.assert_awaited_once_with("issue-42", after_id=5)


@pytest.mark.anyio
async def test_query_run_events_missing_run_id_returns_error() -> None:
    """query_run_events returns isError=True when run_id is absent."""
    result = await call_tool_async("query_run_events", {})
    assert result["isError"] is True


def test_query_run_events_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_run_events" in names


# ---------------------------------------------------------------------------
# query_agent_task
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_agent_task_returns_content(tmp_path: Path) -> None:
    """query_agent_task returns ok=True with file content when .agent-task exists."""
    task_file = tmp_path / ".agent-task"
    task_file.write_text("[agent]\nrun_id = 42\n")

    mock_teardown = {"worktree_path": str(tmp_path), "branch": "ac/issue-42"}
    with patch(
        "agentception.mcp.query_tools.get_agent_run_teardown",
        new_callable=AsyncMock,
        return_value=mock_teardown,
    ):
        result = await call_tool_async("query_agent_task", {"run_id": "issue-42"})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert "run_id = 42" in str(payload["content"])


@pytest.mark.anyio
async def test_query_agent_task_returns_error_when_run_not_found() -> None:
    """query_agent_task returns isError=True when run does not exist."""
    with patch(
        "agentception.mcp.query_tools.get_agent_run_teardown",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await call_tool_async("query_agent_task", {"run_id": "issue-999"})

    assert result["isError"] is True
    assert _payload(result)["ok"] is False


@pytest.mark.anyio
async def test_query_agent_task_returns_error_when_file_missing(tmp_path: Path) -> None:
    """query_agent_task returns isError=True when .agent-task file does not exist."""
    mock_teardown = {"worktree_path": str(tmp_path), "branch": "ac/issue-42"}
    with patch(
        "agentception.mcp.query_tools.get_agent_run_teardown",
        new_callable=AsyncMock,
        return_value=mock_teardown,
    ):
        result = await call_tool_async("query_agent_task", {"run_id": "issue-42"})

    assert result["isError"] is True
    assert _payload(result)["ok"] is False


@pytest.mark.anyio
async def test_query_agent_task_missing_run_id_returns_error() -> None:
    """query_agent_task returns isError=True when run_id is absent."""
    result = await call_tool_async("query_agent_task", {})
    assert result["isError"] is True


def test_query_agent_task_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_agent_task" in names


# ---------------------------------------------------------------------------
# query_active_runs
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_active_runs_returns_list() -> None:
    """query_active_runs MCP tool returns ok=True with active runs."""
    mock_runs = [
        {
            "run_id": "issue-1",
            "status": "implementing",
            "role": "python-developer",
            "issue_number": 1,
            "pr_number": None,
            "branch": None,
            "worktree_path": None,
            "batch_id": "batch-x",
            "tier": "engineer",
            "org_domain": "engineering",
            "parent_run_id": None,
            "spawned_at": "2026-03-01T00:00:00+00:00",
            "last_activity_at": None,
            "completed_at": None,
        }
    ]
    with patch(
        "agentception.mcp.query_tools.get_active_runs",
        new_callable=AsyncMock,
        return_value=mock_runs,
    ):
        result = await call_tool_async("query_active_runs", {})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1


def test_query_active_runs_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_active_runs" in names


# ---------------------------------------------------------------------------
# query_run_tree
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_run_tree_returns_nodes() -> None:
    """query_run_tree MCP tool returns ok=True with tree nodes."""
    mock_nodes = [
        {
            "id": "issue-1",
            "role": "cto",
            "status": "implementing",
            "agent_status": "implementing",
            "tier": "coordinator",
            "org_domain": "c-suite",
            "parent_run_id": None,
            "issue_number": None,
            "pr_number": None,
            "batch_id": "batch-x",
            "spawned_at": "2026-03-01T00:00:00+00:00",
            "last_activity_at": None,
            "current_step": None,
        }
    ]
    with patch(
        "agentception.mcp.query_tools.get_run_tree_by_batch_id",
        new_callable=AsyncMock,
        return_value=mock_nodes,
    ):
        result = await call_tool_async("query_run_tree", {"batch_id": "batch-x"})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    nodes = payload["nodes"]
    assert isinstance(nodes, list)
    assert nodes[0]["id"] == "issue-1"


@pytest.mark.anyio
async def test_query_run_tree_missing_batch_id_returns_error() -> None:
    """query_run_tree returns isError=True when batch_id is absent."""
    result = await call_tool_async("query_run_tree", {})
    assert result["isError"] is True


def test_query_run_tree_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_run_tree" in names


# ---------------------------------------------------------------------------
# query_dispatcher_state
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_dispatcher_state_returns_state() -> None:
    """query_dispatcher_state MCP tool returns ok=True with status counts."""
    mock_counts = [
        {"status": "implementing", "count": 3},
        {"status": "completed", "count": 10},
    ]
    with (
        patch(
            "agentception.mcp.query_tools.get_run_status_counts",
            new_callable=AsyncMock,
            return_value=mock_counts,
        ),
        patch(
            "agentception.mcp.query_tools.get_latest_active_batch_id",
            new_callable=AsyncMock,
            return_value="batch-xyz",
        ),
    ):
        result = await call_tool_async("query_dispatcher_state", {})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["active_count"] == 3
    assert payload["latest_batch_id"] == "batch-xyz"
    sc = payload["status_counts"]
    assert isinstance(sc, list)
    assert len(sc) == 2


def test_query_dispatcher_state_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_dispatcher_state" in names


# ---------------------------------------------------------------------------
# query_system_health
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_system_health_db_up() -> None:
    """query_system_health returns ok=True with db_ok=True when DB is reachable."""
    mock_counts = [{"status": "implementing", "count": 2}]
    with (
        patch(
            "agentception.mcp.query_tools.check_db_reachable",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.query_tools.get_run_status_counts",
            new_callable=AsyncMock,
            return_value=mock_counts,
        ),
    ):
        result = await call_tool_async("query_system_health", {})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["db_ok"] is True
    assert payload["total_runs"] == 2


@pytest.mark.anyio
async def test_query_system_health_db_down() -> None:
    """Regression: query_system_health returns ok=True but db_ok=False when DB is unreachable.

    The tool must never raise — it degrades gracefully so supervisory agents
    can detect the outage without crashing.
    """
    with patch(
        "agentception.mcp.query_tools.check_db_reachable",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await call_tool_async("query_system_health", {})

    assert result["isError"] is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["db_ok"] is False
    assert payload["total_runs"] == 0
    assert payload["status_counts"] == []


def test_query_system_health_in_tools_list() -> None:
    names = [t["name"] for t in TOOLS]
    assert "query_system_health" in names
