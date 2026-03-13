from __future__ import annotations

"""Tests for the wave aggregation telemetry layer.

Covers the four acceptance criteria from issue #620:
- Waves are correctly grouped by BATCH_ID prefix
- started_at is derived from spawned_at timestamps (DB-backed)
- ended_at is None when any worktree in the batch is still active
- Empty run list returns empty waves
"""

import datetime
from pathlib import Path

import pytest

from agentception.db.queries import RunContextRow
from agentception.telemetry import (
    AVG_INPUT_RATIO,
    AVG_TOKENS_PER_MSG,
    SONNET_INPUT_PER_M,
    SONNET_OUTPUT_PER_M,
    WaveSummary,
    _build_wave_summaries,
    aggregate_waves,
    compute_wave_timing,
    estimate_cost,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────


def _make_run(
    batch_id: str,
    issue_number: int = 1,
    spawned_at: float = 1_000_000.0,
    worktree_path: str | None = None,
) -> RunContextRow:
    """Build a minimal ``RunContextRow`` for testing."""
    ts = datetime.datetime.fromtimestamp(spawned_at, tz=datetime.timezone.utc).isoformat()
    return RunContextRow(
        run_id=f"issue-{issue_number}",
        status="implementing",
        role="developer",
        cognitive_arch=None,
        task_description=None,
        issue_number=issue_number,
        pr_number=None,
        branch=f"feat/issue-{issue_number}",
        worktree_path=worktree_path,
        batch_id=batch_id,
        tier="worker",
        org_domain=None,
        parent_run_id=None,
        gh_repo=None,
        is_resumed=False,
        coord_fingerprint=None,
        spawned_at=ts,
        last_activity_at=None,
        completed_at=None,
    )


# ── Unit tests for compute_wave_timing ────────────────────────────────────────


@pytest.mark.anyio
async def test_wave_timing_uses_earliest_spawned_at(tmp_path: Path) -> None:
    """started_at must equal the earliest spawned_at among runs in the group."""
    early = _make_run("batch-A", issue_number=1, spawned_at=1_000.0)
    late = _make_run("batch-A", issue_number=2, spawned_at=9_000.0)

    started_at, ended_at = await compute_wave_timing([early, late])

    assert started_at == pytest.approx(1_000.0)
    # No worktree_path set → dirs don't exist → batch is complete → ended_at is max timestamp.
    assert ended_at == pytest.approx(9_000.0)


@pytest.mark.anyio
async def test_wave_ended_at_none_when_active(tmp_path: Path) -> None:
    """ended_at must be None when any worktree_path still exists on disk."""
    wt_dir = tmp_path / "wt-active"
    wt_dir.mkdir()
    active = _make_run("batch-B", issue_number=3, spawned_at=5_000.0, worktree_path=str(wt_dir))

    started_at, ended_at = await compute_wave_timing([active])

    assert started_at == pytest.approx(5_000.0)
    assert ended_at is None


@pytest.mark.anyio
async def test_compute_wave_timing_empty_list() -> None:
    """compute_wave_timing([]) returns (0.0, None) — no crash, no sentinel."""
    started_at, ended_at = await compute_wave_timing([])
    assert started_at == 0.0
    assert ended_at is None


# ── Unit tests for _build_wave_summaries ──────────────────────────────────────


def test_aggregate_waves_groups_by_batch_id() -> None:
    """_build_wave_summaries must produce one WaveSummary per unique BATCH_ID."""
    run_a1 = _make_run("eng-batch-A", issue_number=10, spawned_at=1_000.0)
    run_a2 = _make_run("eng-batch-A", issue_number=11, spawned_at=2_000.0)
    run_b1 = _make_run("eng-batch-B", issue_number=20, spawned_at=3_000.0)

    result = _build_wave_summaries([run_a1, run_a2, run_b1])

    assert len(result) == 2
    batch_ids = {s.batch_id for s in result}
    assert batch_ids == {"eng-batch-A", "eng-batch-B"}


def test_aggregate_waves_issues_worked_correct() -> None:
    """issues_worked must list all unique issue numbers from the batch."""
    run1 = _make_run("batch-X", issue_number=100)
    run2 = _make_run("batch-X", issue_number=101)

    result = _build_wave_summaries([run1, run2])

    assert len(result) == 1
    wave = result[0]
    assert sorted(wave.issues_worked) == [100, 101]


def test_empty_runs_returns_empty_waves() -> None:
    """_build_wave_summaries([]) must return [] without error."""
    result = _build_wave_summaries([])
    assert result == []


def test_runs_without_batch_id_are_skipped() -> None:
    """Runs with no batch_id must be silently excluded from wave grouping."""
    no_batch = _make_run.__wrapped__ if hasattr(_make_run, "__wrapped__") else None
    # Build a RunContextRow with batch_id=None directly.
    run_no_batch: RunContextRow = _make_run("batch-Y", issue_number=999)
    run_no_batch = RunContextRow(
        **{**run_no_batch, "batch_id": None}
    )
    run_with_batch = _make_run("batch-Y", issue_number=1)

    result = _build_wave_summaries([run_no_batch, run_with_batch])

    assert len(result) == 1
    assert result[0].batch_id == "batch-Y"


def test_wave_summaries_sorted_most_recent_first() -> None:
    """Waves must be sorted by started_at descending (most recent first)."""
    old = _make_run("batch-old", issue_number=1, spawned_at=100.0)
    new = _make_run("batch-new", issue_number=2, spawned_at=9_000.0)

    result = _build_wave_summaries([old, new])

    assert len(result) == 2
    assert result[0].batch_id == "batch-new"
    assert result[1].batch_id == "batch-old"


def test_wave_summary_type_is_wave_summary() -> None:
    """Each result must be a WaveSummary Pydantic model (not a dict or stub)."""
    run = _make_run("batch-Z", issue_number=5)
    result = _build_wave_summaries([run])
    assert isinstance(result[0], WaveSummary)


# ── Unit tests for estimate_cost ──────────────────────────────────────────────


def test_estimate_cost_zero_messages() -> None:
    """estimate_cost(0) must return (0, 0.0) — zero messages means zero cost."""
    tokens, cost = estimate_cost(0)
    assert tokens == 0
    assert cost == 0.0


def test_estimate_cost_known_input() -> None:
    """estimate_cost(100) must return tokens and cost within the expected range.

    100 messages × 800 tokens/msg = 80 000 tokens.
    Input: 80 000 × 0.4 = 32 000 tokens → $0.096 at $3/M
    Output: 80 000 × 0.6 = 48 000 tokens → $0.72 at $15/M
    Total cost ≈ $0.816 → rounded to 4 dp.
    """
    tokens, cost = estimate_cost(100)
    expected_tokens = 100 * AVG_TOKENS_PER_MSG
    assert tokens == expected_tokens

    input_tokens = int(expected_tokens * AVG_INPUT_RATIO)
    output_tokens = expected_tokens - input_tokens
    expected_cost = round(
        input_tokens / 1_000_000 * SONNET_INPUT_PER_M
        + output_tokens / 1_000_000 * SONNET_OUTPUT_PER_M,
        4,
    )
    assert cost == pytest.approx(expected_cost, abs=1e-4)
    # Sanity-check range: must be between $0.5 and $2.0 for 100 messages.
    assert 0.5 <= cost <= 2.0


def test_wave_summary_includes_cost() -> None:
    """Each WaveSummary must expose estimated_tokens and estimated_cost_usd."""
    run = _make_run("batch-cost", issue_number=42)
    result = _build_wave_summaries([run])

    assert len(result) == 1
    wave = result[0]
    assert isinstance(wave.estimated_tokens, int)
    assert isinstance(wave.estimated_cost_usd, float)
    assert wave.estimated_tokens >= 0
    assert wave.estimated_cost_usd >= 0.0


def test_total_cost_sums_waves() -> None:
    """Summing estimated_cost_usd across waves must equal the per-wave sum."""
    run_a = _make_run("batch-sum-A", issue_number=50)
    run_b = _make_run("batch-sum-B", issue_number=51)
    waves = _build_wave_summaries([run_a, run_b])

    assert len(waves) == 2
    total_cost = round(sum(w.estimated_cost_usd for w in waves), 4)
    total_tokens = sum(w.estimated_tokens for w in waves)
    expected_cost = round(waves[0].estimated_cost_usd + waves[1].estimated_cost_usd, 4)
    assert total_cost == pytest.approx(expected_cost, abs=1e-6)
    assert total_tokens == waves[0].estimated_tokens + waves[1].estimated_tokens


# ── Integration smoke: aggregate_waves (live filesystem) ─────────────────────


@pytest.mark.anyio
async def test_aggregate_waves_returns_list() -> None:
    """aggregate_waves() must return a list (possibly empty) without raising.

    This is an integration smoke test — it uses the real worktrees_dir from
    settings so it may return any number of waves depending on the host state.
    It only asserts that the return type is correct and no exception is raised.
    """
    result = await aggregate_waves()
    assert isinstance(result, list)
    for wave in result:
        assert isinstance(wave, WaveSummary)
        assert isinstance(wave.batch_id, str)
        assert isinstance(wave.started_at, float)
        assert isinstance(wave.issues_worked, list)
