from __future__ import annotations

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

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.queries import RunContextRow
from agentception.models import AgentStatus, PipelineState, StalledAgentEvent
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
from agentception.types import JsonValue


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


def _make_worktree(issue_number: int | None = None, branch: str | None = None) -> RunContextRow:
    return RunContextRow(
        run_id=f"issue-{issue_number}" if issue_number else "unknown",
        status="implementing",
        role="developer",
        cognitive_arch=None,
        task_description=None,
        issue_number=issue_number,
        pr_number=None,
        branch=branch,
        worktree_path=f"/tmp/fake-worktree-{issue_number}",
        batch_id=None,
        tier="worker",
        org_domain=None,
        parent_run_id=None,
        gh_repo=None,
        is_resumed=False,
        coord_fingerprint=None,
        spawned_at="2024-01-01T00:00:00",
        last_activity_at=None,
        completed_at=None,
        pr_base_branch=None,
    )


# ---------------------------------------------------------------------------
# tick() — full pipeline round-trip
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tick_returns_pipeline_state() -> None:
    """tick() should return a PipelineState with a fresh polled_at timestamp."""
    board = _empty_board(active_label="agentception/1-readers")

    with (
        patch("agentception.poller.list_active_runs", new_callable=AsyncMock, return_value=[]),
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
        patch("agentception.poller.list_active_runs", new_callable=AsyncMock, return_value=[]),
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
    """An agent/wip issue with no live worktree should produce a stale-claim alert."""
    board = GitHubBoard(
        active_label="agentception/0-scaffold",
        open_issues=[{"number": 42, "title": "Test issue", "labels": [], "body": ""}],
        open_prs=[],
        wip_issues=[{"number": 42, "title": "Test issue", "labels": [{"name": "agent/wip"}]}],
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
        alerts, stale_claims, _stalled = await detect_alerts([], board)

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
        wip_issues=[{"number": 99, "title": "In progress", "labels": [{"name": "agent/wip"}]}],
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
        alerts, _stale_claims, _stalled = await detect_alerts(worktrees, board)

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
        alerts, _stale_claims, _stalled = await detect_alerts([], board)

    assert any("Out-of-order PR #77" in a for a in alerts), f"Expected out-of-order alert, got: {alerts}"


@pytest.mark.anyio
async def test_stuck_agent_alert_detected(tmp_path: Path) -> None:
    """A worktree whose last commit is > 30 min old should trigger a stuck-agent alert."""
    old_timestamp = time.time() - (31 * 60)  # 31 minutes ago

    worktrees = [
        RunContextRow(
            run_id="issue-55",
            status="implementing",
            role="developer",
            cognitive_arch=None,
            task_description=None,
            issue_number=55,
            pr_number=None,
            branch="feat/issue-55",
            worktree_path=str(tmp_path),
            batch_id=None,
            tier="worker",
            org_domain=None,
            parent_run_id=None,
            gh_repo=None,
            is_resumed=False,
            coord_fingerprint=None,
            spawned_at="2024-01-01T00:00:00",
            last_activity_at=None,
            completed_at=None,
            pr_base_branch=None,
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
        alerts, _stale_claims, _stalled = await detect_alerts(worktrees, board)

    assert any("stuck agent" in a.lower() for a in alerts), f"Expected stuck-agent alert, got: {alerts}"


@pytest.mark.anyio
async def test_stall_detection_primary_signal_cold_heartbeat(tmp_path: Path) -> None:
    """Poller marks STALLED and emits StalledAgentEvent when last_activity_at is beyond threshold."""
    stale_ts = time.time() - (35 * 60)  # 35 minutes ago
    stale_iso = "2024-01-01T00:35:00"

    worktrees = [
        RunContextRow(
            run_id="issue-99",
            status="implementing",
            role="developer",
            cognitive_arch=None,
            task_description=None,
            issue_number=99,
            pr_number=None,
            branch="feat/issue-99",
            worktree_path=str(tmp_path),
            batch_id=None,
            tier="worker",
            org_domain=None,
            parent_run_id=None,
            gh_repo=None,
            is_resumed=False,
            coord_fingerprint=None,
            spawned_at="2024-01-01T00:00:00",
            last_activity_at=stale_iso,
            completed_at=None,
            pr_base_branch=None,
        )
    ]
    board = _empty_board()
    threshold = 30 * 60  # 30 minutes

    with (
        patch("agentception.poller.settings") as mock_settings,
        patch(
            "agentception.poller.worktree_last_commit_time",
            new_callable=AsyncMock,
            return_value=stale_ts,
        ),
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("agentception.db.persist.update_agent_status", new_callable=AsyncMock),
    ):
        mock_settings.worktrees_dir = tmp_path / "worktrees"
        # Freeze now so elapsed calculation is deterministic.
        with patch("agentception.poller.time") as mock_time:
            mock_time.time.return_value = stale_ts + 35 * 60
            alerts, _claims, stalled = await detect_alerts(worktrees, board, threshold)

    assert any("stuck agent" in a.lower() for a in alerts), alerts
    assert len(stalled) == 1
    assert isinstance(stalled[0], StalledAgentEvent)
    assert stalled[0].run_id == "issue-99"
    assert stalled[0].issue_number == 99
    assert stalled[0].stalled_for_minutes >= 30


@pytest.mark.anyio
async def test_stall_detection_no_stall_when_heartbeat_warm(tmp_path: Path) -> None:
    """Poller does NOT mark STALLED when last_activity_at is within the threshold window."""
    recent_iso = "2024-01-01T00:55:00"  # only 5 min ago relative to mocked now
    now_ts = 1704067200.0 + 60 * 60  # arbitrary base + 1 hour

    worktrees = [
        RunContextRow(
            run_id="issue-88",
            status="implementing",
            role="developer",
            cognitive_arch=None,
            task_description=None,
            issue_number=88,
            pr_number=None,
            branch="feat/issue-88",
            worktree_path=str(tmp_path),
            batch_id=None,
            tier="worker",
            org_domain=None,
            parent_run_id=None,
            gh_repo=None,
            is_resumed=False,
            coord_fingerprint=None,
            spawned_at="2024-01-01T00:00:00",
            last_activity_at=recent_iso,
            completed_at=None,
            pr_base_branch=None,
        )
    ]
    board = _empty_board()
    threshold = 30 * 60

    with (
        patch("agentception.poller.settings") as mock_settings,
        patch(
            "agentception.poller.worktree_last_commit_time",
            new_callable=AsyncMock,
            # Old commit — but heartbeat is warm, so should only be advisory.
            return_value=now_ts - 45 * 60,
        ),
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_settings.worktrees_dir = tmp_path / "worktrees"
        import datetime as _dt

        with patch("agentception.poller.time") as mock_time:
            # now_ts is 5 min after recent_iso → heartbeat warm
            recent_ts = _dt.datetime.fromisoformat(recent_iso).timestamp()
            mock_time.time.return_value = recent_ts + 5 * 60
            alerts, _claims, stalled = await detect_alerts(worktrees, board, threshold)

    assert len(stalled) == 0, f"Expected no stalled agents, got: {stalled}"
    assert not any("stuck agent" in a.lower() for a in alerts), alerts


@pytest.mark.anyio
async def test_stalled_agents_in_pipeline_state(tmp_path: Path) -> None:
    """PipelineState.stalled_agents is populated from detect_alerts and broadcast via SSE."""
    stale_iso = "2024-01-01T00:00:00"
    stalled_event = StalledAgentEvent(
        run_id="issue-77",
        issue_number=77,
        worktree_path=str(tmp_path),
        last_activity_at=stale_iso,
        stalled_for_minutes=45,
    )

    # Patch only the three surfaces that tick() calls at module-level;
    # all DB/SSE-expansion imports are lazy and fail silently via try/except.
    with (
        patch("agentception.poller.list_active_runs", new_callable=AsyncMock, return_value=[]),
        patch(
            "agentception.poller.build_github_board",
            new_callable=AsyncMock,
            return_value=_empty_board(),
        ),
        patch(
            "agentception.poller.detect_alerts",
            new_callable=AsyncMock,
            return_value=([], [], [stalled_event]),
        ),
    ):
        state = await tick()

    assert len(state.stalled_agents) == 1
    assert state.stalled_agents[0].run_id == "issue-77"
    assert state.stalled_agents[0].stalled_for_minutes == 45


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
    """A worktree whose issue is agent/wip but has no PR should be IMPLEMENTING."""
    board = GitHubBoard(
        active_label=None,
        open_issues=[],
        open_prs=[],
        wip_issues=[{"number": 20, "title": "...", "labels": [{"name": "agent/wip"}]}],
    )
    worktrees = [_make_worktree(issue_number=20, branch="feat/issue-20")]
    agents = await merge_agents(worktrees, board)

    assert len(agents) == 1
    assert agents[0].status == AgentStatus.IMPLEMENTING


@pytest.mark.anyio
async def test_merge_agents_implementing_when_issue_number_present() -> None:
    """A worktree with an issue_number is IMPLEMENTING regardless of agent/wip label.

    The worktree's existence is the authoritative signal — we no longer require
    the agent/wip GitHub label because leaf agents may not have claimed the
    issue by the time the first poller tick fires.
    """
    worktrees = [_make_worktree(issue_number=30, branch="feat/issue-30")]
    agents = await merge_agents(worktrees, _empty_board())

    assert len(agents) == 1
    assert agents[0].status == AgentStatus.IMPLEMENTING


@pytest.mark.anyio
async def test_merge_agents_unknown_status() -> None:
    """Ad-hoc runs (no issue_number, no PR) display as IMPLEMENTING for the dashboard.

    Runs with no issue/PR number are ad-hoc coordinator sub-runs or runs
    that were not tied to a GitHub issue.  The poller shows them as IMPLEMENTING
    rather than FAILED to avoid false-positive failure signals — their real status
    is managed entirely by the agent loop's lifecycle, never by the poller.
    """
    wt = _make_worktree(issue_number=None, branch="feat/unknown-thing")
    agents = await merge_agents([wt], _empty_board())

    assert len(agents) == 1
    assert agents[0].status == AgentStatus.IMPLEMENTING


@pytest.mark.anyio
async def test_merge_agents_passes_pr_number_from_task_file() -> None:
    """pr_number from DB row is passed through to AgentNode so poller upsert sets run.pr_number."""
    worktree = RunContextRow(
        run_id="issue-20",
        status="implementing",
        role="developer",
        cognitive_arch=None,
        task_description=None,
        issue_number=20,
        pr_number=99,
        branch="feat/issue-20",
        worktree_path="/tmp/fake-worktree-20",
        batch_id=None,
        tier="worker",
        org_domain=None,
        parent_run_id=None,
        gh_repo=None,
        is_resumed=False,
        coord_fingerprint=None,
        spawned_at="2024-01-01T00:00:00",
        last_activity_at=None,
        completed_at=None,
        pr_base_branch=None,
    )
    board = GitHubBoard(
        active_label=None,
        open_issues=[],
        open_prs=[],
        wip_issues=[{"number": 20, "title": "...", "labels": [{"name": "agent/wip"}]}],
    )
    agents = await merge_agents([worktree], board)

    assert len(agents) == 1
    assert agents[0].pr_number == 99


# ---------------------------------------------------------------------------
# polling_loop() — interval behaviour
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# scan_plan_draft_worktrees() — plan draft event detection
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


# ---------------------------------------------------------------------------
# _auto_advance_phases
# ---------------------------------------------------------------------------


def _phase_row(
    label: str,
    complete: bool,
    depends_on: list[str] | None = None,
) -> dict[str, JsonValue]:
    """Minimal PhaseGroupRow-compatible dict for testing."""
    deps: list[JsonValue] = []
    deps.extend(depends_on or [])
    return {
        "label": label,
        "complete": complete,
        "locked": bool(depends_on and not complete),
        "depends_on": deps,
        "issues": [],
    }


@pytest.mark.anyio
async def test_auto_advance_phases_calls_plan_advance_when_dep_complete() -> None:
    """plan_advance_phase is called when a phase's dependency is fully closed."""
    phases = [
        _phase_row("ac-wf/0-foundation", complete=True),
        _phase_row("ac-wf/1-migration", complete=False, depends_on=["ac-wf/0-foundation"]),
    ]

    with (
        patch(
            "agentception.db.queries.get_initiatives",
            new=AsyncMock(return_value=["ac-wf"]),
        ),
        patch(
            "agentception.db.queries.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=phases),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.plan_advance_phase",
            new=AsyncMock(return_value={"advanced": True, "unlocked_count": 3}),
        ) as mock_advance,
    ):
        import agentception.poller as pm
        pm._auto_advanced.clear()
        from agentception.poller import _auto_advance_phases
        await _auto_advance_phases("cgcardona/agentception")

    mock_advance.assert_awaited_once_with("ac-wf", "ac-wf/0-foundation", "ac-wf/1-migration")


@pytest.mark.anyio
async def test_auto_advance_phases_skips_already_advanced() -> None:
    """plan_advance_phase is NOT called for a transition already in _auto_advanced."""
    phases = [
        _phase_row("ac-wf/0-foundation", complete=True),
        _phase_row("ac-wf/1-migration", complete=False, depends_on=["ac-wf/0-foundation"]),
    ]

    with (
        patch(
            "agentception.db.queries.get_initiatives",
            new=AsyncMock(return_value=["ac-wf"]),
        ),
        patch(
            "agentception.db.queries.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=phases),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.plan_advance_phase",
            new=AsyncMock(return_value={"advanced": True, "unlocked_count": 3}),
        ) as mock_advance,
    ):
        import agentception.poller as pm
        pm._auto_advanced.clear()
        pm._auto_advanced.add(("ac-wf", "ac-wf/0-foundation", "ac-wf/1-migration"))
        from agentception.poller import _auto_advance_phases
        await _auto_advance_phases("cgcardona/agentception")

    mock_advance.assert_not_awaited()


@pytest.mark.anyio
async def test_auto_advance_phases_skips_incomplete_dep() -> None:
    """plan_advance_phase is NOT called when a dependency is not yet complete."""
    phases = [
        _phase_row("ac-wf/0-foundation", complete=False),
        _phase_row("ac-wf/1-migration", complete=False, depends_on=["ac-wf/0-foundation"]),
    ]

    with (
        patch(
            "agentception.db.queries.get_initiatives",
            new=AsyncMock(return_value=["ac-wf"]),
        ),
        patch(
            "agentception.db.queries.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=phases),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.plan_advance_phase",
            new=AsyncMock(return_value={"advanced": True, "unlocked_count": 0}),
        ) as mock_advance,
    ):
        import agentception.poller as pm
        pm._auto_advanced.clear()
        from agentception.poller import _auto_advance_phases
        await _auto_advance_phases("cgcardona/agentception")

    mock_advance.assert_not_awaited()


@pytest.mark.anyio
async def test_auto_advance_phases_does_not_add_to_dedup_on_failure() -> None:
    """A failed plan_advance_phase call is retried on the next tick."""
    phases = [
        _phase_row("ac-wf/0-foundation", complete=True),
        _phase_row("ac-wf/1-migration", complete=False, depends_on=["ac-wf/0-foundation"]),
    ]

    with (
        patch(
            "agentception.db.queries.get_initiatives",
            new=AsyncMock(return_value=["ac-wf"]),
        ),
        patch(
            "agentception.db.queries.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=phases),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.plan_advance_phase",
            new=AsyncMock(side_effect=RuntimeError("gh timeout")),
        ) as mock_advance,
    ):
        import agentception.poller as pm
        pm._auto_advanced.clear()
        from agentception.poller import _auto_advance_phases
        await _auto_advance_phases("cgcardona/agentception")

    # Called once but not added to dedup — will be retried next tick.
    mock_advance.assert_awaited_once()
    assert ("ac-wf", "ac-wf/0-foundation", "ac-wf/1-migration") not in pm._auto_advanced


@pytest.mark.anyio
async def test_auto_advance_phases_does_not_advance_complete_phase() -> None:
    """A phase that is itself complete is never a candidate for advancing."""
    phases = [
        _phase_row("ac-wf/0-foundation", complete=True),
        _phase_row("ac-wf/1-migration", complete=True, depends_on=["ac-wf/0-foundation"]),
    ]

    with (
        patch(
            "agentception.db.queries.get_initiatives",
            new=AsyncMock(return_value=["ac-wf"]),
        ),
        patch(
            "agentception.db.queries.get_issues_grouped_by_phase",
            new=AsyncMock(return_value=phases),
        ),
        patch(
            "agentception.mcp.plan_advance_phase.plan_advance_phase",
            new=AsyncMock(return_value={"advanced": True, "unlocked_count": 0}),
        ) as mock_advance,
    ):
        import agentception.poller as pm
        pm._auto_advanced.clear()
        from agentception.poller import _auto_advance_phases
        await _auto_advance_phases("cgcardona/agentception")

    mock_advance.assert_not_awaited()


# _auto_unblock_ticket_deps
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_auto_unblock_deps_removes_label_when_all_deps_closed() -> None:
    """blocked/deps is removed when every dep issue is closed in the DB."""
    from agentception.poller import _auto_unblock_deps

    candidates = [{"github_number": 177, "dep_numbers": [175]}]
    closed = {175}
    remove_mock = AsyncMock()

    with (
        patch(
            "agentception.db.queries.get_blocked_deps_open_issues",
            new=AsyncMock(return_value=candidates),
        ),
        patch(
            "agentception.db.queries.get_closed_issue_numbers",
            new=AsyncMock(return_value=closed),
        ),
        patch(
            "agentception.readers.github.remove_label_from_issue",
            remove_mock,
        ),
    ):
        await _auto_unblock_deps("cgcardona/agentception")

    remove_mock.assert_awaited_once_with(177, "blocked/deps")


@pytest.mark.anyio
async def test_auto_unblock_deps_skips_when_dep_still_open() -> None:
    """blocked/deps is NOT removed when any dep issue is still open."""
    from agentception.poller import _auto_unblock_deps

    candidates = [{"github_number": 177, "dep_numbers": [175]}]
    closed: set[int] = set()  # 175 still open
    remove_mock = AsyncMock()

    with (
        patch(
            "agentception.db.queries.get_blocked_deps_open_issues",
            new=AsyncMock(return_value=candidates),
        ),
        patch(
            "agentception.db.queries.get_closed_issue_numbers",
            new=AsyncMock(return_value=closed),
        ),
        patch(
            "agentception.readers.github.remove_label_from_issue",
            remove_mock,
        ),
    ):
        await _auto_unblock_deps("cgcardona/agentception")

    remove_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_auto_unblock_deps_skips_when_no_candidates() -> None:
    """No GitHub calls made when queue is empty."""
    from agentception.poller import _auto_unblock_deps

    remove_mock = AsyncMock()
    closed_mock = AsyncMock(return_value=set())

    with (
        patch(
            "agentception.db.queries.get_blocked_deps_open_issues",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.db.queries.get_closed_issue_numbers",
            closed_mock,
        ),
        patch(
            "agentception.readers.github.remove_label_from_issue",
            remove_mock,
        ),
    ):
        await _auto_unblock_deps("cgcardona/agentception")

    closed_mock.assert_not_awaited()
    remove_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# _stamp_missing_blocked_deps — regression for silent label-stamp failure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stamp_missing_blocked_deps_restamps_when_dep_open() -> None:
    """blocked/deps is applied when depends_on_json has an open dep but label is absent.

    Regression: issue_creator used a shared try/except that silently swallowed
    add_label_to_issue failures, leaving issues dispatchable despite open blockers.
    """
    from agentception.poller import _stamp_missing_blocked_deps

    candidates = [{"github_number": 177, "dep_numbers": [175]}]
    closed: set[int] = set()  # 175 still open
    add_mock = AsyncMock()

    with (
        patch(
            "agentception.db.queries.get_issues_missing_blocked_deps",
            new=AsyncMock(return_value=candidates),
        ),
        patch(
            "agentception.db.queries.get_closed_issue_numbers",
            new=AsyncMock(return_value=closed),
        ),
        patch(
            "agentception.readers.github.add_label_to_issue",
            add_mock,
        ),
    ):
        await _stamp_missing_blocked_deps("cgcardona/agentception")

    add_mock.assert_awaited_once_with(177, "blocked/deps")


@pytest.mark.anyio
async def test_stamp_missing_blocked_deps_skips_when_all_deps_closed() -> None:
    """No label is added when every dep is already closed (issue is about to be unblocked)."""
    from agentception.poller import _stamp_missing_blocked_deps

    candidates = [{"github_number": 177, "dep_numbers": [175]}]
    closed = {175}  # dep already closed
    add_mock = AsyncMock()

    with (
        patch(
            "agentception.db.queries.get_issues_missing_blocked_deps",
            new=AsyncMock(return_value=candidates),
        ),
        patch(
            "agentception.db.queries.get_closed_issue_numbers",
            new=AsyncMock(return_value=closed),
        ),
        patch(
            "agentception.readers.github.add_label_to_issue",
            add_mock,
        ),
    ):
        await _stamp_missing_blocked_deps("cgcardona/agentception")

    add_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_stamp_missing_blocked_deps_skips_when_no_candidates() -> None:
    """No GitHub calls when every issue already has blocked/deps or has no deps."""
    from agentception.poller import _stamp_missing_blocked_deps

    add_mock = AsyncMock()
    closed_mock = AsyncMock(return_value=set())

    with (
        patch(
            "agentception.db.queries.get_issues_missing_blocked_deps",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "agentception.db.queries.get_closed_issue_numbers",
            closed_mock,
        ),
        patch(
            "agentception.readers.github.add_label_to_issue",
            add_mock,
        ),
    ):
        await _stamp_missing_blocked_deps("cgcardona/agentception")

    closed_mock.assert_not_awaited()
    add_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# polling_loop() — network / DNS error backoff
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_polling_loop_oserror_triggers_backoff() -> None:
    """OSError (socket.gaierror, connection refused, DNS failure) triggers
    exponential backoff instead of sleeping at the normal poll interval.

    Regression: [Errno -2] Name or service not known was being logged as a
    plain 'Polling loop error' with no backoff, causing the poller to hammer
    an unreachable GitHub API on every poll cycle.
    """
    import socket

    sleep_calls: list[float] = []
    call_count = 0

    async def fake_tick() -> None:
        nonlocal call_count
        call_count += 1
        # First call raises a DNS error; second call cancels the loop.
        if call_count == 1:
            raise socket.gaierror(-2, "Name or service not known")
        raise asyncio.CancelledError

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with (
        patch("agentception.poller.tick", side_effect=fake_tick),
        patch("agentception.poller.asyncio.sleep", side_effect=fake_sleep),
        patch("agentception.poller.settings") as mock_settings,
    ):
        mock_settings.poll_interval_seconds = 5
        mock_settings.stale_run_threshold_minutes = 60
        try:
            await polling_loop()
        except asyncio.CancelledError:
            pass

    # The backoff sleep must be the initial net-error backoff (30 s), not the
    # normal poll interval (5 s).
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] == 30, (
        f"Expected 30 s net backoff on OSError, got {sleep_calls[0]} s"
    )


@pytest.mark.anyio
async def test_polling_loop_oserror_backoff_doubles_on_repeat() -> None:
    """Second consecutive OSError doubles the backoff sleep."""
    import socket

    sleep_calls: list[float] = []
    call_count = 0

    async def fake_tick() -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise socket.gaierror(-2, "Name or service not known")
        raise asyncio.CancelledError

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with (
        patch("agentception.poller.tick", side_effect=fake_tick),
        patch("agentception.poller.asyncio.sleep", side_effect=fake_sleep),
        patch("agentception.poller.settings") as mock_settings,
    ):
        mock_settings.poll_interval_seconds = 5
        mock_settings.stale_run_threshold_minutes = 60
        try:
            await polling_loop()
        except asyncio.CancelledError:
            pass

    assert len(sleep_calls) >= 2
    assert sleep_calls[0] == 30, f"First backoff should be 30 s, got {sleep_calls[0]}"
    assert sleep_calls[1] == 60, f"Second backoff should be 60 s, got {sleep_calls[1]}"
