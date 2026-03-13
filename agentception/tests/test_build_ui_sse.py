"""Tests for the new tool_call / tool_result SSE event shapes in _inspector_sse.

Covers issue #851: extend the inspector SSE stream to emit tool_call and
tool_result message rows.

Run targeted:
    pytest agentception/tests/test_build_ui_sse.py -v
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agentception.db.queries import AgentThoughtRow
from agentception.routes.ui.build_ui import _inspector_sse


def _make_thought(
    seq: int,
    role: str,
    content: str,
    tool_name: str = "",
    recorded_at: str = "2026-01-01T00:00:00",
) -> AgentThoughtRow:
    return AgentThoughtRow(
        seq=seq,
        role=role,
        content=content,
        tool_name=tool_name,
        recorded_at=recorded_at,
    )


@pytest.mark.anyio
async def test_tool_call_emitted() -> None:
    with (
        patch(
            "agentception.routes.ui.build_ui.get_agent_events_tail",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.build_ui.get_agent_thoughts_tail",
            new_callable=AsyncMock,
            side_effect=[
                [_make_thought(0, "tool_call", '{"foo": 1}', tool_name="bash")],
                Exception("stop"),
            ],
        ),
    ):
        gen = _inspector_sse("run-1")
        events = []
        try:
            async for chunk in gen:
                if chunk.startswith("data:"):
                    events.append(json.loads(chunk.removeprefix("data: ").strip()))
        except Exception:
            pass
    tc = [e for e in events if e.get("t") == "tool_call"]
    assert len(tc) == 1
    assert tc[0]["tool_name"] == "bash"
    assert len(tc[0]["args_preview"]) <= 120


@pytest.mark.anyio
async def test_tool_result_emitted() -> None:
    with (
        patch(
            "agentception.routes.ui.build_ui.get_agent_events_tail",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.build_ui.get_agent_thoughts_tail",
            new_callable=AsyncMock,
            side_effect=[
                [_make_thought(0, "tool_result", "ok output", tool_name="bash")],
                Exception("stop"),
            ],
        ),
    ):
        gen = _inspector_sse("run-1")
        events = []
        try:
            async for chunk in gen:
                if chunk.startswith("data:"):
                    events.append(json.loads(chunk.removeprefix("data: ").strip()))
        except Exception:
            pass
    tr = [e for e in events if e.get("t") == "tool_result"]
    assert len(tr) == 1
    assert tr[0]["tool_name"] == "bash"


@pytest.mark.anyio
async def test_file_edit_tool_result_suppressed() -> None:
    with (
        patch(
            "agentception.routes.ui.build_ui.get_agent_events_tail",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.build_ui.get_agent_thoughts_tail",
            new_callable=AsyncMock,
            side_effect=[
                [_make_thought(0, "tool_result", "wrote ok", tool_name="write_file")],
                Exception("stop"),
            ],
        ),
    ):
        gen = _inspector_sse("run-1")
        events = []
        try:
            async for chunk in gen:
                if chunk.startswith("data:"):
                    events.append(json.loads(chunk.removeprefix("data: ").strip()))
        except Exception:
            pass
    assert not any(e.get("t") == "tool_result" for e in events)


@pytest.mark.anyio
async def test_args_preview_truncated() -> None:
    long_content = "x" * 200
    from agentception.routes.ui.build_ui import _preview
    result = _preview(long_content)
    assert result.endswith("…")
    assert len(result) <= 120
