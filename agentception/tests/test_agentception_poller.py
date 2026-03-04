"""Tests for agentception/poller.py (AC-005).

Coverage:
- tick() returns a valid PipelineState
- broadcast() reaches all subscribers
- subscribe() / unsubscribe() lifecycle
- detect_alerts() surfaces stale-claim alerts correctly
- polling_loop() advances state on each iteration (mock sleep)

Run targeted:
    pytest agentception/tests/test_agentception_poller.py -v
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.models import AgentStatus, PipelineState, TaskFile
from agentception.poller import (
    GitHubBoard,
    broadcast,
    detect_alerts,
    get_state,
    merge_agents,
    polling_loop,
    subscribe,
    tick,
    unsubscribe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_board(active_label: str | None = None) -> GitHubBoard:
    """Return a GitHubBoard with no issues, PRs, or WIP issues."""
    return GitHubBoard(
        active_label=active_label,
        open_issues=[],
        open_prs=[],
        wip_issues=[],
    )


def _make_worktree(issue_number: int | None = None, branch: str | None = None) -> TaskFile:
    return TaskFile(
        task="issue-to-pr",
        issue_number=issue_number,
        branch=branch,
        role="python-developer",
        worktree=f"/tmp/fake-worktree-{issue_number}",
    )


# ---------------------------------------------------------------------------
# tick() — full pipeline round-trip
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tick_returns_pipeline_state() -> None:
    """tick() should return a PipelineState with a fresh polled_at timestamp."""
    board = _empty_board(active_label="agentception/1-readers")

    with (
        patch("agentception.poller.list_active_worktrees", new_callable=AsyncMock, return_value=[]),
        patch("agentception.poller.build_github_board", new_callable=AsyncMock, return_value=board),
        patch("agentception.poller.detect_out_of_order_prs", new_callable=AsyncMock, return_value=[]),
    ):
        before = time.time()
        state = await tick()
        after = time.time()

    assert isinstance(state, PipelineState)
    assert state.active_label == "agentception/1-readers"
    assert state.issues_open == 0
    assert state.prs_open == 0
    assert state.agents == []
    assert state.alerts == []
    assert before <= state.polled_at <= after


@pytest.mark.anyio
async def test_tick_updates_global_state() -> None:
    """tick() should update the module-level _state so get_state() reflects it."""
    board = _empty_board()

    with (
        patch("agentception.poller.list_active_worktrees", new_callable=AsyncMock, return_value=[]),
        patch("agentception.poller.build_github_board", new_callable=AsyncMock, return_value=board),
        patch("agentception.poller.detect_out_of_order_prs", new_callable=AsyncMock, return_value=[]),
    ):
        state = await tick()

    assert get_state() is state


# ---------------------------------------------------------------------------
# broadcast() + subscribe() / unsubscribe()
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_broadcast_reaches_subscriber() -> None:
    """broadcast() should put the state into every registered subscriber queue."""
    q = subscribe()
    try:
        state = PipelineState(
            active_label=None,
            issues_open=3,
            prs_open=1,
            agents=[],
            alerts=[],
            polled_at=time.time(),
        )
        await broadcast(state)
        received = await asyncio.wait_for(q.get(), timeout=1.0)
        assert received is state
    finally:
        unsubscribe(q)


@pytest.mark.anyio
async def test_broadcast_reaches_multiple_subscribers() -> None:
    """broadcast() should deliver to all connected clients concurrently."""
    q1 = subscribe()
    q2 = subscribe()
    try:
        state = PipelineState(
            active_label="agentception/0-scaffold",
            issues_open=0,
            prs_open=0,
            agents=[],
            polled_at=time.time(),
        )
        await broadcast(state)
        r1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        r2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert r1 is state
        assert r2 is state
    finally:
        unsubscribe(q1)
        unsubscribe(q2)


@pytest.mark.anyio
async def test_subscribe_unsubscribe() -> None:
    """unsubscribe() should remove the queue so it no longer receives events."""
    q = subscribe()
    unsubscribe(q)

    state = PipelineState(
        active_label=None,
        issues_open=0,
        prs_open=0,
        agents=[],
        polled_at=time.time(),
    )
    await broadcast(state)
    # Queue should be empty because it was unsubscribed before broadcast.
    assert q.empty()


@pytest.mark.anyio
async def test_unsubscribe_idempotent() -> None:
    """Calling unsubscribe() twice on the same queue must not raise."""
    q = subscribe()
    unsubscribe(q)
    unsubscribe(q)  # second call — should be a no-op


# ---------------------------------------------------------------------------
# detect_alerts() — stale claim detection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_claim_alert_detected(tmp_path: Path) -> None:
    """An agent:wip issue with no live worktree should produce a stale-claim alert."""
    board = GitHubBoard(
        active_label="agentception/0-scaffold",
        open_issues=[{"number": 42, "title": "Test issue", "labels": [], "body": ""}],
        open_prs=[],
        wip_issues=[{"number": 42, "title": "Test issue", "labels": [{"name": "agent:wip"}]}],
    )
    # No worktrees — issue 42 has no live worktree.
    with (
        patch("agentception.poller.settings") as mock_settings,
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_settings.worktrees_dir = tmp_path
        alerts, stale_claims = await detect_alerts([], board)

    assert any("Stale claim on #42" in a for a in alerts), f"Expected stale-claim alert, got: {alerts}"
    assert len(stale_claims) == 1
    assert stale_claims[0].issue_number == 42


@pytest.mark.anyio
async def test_no_stale_claim_when_worktree_exists(tmp_path: Path) -> None:
    """No stale-claim alert when the wip issue has a matching worktree directory."""
    board = GitHubBoard(
        active_label="agentception/0-scaffold",
        open_issues=[],
        open_prs=[],
        wip_issues=[{"number": 99, "title": "In progress", "labels": [{"name": "agent:wip"}]}],
    )
    # Create the expected worktree directory so the issue is considered live.
    (tmp_path / "issue-99").mkdir()
    worktrees = [_make_worktree(issue_number=99, branch="feat/issue-99")]
    with (
        patch("agentception.poller.settings") as mock_settings,
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_settings.worktrees_dir = tmp_path
        alerts, _stale_claims = await detect_alerts(worktrees, board)

    assert not any("Stale claim on #99" in a for a in alerts)


@pytest.mark.anyio
async def test_out_of_order_pr_alert(tmp_path: Path) -> None:
    """An open PR labelled with a non-active agentception phase should be flagged."""
    board = GitHubBoard(
        active_label="agentception/1-readers",  # current active phase
        open_issues=[],
        open_prs=[
            {
                "number": 77,
                "headRefName": "feat/issue-77",
                "labels": [{"name": "agentception/0-scaffold"}],  # old phase
            }
        ],
        wip_issues=[],
    )
    with (
        patch("agentception.poller.settings") as mock_settings,
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_settings.worktrees_dir = tmp_path
        alerts, _stale_claims = await detect_alerts([], board)

    assert any("Out-of-order PR #77" in a for a in alerts), f"Expected out-of-order alert, got: {alerts}"


@pytest.mark.anyio
async def test_stuck_agent_alert_detected(tmp_path: Path) -> None:
    """A worktree whose last commit is > 30 min old should trigger a stuck-agent alert."""
    old_timestamp = time.time() - (31 * 60)  # 31 minutes ago

    worktrees = [
        TaskFile(
            task="issue-to-pr",
            issue_number=55,
            branch="feat/issue-55",
            role="python-developer",
            worktree=str(tmp_path),
        )
    ]
    board = _empty_board()

    with (
        patch("agentception.poller.settings") as mock_settings,
        patch(
            "agentception.poller.worktree_last_commit_time",
            new_callable=AsyncMock,
            return_value=old_timestamp,
        ),
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_settings.worktrees_dir = tmp_path / "worktrees"
        alerts, _stale_claims = await detect_alerts(worktrees, board)

    assert any("stuck agent" in a.lower() for a in alerts), f"Expected stuck-agent alert, got: {alerts}"


# ---------------------------------------------------------------------------
# merge_agents()
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_merge_agents_reviewing_status() -> None:
    """A worktree whose branch matches an open PR head should be REVIEWING."""
    board = GitHubBoard(
        active_label=None,
        open_issues=[],
        open_prs=[{"number": 10, "headRefName": "feat/issue-10", "labels": []}],
        wip_issues=[],
    )
    worktrees = [_make_worktree(issue_number=10, branch="feat/issue-10")]
    agents = await merge_agents(worktrees, board)

    assert len(agents) == 1
    assert agents[0].status == AgentStatus.REVIEWING
    assert agents[0].issue_number == 10


@pytest.mark.anyio
async def test_merge_agents_implementing_status() -> None:
    """A worktree whose issue is agent:wip but has no PR should be IMPLEMENTING."""
    board = GitHubBoard(
        active_label=None,
        open_issues=[],
        open_prs=[],
        wip_issues=[{"number": 20, "title": "...", "labels": [{"name": "agent:wip"}]}],
    )
    worktrees = [_make_worktree(issue_number=20, branch="feat/issue-20")]
    agents = await merge_agents(worktrees, board)

    assert len(agents) == 1
    assert agents[0].status == AgentStatus.IMPLEMENTING


@pytest.mark.anyio
async def test_merge_agents_unknown_status() -> None:
    """A worktree with no matching PR or WIP issue should be UNKNOWN."""
    worktrees = [_make_worktree(issue_number=30, branch="feat/issue-30")]
    agents = await merge_agents(worktrees, _empty_board())

    assert len(agents) == 1
    assert agents[0].status == AgentStatus.UNKNOWN


# ---------------------------------------------------------------------------
# polling_loop() — interval behaviour
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_polling_loop_runs_at_interval() -> None:
    """polling_loop() should call tick() after each sleep and stop on CancelledError."""
    tick_count = 0

    async def fake_tick() -> PipelineState:
        nonlocal tick_count
        tick_count += 1
        return PipelineState(
            active_label=None,
            issues_open=0,
            prs_open=0,
            agents=[],
            polled_at=time.time(),
        )

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            # Cancel the loop after two ticks to keep the test fast.
            raise asyncio.CancelledError

    with (
        patch("agentception.poller.tick", side_effect=fake_tick),
        patch("agentception.poller.asyncio.sleep", side_effect=fake_sleep),
        patch("agentception.poller.settings") as mock_settings,
    ):
        mock_settings.poll_interval_seconds = 5
        task = asyncio.create_task(polling_loop())
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # At least one tick should have occurred before cancellation.
    assert tick_count >= 1
    # Sleep should have been called with the configured interval.
    assert all(s == 5 for s in sleep_calls)
