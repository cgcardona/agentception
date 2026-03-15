from __future__ import annotations

"""Integration + regression tests for build command MCP tools.

Tests:
- build_cancel_run:  any active → cancelled; rejects terminal

All tests go through the MCP layer (call_tool_async) to verify end-to-end
dispatch in addition to the unit tests in test_persist_pending_launch_guard.py.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from agentception.mcp.server import call_tool_async


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
