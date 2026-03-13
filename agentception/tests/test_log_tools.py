from __future__ import annotations

"""Tests for agentception/mcp/log_tools.py — specifically log_file_edit_event."""

import datetime
from unittest.mock import AsyncMock, patch

import pytest

from agentception.models import FileEditEvent


@pytest.mark.anyio
async def test_log_file_edit_event_persists_row() -> None:
    """log_file_edit_event calls persist_agent_event with event_type='file_edit'
    and a payload containing the expected FileEditEvent fields."""
    event = FileEditEvent(
        timestamp=datetime.datetime(2024, 1, 1, 12, 0, 0),
        path="agentception/mcp/log_tools.py",
        diff="--- a/log_tools.py\n+++ b/log_tools.py\n@@ -1 +1 @@\n-old\n+new",
        lines_omitted=0,
    )

    with patch(
        "agentception.mcp.log_tools.persist_agent_event",
        new_callable=AsyncMock,
    ) as mock_persist:
        from agentception.mcp.log_tools import log_file_edit_event

        await log_file_edit_event(42, event, agent_run_id="issue-42")

    mock_persist.assert_awaited_once()
    call_kwargs = mock_persist.call_args.kwargs
    assert call_kwargs["issue_number"] == 42
    assert call_kwargs["event_type"] == "file_edit"
    assert call_kwargs["agent_run_id"] == "issue-42"

    payload = call_kwargs["payload"]
    assert "path" in payload
    assert "diff" in payload
    assert "lines_omitted" in payload
    assert "timestamp" in payload
    assert payload["path"] == "agentception/mcp/log_tools.py"
    assert payload["lines_omitted"] == 0
