"""Tests for inspector SSE: get_all_events_tail and activity events in stream (issue #943).

Verifies that get_all_events_tail returns all event types ordered by id, that
the cursor advances correctly, and that activity events are emitted as t=activity.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.queries import AgentEventRow
from agentception.db.queries.events import get_all_events_tail
from agentception.routes.ui.build_ui import _inspector_sse
from agentception.types import JsonValue

_RUN_ID = "run-943"


def _event_row(
    id: int,
    event_type: str,
    payload: dict[str, JsonValue] | str,
    recorded_at: str = "2026-03-13T12:00:00Z",
) -> AgentEventRow:
    return AgentEventRow(
        id=id,
        event_type=event_type,
        payload=json.dumps(payload) if isinstance(payload, dict) else payload,
        recorded_at=recorded_at,
    )


# ---------------------------------------------------------------------------
# get_all_events_tail — returns all event types ordered by id
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_activity_events_in_sse_stream() -> None:
    """get_all_events_tail returns both step_start and activity (shell_done) rows ordered by id."""
    step_row = _event_row(1, "step_start", {"step_name": "reading"}, "2026-03-13T12:00:00Z")
    activity_row = _event_row(
        2,
        "activity",
        {"subtype": "shell_done", "exit_code": 0, "stdout_bytes": 10, "stderr_bytes": 0},
        "2026-03-13T12:00:01Z",
    )

    with patch(
        "agentception.routes.ui.build_ui.get_all_events_tail",
        new_callable=AsyncMock,
        side_effect=[[step_row, activity_row], []],
    ), patch(
        "agentception.routes.ui.build_ui.get_agent_thoughts_tail",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "agentception.routes.ui.build_ui.asyncio.sleep",
        side_effect=Exception("stop"),
    ):
        gen = _inspector_sse(_RUN_ID)
        events: list[dict[str, JsonValue]] = []
        try:
            async for chunk in gen:
                if chunk.startswith("data:"):
                    events.append(json.loads(chunk.removeprefix("data: ").strip()))
        except Exception:
            pass

    event_frames = [e for e in events if e.get("t") == "event"]
    activity_frames = [e for e in events if e.get("t") == "activity"]
    assert len(event_frames) == 1, f"Expected one t=event, got {events}"
    assert event_frames[0]["event_type"] == "step_start"
    assert event_frames[0]["id"] == 1
    assert len(activity_frames) == 1, f"Expected one t=activity, got {events}"
    assert activity_frames[0]["subtype"] == "shell_done"
    assert activity_frames[0]["id"] == 2
    payload = activity_frames[0]["payload"]
    assert isinstance(payload, dict)
    assert payload.get("exit_code") == 0
    # Order: step (id=1) then activity (id=2)
    assert events.index(activity_frames[0]) > events.index(event_frames[0])


def _mock_db_row(id: int, event_type: str, payload: str, recorded_at: str) -> MagicMock:
    row = MagicMock()
    row.id = id
    row.event_type = event_type
    row.payload = payload
    row.recorded_at = MagicMock(isoformat=lambda: recorded_at)
    return row


@pytest.mark.anyio
async def test_sse_cursor_advances() -> None:
    """get_all_events_tail(run_id, after_id=N) returns only rows with id > N."""
    mock_rows = [
        _mock_db_row(6, "activity", '{"subtype":"shell_done"}', "2026-03-13T12:00:00Z"),
        _mock_db_row(7, "step_start", "{}", "2026-03-13T12:00:01Z"),
    ]
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "agentception.db.queries.events.get_session",
        return_value=mock_cm,
    ):
        result = await get_all_events_tail(_RUN_ID, after_id=5)

    assert len(result) == 2
    assert result[0]["id"] == 6
    assert result[1]["id"] == 7


@pytest.mark.anyio
async def test_get_all_events_tail_returns_ordered_by_id() -> None:
    """get_all_events_tail returns rows in id ascending order."""
    mock_rows = [
        _mock_db_row(10, "activity", '{"subtype":"shell_start"}', "2026-03-13T12:00:00Z"),
        _mock_db_row(11, "step_start", "{}", "2026-03-13T12:00:01Z"),
        _mock_db_row(12, "activity", '{"subtype":"shell_done"}', "2026-03-13T12:00:02Z"),
    ]
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "agentception.db.queries.events.get_session",
        return_value=mock_cm,
    ):
        result = await get_all_events_tail(_RUN_ID, after_id=0)

    assert len(result) == 3
    assert [r["id"] for r in result] == [10, 11, 12]
    assert [r["event_type"] for r in result] == ["activity", "step_start", "activity"]
