"""Unit tests for activity event emission from agent_loop (issue #940).

Verifies that persist_activity_event is called with the correct subtype and
payload at each log site: tool_invoked, delay, and (via llm.py) llm_iter,
llm_usage, llm_reply, llm_done.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.services.llm import ToolCall, ToolCallFunction


# ---------------------------------------------------------------------------
# tool_invoked
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tool_invoked_event_emitted(tmp_path: Path) -> None:
    """Dispatch path calls persist_activity_event with subtype tool_invoked and payload."""
    (tmp_path / "foo").write_text("hello")
    tc = ToolCall(
        id="call_1",
        type="function",
        function=ToolCallFunction(name="read_file", arguments=json.dumps({"path": "foo"})),
    )
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()

    with patch("agentception.services.agent_loop.persist_activity_event") as mock_persist:
        from agentception.services.agent_loop import _dispatch_single_tool

        result = await _dispatch_single_tool(
            tc, tmp_path, "run-42", session=mock_session
        )

    assert result.get("ok") is True
    mock_persist.assert_called_once()
    call = mock_persist.call_args
    assert call[0][2] == "tool_invoked"
    payload = call[0][3]
    assert payload["tool_name"] == "read_file"
    assert "foo" in str(payload["arg_preview"])
    mock_session.flush.assert_called_once()


# ---------------------------------------------------------------------------
# delay
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delay_event_emitted() -> None:
    """Delay path calls persist_activity_event with subtype delay and payload.secs > 0."""
    import time

    import agentception.services.agent_loop as al

    al._last_llm_call_at = time.monotonic() - 0.5  # 0.5s ago
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()

    with (
        patch("agentception.services.agent_loop.settings") as mock_settings,
        patch("agentception.services.agent_loop.persist_activity_event") as mock_persist,
    ):
        mock_settings.ac_min_turn_delay_secs = 2.0  # need to wait ~1.5s
        from agentception.services.agent_loop import _enforce_turn_delay

        await _enforce_turn_delay(mock_session, "run-42")

    mock_persist.assert_called_once()
    call = mock_persist.call_args
    assert call[0][2] == "delay"
    payload = call[0][3]
    assert payload["secs"] > 0
    mock_session.flush.assert_called_once()
