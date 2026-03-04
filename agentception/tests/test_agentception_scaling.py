"""Tests for agentception/intelligence/scaling.py and apply endpoint (AC-501 / AC-502).

Coverage:
- test_no_recommendation_when_balanced: returns no_change when metrics are within bounds
- test_recommend_more_qa_when_pr_backlog: recommends increase_qa_vps when pr_backlog >= 2
- test_recommend_bigger_pool_when_deep_queue: recommends increase_pool when queue deep & agents fast
- test_confidence_low_without_wave_history: returns low confidence when < 3 completed waves
- test_confidence_high_with_sufficient_waves: returns high confidence with >= 3 completed waves
- test_scaling_advice_api_returns_200: GET /api/intelligence/scaling-advice returns 200
- test_apply_increases_correct_field: POST apply writes recommended_value to correct config field
- test_apply_no_change_is_noop: POST apply when no_change leaves config unchanged
- test_banner_hidden_when_dismissed: overview page renders banner markup; dismissed state is client-only

Run targeted:
    pytest agentception/tests/test_agentception_scaling.py -v
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentception.app import app
from agentception.intelligence.scaling import (
    ScalingRecommendation,
    _classify_confidence,
    compute_recommendation,
)
from agentception.models import AgentStatus, AgentNode, PipelineState
from agentception.telemetry import WaveSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(issues_open: int = 0, prs_open: int = 0) -> PipelineState:
    """Build a minimal PipelineState with controllable issue/PR counts."""
    return PipelineState(
        active_label="agentception/5-scaling",
        issues_open=issues_open,
        prs_open=prs_open,
        agents=[],
        alerts=[],
        stale_claims=[],
        polled_at=time.time(),
    )


def _make_wave(duration_minutes: float | None, batch_id: str = "eng-test") -> WaveSummary:
    """Build a WaveSummary with controllable duration.

    When ``duration_minutes`` is None the wave is still in-progress (ended_at=None).
    """
    started_at = time.time() - 3600.0
    ended_at = (started_at + duration_minutes * 60.0) if duration_minutes is not None else None
    return WaveSummary(
        batch_id=batch_id,
        started_at=started_at,
        ended_at=ended_at,
        issues_worked=[1],
        prs_opened=1,
        prs_merged=0,
        estimated_tokens=1000,
        estimated_cost_usd=0.01,
        agents=[],
    )


def _make_pipeline_config(
    max_eng_vps: int = 1,
    max_qa_vps: int = 1,
    pool_size_per_vp: int = 4,
) -> object:
    """Return a PipelineConfig instance with controllable allocation fields."""
    from agentception.models import PipelineConfig

    return PipelineConfig(
        max_eng_vps=max_eng_vps,
        max_qa_vps=max_qa_vps,
        pool_size_per_vp=pool_size_per_vp,
        active_labels_order=["agentception/5-scaling"],
    )


# ---------------------------------------------------------------------------
# Unit: compute_recommendation()
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_recommendation_when_balanced() -> None:
    """Returns no_change when queue depth and PR backlog are within bounds."""
    state = _make_state(issues_open=2, prs_open=1)
    waves = [_make_wave(30.0, f"b{i}") for i in range(3)]  # slow agents, balanced

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(),
    ):
        rec = await compute_recommendation(state, waves)

    assert rec.action == "no_change", f"Expected no_change, got {rec.action!r}: {rec.reason}"
    assert isinstance(rec, ScalingRecommendation)


@pytest.mark.anyio
async def test_recommend_more_qa_when_pr_backlog() -> None:
    """Returns increase_qa_vps when pr_backlog >= 2 and max_qa_vps < 2."""
    state = _make_state(issues_open=1, prs_open=3)  # pr_backlog=3 >= threshold(2)
    waves = [_make_wave(25.0, f"b{i}") for i in range(3)]  # slow waves, no pool trigger

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(max_qa_vps=1),
    ):
        rec = await compute_recommendation(state, waves)

    assert rec.action == "increase_qa_vps", f"Expected increase_qa_vps, got {rec.action!r}"
    assert rec.current_value == 1
    assert rec.recommended_value == 2
    assert "PR backlog" in rec.reason


@pytest.mark.anyio
async def test_recommend_bigger_pool_when_deep_queue() -> None:
    """Returns increase_pool when queue depth > 4, avg_duration < 20 min, and pool_size < 8."""
    state = _make_state(issues_open=6, prs_open=0)  # queue_depth=6 > threshold(4)
    # Three completed fast waves (10 min each) → avg_duration=10 < 20
    waves = [_make_wave(10.0, f"b{i}") for i in range(3)]

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(pool_size_per_vp=4),
    ):
        rec = await compute_recommendation(state, waves)

    assert rec.action == "increase_pool", f"Expected increase_pool, got {rec.action!r}"
    assert rec.current_value == 4
    assert rec.recommended_value == 6  # min(4+2, 8)
    assert "queue depth" in rec.reason.lower()


@pytest.mark.anyio
async def test_confidence_low_without_wave_history() -> None:
    """Returns low confidence when fewer than 3 completed waves are available."""
    state = _make_state(issues_open=0, prs_open=3)  # trigger QA recommendation
    waves: list[WaveSummary] = []  # no wave history at all

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(max_qa_vps=1),
    ):
        rec = await compute_recommendation(state, waves)

    assert rec.confidence == "low", f"Expected low confidence, got {rec.confidence!r}"


@pytest.mark.anyio
async def test_confidence_low_with_one_completed_wave() -> None:
    """Returns low confidence with exactly 1 completed wave."""
    state = _make_state(issues_open=0, prs_open=0)
    waves = [_make_wave(15.0, "b0")]

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(),
    ):
        rec = await compute_recommendation(state, waves)

    assert rec.confidence == "low"


@pytest.mark.anyio
async def test_confidence_medium_with_two_completed_waves() -> None:
    """Returns medium confidence with exactly 2 completed waves."""
    state = _make_state(issues_open=0, prs_open=0)
    waves = [_make_wave(15.0, f"b{i}") for i in range(2)]

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(),
    ):
        rec = await compute_recommendation(state, waves)

    assert rec.confidence == "medium"


@pytest.mark.anyio
async def test_confidence_high_with_sufficient_waves() -> None:
    """Returns high confidence with 3 or more completed waves."""
    state = _make_state(issues_open=0, prs_open=0)
    waves = [_make_wave(15.0, f"b{i}") for i in range(5)]

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(),
    ):
        rec = await compute_recommendation(state, waves)

    assert rec.confidence == "high"


@pytest.mark.anyio
async def test_in_progress_waves_excluded_from_duration() -> None:
    """In-progress waves (ended_at=None) are excluded from avg_duration calculation.

    Even if there are 3 total waves, only completed ones count for duration.
    If the only completed wave is slow (30 min), the pool recommendation
    should NOT be triggered even though the queue is deep.
    """
    state = _make_state(issues_open=6, prs_open=0)
    # Two in-progress waves + one slow completed wave
    waves = [
        _make_wave(None, "in-progress-1"),
        _make_wave(None, "in-progress-2"),
        _make_wave(30.0, "completed"),
    ]

    with patch(
        "agentception.intelligence.scaling.read_pipeline_config",
        new_callable=AsyncMock,
        return_value=_make_pipeline_config(pool_size_per_vp=4),
    ):
        rec = await compute_recommendation(state, waves)

    # avg_duration=30 min (not < 20), so no pool increase
    assert rec.action == "no_change", f"Expected no_change, got {rec.action!r}"


# ---------------------------------------------------------------------------
# Unit: _classify_confidence()
# ---------------------------------------------------------------------------


def test_classify_confidence_low_zero() -> None:
    """Zero completed waves → low confidence."""
    assert _classify_confidence(0) == "low"


def test_classify_confidence_low_one() -> None:
    """One completed wave → low confidence."""
    assert _classify_confidence(1) == "low"


def test_classify_confidence_medium_two() -> None:
    """Two completed waves → medium confidence."""
    assert _classify_confidence(2) == "medium"


def test_classify_confidence_high_three_plus() -> None:
    """Three or more completed waves → high confidence."""
    assert _classify_confidence(3) == "high"
    assert _classify_confidence(10) == "high"


# ---------------------------------------------------------------------------
# API: GET /api/intelligence/scaling-advice
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scaling_advice_api_returns_200() -> None:
    """GET /api/intelligence/scaling-advice returns 200 with a valid ScalingRecommendation."""
    expected = ScalingRecommendation(
        action="no_change",
        reason="Balanced.",
        current_value=0,
        recommended_value=0,
        confidence="high",
    )

    with (
        patch(
            "agentception.routes.intelligence.get_state",
            return_value=_make_state(issues_open=0, prs_open=0),
        ),
        patch(
            "agentception.routes.intelligence.aggregate_waves",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.intelligence.compute_recommendation",
            new_callable=AsyncMock,
            return_value=expected,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/intelligence/scaling-advice")

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "no_change"
    assert body["confidence"] == "high"


# ---------------------------------------------------------------------------
# API: POST /api/intelligence/scaling-advice/apply  (AC-502)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_apply_increases_correct_field() -> None:
    """POST apply writes the recommended_value to the correct PipelineConfig field.

    Scenario: recommendation is increase_pool (pool_size_per_vp: 4 → 6).
    The written config must have pool_size_per_vp == 6; other fields unchanged.
    """
    from agentception.models import PipelineConfig

    initial_config = PipelineConfig(
        max_eng_vps=1,
        max_qa_vps=1,
        pool_size_per_vp=4,
        active_labels_order=["agentception/5-scaling"],
    )
    recommendation = ScalingRecommendation(
        action="increase_pool",
        reason="Queue depth exceeds threshold.",
        current_value=4,
        recommended_value=6,
        confidence="high",
    )
    written: list[PipelineConfig] = []

    async def fake_write(cfg: PipelineConfig) -> PipelineConfig:
        written.append(cfg)
        return cfg

    with (
        patch(
            "agentception.routes.intelligence.get_state",
            return_value=_make_state(issues_open=6, prs_open=0),
        ),
        patch(
            "agentception.routes.intelligence.aggregate_waves",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.intelligence.compute_recommendation",
            new_callable=AsyncMock,
            return_value=recommendation,
        ),
        patch(
            "agentception.routes.intelligence.read_pipeline_config",
            new_callable=AsyncMock,
            return_value=initial_config,
        ),
        patch(
            "agentception.routes.intelligence.write_pipeline_config",
            new_callable=AsyncMock,
            side_effect=fake_write,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/intelligence/scaling-advice/apply")

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == "increase_pool"
    assert body["new_value"] == 6

    assert len(written) == 1, "write_pipeline_config must be called exactly once"
    assert written[0].pool_size_per_vp == 6
    # Other fields must remain unchanged
    assert written[0].max_qa_vps == initial_config.max_qa_vps
    assert written[0].max_eng_vps == initial_config.max_eng_vps


@pytest.mark.anyio
async def test_apply_no_change_is_noop() -> None:
    """POST apply when recommendation is no_change leaves pipeline-config.json untouched.

    write_pipeline_config must NOT be called and the response reflects no_change.
    """
    recommendation = ScalingRecommendation(
        action="no_change",
        reason="Pipeline is balanced.",
        current_value=0,
        recommended_value=0,
        confidence="high",
    )

    with (
        patch(
            "agentception.routes.intelligence.get_state",
            return_value=_make_state(issues_open=2, prs_open=1),
        ),
        patch(
            "agentception.routes.intelligence.aggregate_waves",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.intelligence.compute_recommendation",
            new_callable=AsyncMock,
            return_value=recommendation,
        ),
        patch(
            "agentception.routes.intelligence.write_pipeline_config",
            new_callable=AsyncMock,
        ) as mock_write,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/intelligence/scaling-advice/apply")

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == "no_change"
    assert body["new_value"] == 0
    mock_write.assert_not_called()


def test_banner_hidden_when_dismissed_markup_present() -> None:
    """Overview page HTML includes the scaling advisor banner markup with x-cloak.

    The dismissed state is managed client-side by Alpine.js (x-data/x-show).
    This test verifies the server renders the container element so the browser
    can initialise the Alpine component — it does not execute JavaScript.
    """
    from fastapi.testclient import TestClient
    import time
    from agentception.models import PipelineState

    state = PipelineState(
        active_label="agentception/5-scaling",
        issues_open=3,
        prs_open=1,
        agents=[],
        alerts=[],
        stale_claims=[],
        board_issues=[],
        polled_at=time.time(),
    )

    with patch("agentception.routes.ui.overview.get_state", return_value=state):
        with TestClient(app) as client:
            response = client.get("/")

    assert response.status_code == 200
    html = response.text
    # Banner container with Alpine dismissed-state guard must be rendered.
    # scalingAdvisor() is defined in app.js; the HTML uses it via x-data invocation.
    assert "scalingAdvisor(" in html, "Alpine scalingAdvisor x-data invocation must be in the page"
    # Apply logic (POST /api/intelligence/scaling-advice/apply) lives in app.js,
    # invoked via @click="apply()". Verify the button and app.js are present.
    assert '@click="apply()"' in html, "Apply button must delegate to scalingAdvisor.apply()"
    assert "/static/app.js" in html, "app.js must be loaded for scalingAdvisor to work"
    assert "Dismiss" in html, "Dismiss button must be rendered"
