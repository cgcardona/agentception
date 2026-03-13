"""E2E smoke test harness — poller tick simulation (AC-836).

Exercises the AgentCeption poller end-to-end with all GitHub calls mocked.
No live network, no real filesystem side-effects.

Run targeted:
    docker compose exec agentception pytest agentception/tests/e2e/test_agentception_workflow_e2e.py -v -m e2e
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from agentception.models import PipelineState
from agentception.poller import GitHubBoard, tick

logger = logging.getLogger(__name__)

# All tests in this module are tagged as E2E smoke tests.
pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Poller tick simulation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_poller_tick_returns_pipeline_state() -> None:
    """Poller tick with mocked GitHub readers → PipelineState returned without error.

    Simulates a single tick of the polling loop by mocking all GitHub reader
    calls. Verifies that the tick completes successfully and returns a
    well-formed ``PipelineState``.
    """
    empty_board = GitHubBoard(
        active_label="phase-0/bugs",
        open_issues=[],
        open_prs=[],
        wip_issues=[],
        closed_issues=[],
        merged_prs=[],
    )

    with (
        patch(
            "agentception.poller.list_active_runs",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.poller.build_github_board",
            new_callable=AsyncMock,
            return_value=empty_board,
        ),
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        state = await tick()

    assert isinstance(state, PipelineState), (
        "tick() must return a PipelineState instance"
    )
    assert state.active_label == "phase-0/bugs"
    assert state.agents == []
    assert state.alerts == []
    assert state.polled_at > 0, "polled_at timestamp must be set"
