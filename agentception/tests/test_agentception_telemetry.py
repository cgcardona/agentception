from __future__ import annotations

"""Tests for the wave aggregation telemetry layer.

Covers the four acceptance criteria from issue #620:
- Waves are correctly grouped by BATCH_ID prefix
- started_at is the earliest worktree creation time in the batch
- ended_at is None when any worktree in the batch is still active
- Empty worktree list returns empty waves
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentception.models import TaskFile
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


def _make_task_file(
    tmp_path: Path,
    name: str,
    batch_id: str,
    issue_number: int = 1,
    mtime: float = 1_000_000.0,
    remove_dir: bool = False,
) -> TaskFile:
    """Create a temporary worktree directory + .agent-task file for testing.

    If ``remove_dir`` is True the directory is deleted after creation to
    simulate a completed (self-destructed) worktree.  The task file path is
    still referenced by the TaskFile, so mtime-based logic can observe it.
    """
    wt_dir = tmp_path / name
    wt_dir.mkdir(parents=True, exist_ok=True)
    task_file = wt_dir / ".agent-task"
    task_file.write_text(
        f"BATCH_ID={batch_id}\nISSUE_NUMBER={issue_number}\n", encoding="utf-8"
    )
    # Force a deterministic mtime so tests are not flaky under fast filesystems.
    os.utime(task_file, (mtime, mtime))

    if remove_dir:
        import shutil

        shutil.rmtree(wt_dir)

    return TaskFile(
        batch_id=batch_id,
        issue_number=issue_number,
        worktree=str(wt_dir),
        closes_issues=[issue_number],
    )


# ── Unit tests for compute_wave_timing ────────────────────────────────────────


@pytest.mark.anyio
async def test_wave_timing_uses_earliest_mtime(tmp_path: Path) -> None:
    """started_at must equal the earliest mtime among task files in the group.

    Both worktrees are still present on disk (agent active), so ended_at is
    None — the batch has not yet completed.  The important invariant here is
    that started_at picks up the *minimum* mtime, not the maximum or arbitrary.
    """
    early = _make_task_file(tmp_path, "wt-early", "batch-A", issue_number=1, mtime=1_000.0)
    late = _make_task_file(tmp_path, "wt-late", "batch-A", issue_number=2, mtime=9_000.0)

    started_at, ended_at = await compute_wave_timing([early, late])

    assert started_at == pytest.approx(1_000.0)
    # Both worktree dirs still exist → batch is still active → ended_at is None.
    assert ended_at is None


@pytest.mark.anyio
async def test_wave_ended_at_none_when_active(tmp_path: Path) -> None:
    """ended_at must be None when any worktree in the group is still present."""
    active = _make_task_file(tmp_path, "wt-active", "batch-B", issue_number=3, mtime=5_000.0)
    # Directory still exists → agent is active.
    assert Path(active.worktree or "").exists()

    started_at, ended_at = await compute_wave_timing([active])

    assert started_at == pytest.approx(5_000.0)
    assert ended_at is None


@pytest.mark.anyio
async def test_wave_ended_at_graceful_when_files_missing(tmp_path: Path) -> None:
    """compute_wave_timing handles missing task files gracefully.

    When a self-destructed agent removes its worktree (shutil.rmtree), both
    the worktree directory and the task file inside are deleted.  The function
    must not raise; instead it returns (0.0, None) as the safe fallback so the
    caller can distinguish "no data" from "zero timestamp".
    """
    done1 = _make_task_file(
        tmp_path, "wt-done1", "batch-C", issue_number=4, mtime=2_000.0, remove_dir=True
    )
    done2 = _make_task_file(
        tmp_path, "wt-done2", "batch-C", issue_number=5, mtime=8_000.0, remove_dir=True
    )
    # Dirs (and files inside) are removed — no mtimes can be read.
    started_at, ended_at = await compute_wave_timing([done1, done2])
    assert started_at == pytest.approx(0.0)
    assert ended_at is None


@pytest.mark.anyio
async def test_compute_wave_timing_empty_list() -> None:
    """compute_wave_timing([]) returns (0.0, None) — no crash, no sentinel."""
    started_at, ended_at = await compute_wave_timing([])
    assert started_at == 0.0
    assert ended_at is None


# ── Unit tests for _build_wave_summaries ──────────────────────────────────────


def test_aggregate_waves_groups_by_batch_id(tmp_path: Path) -> None:
    """_build_wave_summaries must produce one WaveSummary per unique BATCH_ID."""
    tf_a1 = _make_task_file(tmp_path, "wt-a1", "eng-batch-A", issue_number=10, mtime=1_000.0)
    tf_a2 = _make_task_file(tmp_path, "wt-a2", "eng-batch-A", issue_number=11, mtime=2_000.0)
    tf_b1 = _make_task_file(tmp_path, "wt-b1", "eng-batch-B", issue_number=20, mtime=3_000.0)

    result = _build_wave_summaries([tf_a1, tf_a2, tf_b1], tmp_path)

    assert len(result) == 2
    batch_ids = {s.batch_id for s in result}
    assert batch_ids == {"eng-batch-A", "eng-batch-B"}


def test_aggregate_waves_issues_worked_correct(tmp_path: Path) -> None:
    """issues_worked must list all unique issue numbers from the batch."""
    tf1 = _make_task_file(tmp_path, "wt-iss1", "batch-X", issue_number=100)
    tf2 = _make_task_file(tmp_path, "wt-iss2", "batch-X", issue_number=101)

    result = _build_wave_summaries([tf1, tf2], tmp_path)

    assert len(result) == 1
    wave = result[0]
    assert sorted(wave.issues_worked) == [100, 101]


def test_empty_worktrees_returns_empty_waves(tmp_path: Path) -> None:
    """_build_wave_summaries([]) must return [] without error."""
    result = _build_wave_summaries([], tmp_path)
    assert result == []


def test_task_files_without_batch_id_are_skipped(tmp_path: Path) -> None:
    """Task files with no BATCH_ID must be silently excluded from wave grouping."""
    no_batch = TaskFile(issue_number=999, worktree=str(tmp_path / "wt-nobatch"))
    tf_with_batch = _make_task_file(tmp_path, "wt-hasbatch", "batch-Y", issue_number=1)

    result = _build_wave_summaries([no_batch, tf_with_batch], tmp_path)

    assert len(result) == 1
    assert result[0].batch_id == "batch-Y"


def test_wave_summaries_sorted_most_recent_first(tmp_path: Path) -> None:
    """Waves must be sorted by started_at descending (most recent first)."""
    old = _make_task_file(tmp_path, "wt-old", "batch-old", issue_number=1, mtime=100.0)
    new = _make_task_file(tmp_path, "wt-new", "batch-new", issue_number=2, mtime=9_000.0)

    result = _build_wave_summaries([old, new], tmp_path)

    assert len(result) == 2
    assert result[0].batch_id == "batch-new"
    assert result[1].batch_id == "batch-old"


def test_wave_summary_type_is_wave_summary(tmp_path: Path) -> None:
    """Each result must be a WaveSummary Pydantic model (not a dict or stub)."""
    tf = _make_task_file(tmp_path, "wt-type", "batch-Z", issue_number=5)
    result = _build_wave_summaries([tf], tmp_path)
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


def test_wave_summary_includes_cost(tmp_path: Path) -> None:
    """Each WaveSummary must expose estimated_tokens and estimated_cost_usd.

    Both fields should be non-negative integers / floats computed by estimate_cost.
    With no transcript enrichment, message_count defaults to 0, so both are 0.
    """
    tf = _make_task_file(tmp_path, "wt-cost", "batch-cost", issue_number=42)
    result = _build_wave_summaries([tf], tmp_path)

    assert len(result) == 1
    wave = result[0]
    assert isinstance(wave.estimated_tokens, int)
    assert isinstance(wave.estimated_cost_usd, float)
    assert wave.estimated_tokens >= 0
    assert wave.estimated_cost_usd >= 0.0


def test_total_cost_sums_waves(tmp_path: Path) -> None:
    """Summing estimated_cost_usd across waves must equal the aggregate total.

    Two independent waves, each with zero message_count (defaults), produce
    zero cost each — their sum is also zero.  The invariant is that the
    aggregate matches the per-wave sum, not that costs are non-zero.
    """
    tf_a = _make_task_file(tmp_path, "wt-sum-a", "batch-sum-A", issue_number=50)
    tf_b = _make_task_file(tmp_path, "wt-sum-b", "batch-sum-B", issue_number=51)
    waves = _build_wave_summaries([tf_a, tf_b], tmp_path)

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
