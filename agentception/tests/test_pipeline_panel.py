from __future__ import annotations

"""Tests for the phase-gate control panel (issue #831).

Covers:
  - GET / returns 200 and renders the phase lanes section.
  - ``phase_lanes`` context key is present and non-empty when labels are configured.
  - gate_status values are exclusively "waiting", "ready", or "done".
  - Pure helper: done detection, ready detection, waiting detection, blockers.
  - Partial HTML: lane names, badge text, blocker links appear in the page.

Run targeted:
    pytest agentception/tests/test_pipeline_panel.py -v
"""

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.intelligence.pipeline_lanes import compute_phase_lanes
from agentception.models import AgentNode, AgentStatus, BoardIssue, PipelineState


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client with full lifespan."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def labels() -> list[str]:
    return [
        "ac-ui/0-critical-bugs",
        "ac-ui/1-design-tokens",
        "ac-ui/2-data-model",
    ]


@pytest.fixture()
def state_with_lanes(labels: list[str]) -> PipelineState:
    """PipelineState with board issues spread across two phases."""
    return PipelineState(
        active_label="ac-ui/0-critical-bugs",
        issues_open=3,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
        board_issues=[
            BoardIssue(number=10, title="Bug alpha", phase_label="ac-ui/0-critical-bugs"),
            BoardIssue(number=11, title="Bug beta", phase_label="ac-ui/0-critical-bugs"),
            BoardIssue(number=20, title="Token issue", phase_label="ac-ui/1-design-tokens"),
        ],
    )


@pytest.fixture()
def empty_state() -> PipelineState:
    return PipelineState(
        active_label=None,
        issues_open=0,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
    )


# ── HTTP route tests ──────────────────────────────────────────────────────────


def _make_pipeline_cfg(labels: list[str]) -> object:
    """Return a PipelineConfig mock with the required fields populated."""
    from agentception.models import PipelineConfig as _PC

    return _PC.model_validate(
        {
            "max_eng_vps": 1,
            "max_qa_vps": 1,
            "pool_size_per_vp": 4,
            "active_labels_order": labels,
        }
    )


def test_overview_returns_200_with_phase_lanes(
    client: TestClient, state_with_lanes: PipelineState, labels: list[str]
) -> None:
    """GET / must return HTTP 200 even when phase lanes are populated."""
    mock_cfg = AsyncMock(return_value=_make_pipeline_cfg(labels))
    with (
        patch("agentception.routes.ui.overview.get_state", return_value=state_with_lanes),
        patch("agentception.routes.ui.overview.read_pipeline_config", mock_cfg),
    ):
        response = client.get("/")
    assert response.status_code == 200


def test_overview_contains_phase_lanes_section(
    client: TestClient, state_with_lanes: PipelineState, labels: list[str]
) -> None:
    """GET / HTML must include the phase-lanes CSS class when lanes are computed."""
    mock_cfg = AsyncMock(return_value=_make_pipeline_cfg(labels))
    with (
        patch("agentception.routes.ui.overview.get_state", return_value=state_with_lanes),
        patch("agentception.routes.ui.overview.read_pipeline_config", mock_cfg),
    ):
        response = client.get("/")
    assert "phase-lanes" in response.text


def test_overview_phase_lanes_show_label_names(
    client: TestClient, state_with_lanes: PipelineState, labels: list[str]
) -> None:
    """GET / HTML must render the phase label names inside the lane strip."""
    mock_cfg = AsyncMock(return_value=_make_pipeline_cfg(labels))
    with (
        patch("agentception.routes.ui.overview.get_state", return_value=state_with_lanes),
        patch("agentception.routes.ui.overview.read_pipeline_config", mock_cfg),
    ):
        response = client.get("/")
    for lbl in labels:
        assert lbl in response.text


def test_overview_phase_lanes_gate_badges_present(
    client: TestClient, state_with_lanes: PipelineState, labels: list[str]
) -> None:
    """GET / HTML must include at least one phase-gate-badge element."""
    mock_cfg = AsyncMock(return_value=_make_pipeline_cfg(labels))
    with (
        patch("agentception.routes.ui.overview.get_state", return_value=state_with_lanes),
        patch("agentception.routes.ui.overview.read_pipeline_config", mock_cfg),
    ):
        response = client.get("/")
    assert "phase-gate-badge" in response.text


def test_overview_waiting_lane_shows_blocker_link(
    client: TestClient, state_with_lanes: PipelineState, labels: list[str]
) -> None:
    """GET / HTML must render blocker issue links for waiting-status lanes.

    Phase ac-ui/1-design-tokens has open issues while ac-ui/0-critical-bugs
    also has open issues, so it must be gated (waiting) and show blockers.
    """
    mock_cfg = AsyncMock(return_value=_make_pipeline_cfg(labels))
    with (
        patch("agentception.routes.ui.overview.get_state", return_value=state_with_lanes),
        patch("agentception.routes.ui.overview.read_pipeline_config", mock_cfg),
    ):
        response = client.get("/")
    # The blocker links point to upstream phase issues (#10 or #11).
    assert "blocker-link" in response.text or "#10" in response.text or "#11" in response.text


# ── Unit tests for compute_phase_lanes ───────────────────────────────────────


def test_compute_phase_lanes_empty_labels() -> None:
    """compute_phase_lanes returns [] when no labels are configured."""
    result = compute_phase_lanes(labels=[], board_issues=[], agents=[])
    assert result == []


def test_compute_phase_lanes_done_when_no_open_issues(labels: list[str]) -> None:
    """A phase with zero open issues must be marked 'done'."""
    result = compute_phase_lanes(labels=labels, board_issues=[], agents=[])
    for lane in result:
        assert lane["gate_status"] == "done"


def test_compute_phase_lanes_ready_when_first_phase_has_issues(labels: list[str]) -> None:
    """The first phase is always 'ready' when it has open issues (no upstream)."""
    board = [BoardIssue(number=1, title="X", phase_label=labels[0])]
    result = compute_phase_lanes(labels=labels, board_issues=board, agents=[])
    first = result[0]
    assert first["gate_status"] == "ready"
    assert first["open_count"] == 1


def test_compute_phase_lanes_waiting_when_upstream_open(labels: list[str]) -> None:
    """A downstream phase must be 'waiting' while any upstream phase has open issues."""
    board = [
        BoardIssue(number=1, title="Upstream bug", phase_label=labels[0]),
        BoardIssue(number=2, title="Downstream work", phase_label=labels[1]),
    ]
    result = compute_phase_lanes(labels=labels, board_issues=board, agents=[])
    assert result[0]["gate_status"] == "ready"
    assert result[1]["gate_status"] == "waiting"
    assert result[2]["gate_status"] == "done"


def test_compute_phase_lanes_blockers_point_to_upstream(labels: list[str]) -> None:
    """Blockers list must contain issues from the first upstream open phase."""
    board = [
        BoardIssue(number=10, title="Blocker issue", phase_label=labels[0]),
        BoardIssue(number=20, title="Downstream issue", phase_label=labels[1]),
    ]
    result = compute_phase_lanes(labels=labels, board_issues=board, agents=[])
    waiting_lane = result[1]
    assert waiting_lane["gate_status"] == "waiting"
    raw_blockers = waiting_lane["blockers"]
    assert isinstance(raw_blockers, list)
    blocker_numbers = [b["number"] for b in raw_blockers]
    assert 10 in blocker_numbers


def test_compute_phase_lanes_gate_status_values_are_valid(labels: list[str]) -> None:
    """gate_status for every lane must be one of: waiting, ready, done."""
    valid = {"waiting", "ready", "done"}
    board = [BoardIssue(number=5, title="Issue", phase_label=labels[0])]
    result = compute_phase_lanes(labels=labels, board_issues=board, agents=[])
    for lane in result:
        assert lane["gate_status"] in valid


def test_compute_phase_lanes_agent_count(labels: list[str]) -> None:
    """agent_count must reflect agents whose issue is in the given phase."""
    board = [BoardIssue(number=99, title="Impl", phase_label=labels[0])]
    agent = AgentNode(id="a1", role="developer", status=AgentStatus.IMPLEMENTING, issue_number=99)
    result = compute_phase_lanes(labels=labels, board_issues=board, agents=[agent])
    assert result[0]["agent_count"] == 1
    assert result[1]["agent_count"] == 0


def test_compute_phase_lanes_ready_when_upstream_done(labels: list[str]) -> None:
    """A phase becomes 'ready' once all upstream phases have 0 open issues."""
    # Only phase index 1 has an open issue; index 0 is empty (done).
    board = [BoardIssue(number=21, title="Token", phase_label=labels[1])]
    result = compute_phase_lanes(labels=labels, board_issues=board, agents=[])
    assert result[0]["gate_status"] == "done"
    assert result[1]["gate_status"] == "ready"
    assert result[2]["gate_status"] == "done"
