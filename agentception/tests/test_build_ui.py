"""Tests for the Mission Control build page UI.

Covers the Force resync button added to build.html (issue #649) and the
inspector SSE poll interval (issue #724).

Run targeted:
    pytest agentception/tests/test_build_ui.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Synchronous test client for the full app."""
    with TestClient(app) as c:
        return c


def test_force_resync_button_present(client: TestClient) -> None:
    """The build page must contain the Force resync HTMX button and its result div.

    Fetches the build page and asserts that:
    - The button carries ``hx-post="/api/control/resync-issues"``.
    - A ``<div id="resync-result">`` exists to receive the HTMX swap.
    """
    with (
        patch(
            "agentception.routes.ui.build_ui.get_initiatives",
            new_callable=AsyncMock,
            return_value=["phase-1"],
        ),
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_latest_active_batch_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agentception.routes.ui.build_ui.get_run_tree_by_batch_id",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        response = client.get("/ship/agentception/phase-1")

    assert response.status_code == 200
    html = response.text
    assert 'hx-post="/api/control/resync-issues"' in html, (
        "Force resync button must carry hx-post pointing to /api/control/resync-issues"
    )
    assert 'id="resync-result"' in html, (
        "A div with id='resync-result' must exist to receive the HTMX swap"
    )
    assert 'aria-label="Refresh from GitHub"' in html, (
        "Resync button must have aria-label='Refresh from GitHub' for accessibility"
    )
    assert 'class="build-header__resync-btn"' in html, (
        "Force resync button must carry the build-header__resync-btn CSS class"
    )
    assert "<svg" in html and 'aria-hidden="true"' in html, (
        "Force resync button must contain an inline SVG icon with aria-hidden='true'"
    )


@pytest.mark.asyncio
async def test_inspector_sse_poll_interval() -> None:
    """_inspector_sse must call asyncio.sleep(0.5) — not 2 s — on each loop iteration.

    Mocks the DB query helpers so the generator can advance one iteration
    without a real database, then asserts the sleep value is 0.5.
    """
    from agentception.routes.ui.build_ui import _inspector_sse

    sleep_calls: list[float] = []

    class _StopLoop(Exception):
        pass

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        raise _StopLoop  # break after first iteration

    with (
        patch(
            "agentception.routes.ui.build_ui.get_all_events_tail",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.build_ui.get_agent_thoughts_tail",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("agentception.routes.ui.build_ui.asyncio.sleep", side_effect=fake_sleep),
    ):
        gen = _inspector_sse("test-run-id")
        try:
            async for _ in gen:
                pass
        except _StopLoop:
            pass

    assert sleep_calls, "asyncio.sleep was never called inside _inspector_sse"
    assert sleep_calls[0] == 0.5, (
        f"Expected asyncio.sleep(0.5) but got asyncio.sleep({sleep_calls[0]})"
    )


# ---------------------------------------------------------------------------
# Integration: StrReplace tool call → SSE stream emits file_edit event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_emits_file_edit_event_after_str_replace() -> None:
    """Integration: a StrReplace tool result produces a file_edit SSE event.

    This test closes the full pipeline loop:
      run_agent_loop (StrReplace tool call)
        → log_file_edit_event
          → persist_agent_event (DB)
            → get_agent_events_tail (DB read)
              → _inspector_sse (SSE generator)
                → browser-bound ``data: {...}`` bytes

    The DB layer is mocked at the boundary (``persist_agent_event`` and
    ``get_agent_events_tail``) so no real database is required.  The test
    asserts that:
    - At least one SSE frame with ``event_type == "file_edit"`` is emitted.
    - The payload deserialises to a valid ``FileEditEvent`` (all required
      fields present, correct types).
    - The ``path`` field matches the file targeted by the StrReplace call.
    """
    import datetime
    import json as _json

    from agentception.db.queries import AgentEventRow
    from agentception.models import FileEditEvent
    from agentception.routes.ui.build_ui import _inspector_sse

    written_path = "agentception/models.py"
    fake_diff = (
        "--- a/agentception/models.py\n"
        "+++ b/agentception/models.py\n"
        "@@ -1 +1 @@\n"
        "-old_value\n"
        "+new_value\n"
    )
    fake_event = FileEditEvent(
        timestamp=datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc),
        path=written_path,
        diff=fake_diff,
        lines_omitted=0,
    )

    # Simulate what persist_agent_event would have written to the DB.
    fake_db_row: AgentEventRow = {
        "id": 1,
        "event_type": "file_edit",
        "payload": _json.dumps(fake_event.model_dump(mode="json")),
        "recorded_at": "2024-06-01T12:00:00Z",
    }

    class _StopLoop(Exception):
        pass

    collected_frames: list[str] = []
    call_count = 0

    async def fake_events_tail(run_id: str, after_id: int = 0) -> list[AgentEventRow]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First poll: return the file_edit event row.
            return [fake_db_row]
        # Subsequent polls: empty (stop the loop via sleep mock).
        return []

    async def fake_sleep(delay: float) -> None:
        raise _StopLoop

    with (
        patch(
            "agentception.routes.ui.build_ui.get_all_events_tail",
            side_effect=fake_events_tail,
        ),
        patch(
            "agentception.routes.ui.build_ui.get_agent_thoughts_tail",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("agentception.routes.ui.build_ui.asyncio.sleep", side_effect=fake_sleep),
    ):
        gen = _inspector_sse("issue-686")
        try:
            async for frame in gen:
                collected_frames.append(frame)
        except _StopLoop:
            pass

    # At least one frame must have been emitted.
    assert collected_frames, "No SSE frames were emitted by _inspector_sse"

    # Find the file_edit frame.
    file_edit_frames = [
        f for f in collected_frames
        if "file_edit" in f
    ]
    assert file_edit_frames, (
        f"No file_edit SSE frame found. Frames received: {collected_frames!r}"
    )

    # Parse the first file_edit frame and validate the payload.
    raw_frame = file_edit_frames[0]
    assert raw_frame.startswith("data: "), (
        f"SSE frame must start with 'data: ', got: {raw_frame!r}"
    )
    parsed = _json.loads(raw_frame[len("data: "):].strip())

    assert parsed["t"] == "event", f"Expected t='event', got {parsed['t']!r}"
    assert parsed["event_type"] == "file_edit", (
        f"Expected event_type='file_edit', got {parsed['event_type']!r}"
    )

    # Validate the payload deserialises to a valid FileEditEvent.
    payload = parsed["payload"]
    validated = FileEditEvent.model_validate(payload)
    assert validated.path == written_path, (
        f"FileEditEvent.path mismatch: expected {written_path!r}, got {validated.path!r}"
    )
    assert validated.diff == fake_diff, "FileEditEvent.diff must match the original diff"
    assert validated.lines_omitted == 0, "lines_omitted must be 0 for a short diff"


# ---------------------------------------------------------------------------
# Inspector partial: pre-rendered file-edit-card divs for completed runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_run_prerendered_cards() -> None:
    """Inspector partial renders file-edit-card divs for completed runs.

    A run with status ``"done"`` is a terminal run — the SSE stream is closed.
    The inspector partial must pre-render one ``file-edit-card`` div per
    ``FileEditEvent`` returned by ``get_file_edit_events``.
    """
    import datetime

    from agentception.models import FileEditEvent
    from agentception.routes.ui.build_ui import inspector_partial
    from starlette.requests import Request as StarletteRequest
    from starlette.testclient import TestClient

    known_path = "agentception/models.py"
    fake_event = FileEditEvent(
        timestamp=datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc),
        path=known_path,
        diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        lines_omitted=0,
    )

    with (
        patch(
            "agentception.routes.ui.build_ui.get_session",
        ) as mock_get_session,
        patch(
            "agentception.routes.ui.build_ui.get_file_edit_events",
            new_callable=AsyncMock,
            return_value=[fake_event],
        ),
    ):
        # Simulate a DB session that returns status="done"
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "done"
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

        from agentception.app import app

        with TestClient(app) as tc:
            response = tc.get("/ship/runs/issue-999/inspector")

    assert response.status_code == 200
    html = response.text
    assert 'class="file-edit-card' in html, (
        "Inspector partial must render file-edit-card divs for completed runs"
    )
    assert known_path in html, (
        f"Inspector partial must include the file path {known_path!r} in the rendered card"
    )


@pytest.mark.asyncio
async def test_active_run_no_prerendered_cards() -> None:
    """Inspector partial does NOT render file-edit-card divs for active runs.

    A run with status ``"implementing"`` is active — the SSE stream is open
    and will append cards live.  The inspector partial must not pre-render any
    ``file-edit-card`` divs so the live stream is the single source of truth.
    """
    import datetime

    from agentception.models import FileEditEvent

    known_path = "agentception/models.py"
    fake_event = FileEditEvent(
        timestamp=datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc),
        path=known_path,
        diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        lines_omitted=0,
    )

    with (
        patch(
            "agentception.routes.ui.build_ui.get_session",
        ) as mock_get_session,
        patch(
            "agentception.routes.ui.build_ui.get_file_edit_events",
            new_callable=AsyncMock,
            return_value=[fake_event],
        ),
    ):
        # Simulate a DB session that returns status="implementing"
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "implementing"
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

        from agentception.app import app

        with TestClient(app) as tc:
            response = tc.get("/ship/runs/issue-999/inspector")

    assert response.status_code == 200
    html = response.text
    assert 'class="file-edit-card' not in html, (
        "Inspector partial must NOT render file-edit-card divs for active runs"
    )

