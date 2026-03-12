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
    assert 'aria-label="Force resync"' in html, (
        "Force resync button must have aria-label='Force resync' for accessibility"
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
            "agentception.routes.ui.build_ui.get_agent_events_tail",
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
