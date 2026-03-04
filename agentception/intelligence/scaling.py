"""Auto-scaling advisor for the AgentCeption pipeline.

Evaluates current pipeline state against wave history to produce actionable
scaling recommendations.  All thresholds come from ``PipelineConfig`` so the
advisor adapts to operator-configured limits without code changes.

Typical usage::

    from agentception.intelligence.scaling import compute_recommendation

    recommendation = await compute_recommendation(state, waves)
    print(recommendation.action)       # "increase_pool"
    print(recommendation.confidence)   # "high"
"""
from __future__ import annotations

import logging
from statistics import mean
from typing import Literal

from pydantic import BaseModel

from agentception.models import PipelineState
from agentception.readers.pipeline_config import read_pipeline_config
from agentception.telemetry import WaveSummary

logger = logging.getLogger(__name__)

# Minimum number of completed waves required before we trust timing data.
# Fewer waves → "low" confidence because duration estimates are noisy.
_MIN_WAVES_FOR_HIGH_CONFIDENCE = 3

# Threshold: PR backlog large enough to warrant an extra QA VP.
_PR_BACKLOG_THRESHOLD = 2

# Threshold: queue depth that signals under-staffed Engineering VPs.
_QUEUE_DEPTH_THRESHOLD = 4

# Threshold: fast average wave duration (minutes) that indicates agents can
# handle a larger pool without quality degradation.
_AVG_DURATION_FAST_THRESHOLD = 20.0

# Upper bound pool size — never recommend above this value.
_MAX_SAFE_POOL_SIZE = 8


class ScalingRecommendation(BaseModel):
    """A single scaling recommendation produced by :func:`compute_recommendation`.

    The ``action`` field drives the dashboard and CTO role prompt — it is
    a machine-readable verb pair that tells the operator exactly what to change.
    ``confidence`` reflects how many completed waves of history are available:
    fewer than :data:`_MIN_WAVES_FOR_HIGH_CONFIDENCE` completed waves always
    yields ``"low"`` confidence even when the thresholds are clearly breached.
    """

    action: Literal["increase_eng_vps", "increase_qa_vps", "increase_pool", "no_change"]
    reason: str
    current_value: int
    recommended_value: int
    confidence: Literal["high", "medium", "low"]


async def compute_recommendation(
    state: PipelineState,
    waves: list[WaveSummary],
) -> ScalingRecommendation:
    """Evaluate queue depth, PR backlog, and wave durations to suggest a scaling action.

    Decision rules (evaluated in priority order, first match wins):

    1. ``pr_backlog >= 2`` and ``max_qa_vps < 2``  →  increase QA VPs.
    2. ``queue_depth > 4`` and ``avg_duration < 20`` and ``pool_size_per_vp < 8``
       →  increase pool size per VP.
    3. Otherwise  →  no change.

    Confidence is always ``"low"`` when fewer than
    :data:`_MIN_WAVES_FOR_HIGH_CONFIDENCE` completed waves are available,
    regardless of which action is recommended.

    Parameters
    ----------
    state:
        Current pipeline snapshot (provides ``issues_open`` and ``prs_open``).
    waves:
        List of historical wave summaries used to compute mean wave duration.

    Returns
    -------
    ScalingRecommendation
        The recommended action with supporting rationale and confidence level.
    """
    config = await read_pipeline_config()

    queue_depth = state.issues_open
    pr_backlog = state.prs_open

    # Compute mean wave duration (minutes) across completed waves only.
    # In-progress waves (ended_at=None) are excluded so partial data does not
    # artificially deflate the average.
    completed_durations = [
        (w.ended_at - w.started_at) / 60.0
        for w in waves
        if w.ended_at is not None and w.started_at > 0
    ]
    avg_duration = mean(completed_durations) if completed_durations else float("inf")

    # Confidence is based on the number of *completed* waves.
    completed_wave_count = len(completed_durations)
    confidence: Literal["high", "medium", "low"] = _classify_confidence(completed_wave_count)

    # ── Rule 1: QA VP backlog ─────────────────────────────────────────────────
    if pr_backlog >= _PR_BACKLOG_THRESHOLD and config.max_qa_vps < 2:
        logger.info(
            "⚠️  PR backlog %d ≥ %d and max_qa_vps=%d < 2 — recommending increase_qa_vps",
            pr_backlog,
            _PR_BACKLOG_THRESHOLD,
            config.max_qa_vps,
        )
        return ScalingRecommendation(
            action="increase_qa_vps",
            reason=(
                f"PR backlog ({pr_backlog}) has reached the threshold of "
                f"{_PR_BACKLOG_THRESHOLD} but only {config.max_qa_vps} QA VP(s) are "
                "configured.  Adding a second QA VP will clear the review queue faster."
            ),
            current_value=config.max_qa_vps,
            recommended_value=2,
            confidence=confidence,
        )

    # ── Rule 2: Pool size for deep queue with fast agents ────────────────────
    if (
        queue_depth > _QUEUE_DEPTH_THRESHOLD
        and avg_duration < _AVG_DURATION_FAST_THRESHOLD
        and config.pool_size_per_vp < _MAX_SAFE_POOL_SIZE
    ):
        new_pool = min(config.pool_size_per_vp + 2, _MAX_SAFE_POOL_SIZE)
        logger.info(
            "⚠️  Queue depth %d > %d, avg_duration=%.1f min < %.0f — recommending increase_pool",
            queue_depth,
            _QUEUE_DEPTH_THRESHOLD,
            avg_duration,
            _AVG_DURATION_FAST_THRESHOLD,
        )
        return ScalingRecommendation(
            action="increase_pool",
            reason=(
                f"Issue queue depth ({queue_depth}) exceeds {_QUEUE_DEPTH_THRESHOLD} "
                f"while agents complete waves in {avg_duration:.1f} min on average — "
                f"well under the {_AVG_DURATION_FAST_THRESHOLD:.0f}-min threshold.  "
                f"Increasing pool size from {config.pool_size_per_vp} to {new_pool} "
                "will absorb the backlog without overloading reviewers."
            ),
            current_value=config.pool_size_per_vp,
            recommended_value=new_pool,
            confidence=confidence,
        )

    # ── Rule 3: No action needed ──────────────────────────────────────────────
    logger.info("✅ Pipeline is balanced — no scaling action recommended")
    return ScalingRecommendation(
        action="no_change",
        reason=(
            "Current queue depth, PR backlog, and agent throughput are within "
            "acceptable bounds.  No scaling adjustment is required."
        ),
        current_value=0,
        recommended_value=0,
        confidence=confidence,
    )


# ── Private helpers ────────────────────────────────────────────────────────────


def _classify_confidence(completed_wave_count: int) -> Literal["high", "medium", "low"]:
    """Return a confidence tier based on how many completed waves are available.

    Fewer completed waves mean the timing data is sparse and recommendations
    may be based on noise rather than a real trend.

    Tiers:
    - ``"high"``   — 3 or more completed waves.
    - ``"medium"`` — exactly 2 completed waves.
    - ``"low"``    — 0 or 1 completed wave.
    """
    if completed_wave_count >= _MIN_WAVES_FOR_HIGH_CONFIDENCE:
        return "high"
    if completed_wave_count == 2:
        return "medium"
    return "low"
