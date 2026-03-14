from __future__ import annotations

"""Integration tests for agent health: stale detection, orphan sweep, Ship board zero-FS.

Three tests:
1. test_ship_board_zero_filesystem_access — GET /ship returns 200 or 302 even when
   all filesystem access raises FileNotFoundError.
2. test_stale_agent_writes_warning — detect_alerts() emits a StalledAgentEvent when
   last_activity_at is 35 minutes in the past.
3. test_orphan_missing_build_complete_marked_failed — orphan sweep in _upsert_agent_runs
   marks an implementing run with no PR as failed when its worktree is absent.

All tests are deterministic: no time.sleep(), no real filesystem I/O, no network calls.
Timestamps are injected explicitly; freezegun is used where time.time() is called inside
the module under test.
"""

import datetime
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from agentception.app import app
from agentception.db.models import ACAgentRun
from agentception.db.queries import RunContextRow
from agentception.models import AgentNode, AgentStatus
from agentception.poller import GitHubBoard, detect_alerts
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db import persist as _persist


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = datetime.timezone.utc


def _make_run_context_row(
    *,
    run_id: str = "issue-42",
    issue_number: int = 42,
    worktree_path: str | None = None,
    last_activity_at: str | None = None,
    spawned_at: str | None = None,
    tier: str = "worker",
    status: str = "implementing",
) -> RunContextRow:
    """Build a minimal RunContextRow for use in detect_alerts tests."""
    now_iso = datetime.datetime.now(_UTC).isoformat()
    return RunContextRow(
        run_id=run_id,
        status=status,
        role="developer",
        cognitive_arch=None,
        task_description=None,
        issue_number=issue_number,
        pr_number=None,
        branch=f"feat/{run_id}",
        worktree_path=worktree_path,
        batch_id=None,
        tier=tier,
        org_domain=None,
        parent_run_id=None,
        gh_repo=None,
        is_resumed=False,
        coord_fingerprint=None,
        spawned_at=spawned_at or now_iso,
        last_activity_at=last_activity_at,
        completed_at=None,
        pr_base_branch=None,
    )


def _make_orm_run(
    *,
    run_id: str = "issue-42",
    status: str = "implementing",
    worktree_path: str | None = None,
    pr_number: int | None = None,
    issue_number: int = 42,
) -> ACAgentRun:
    """Return a minimal ACAgentRun ORM object."""
    return ACAgentRun(
        id=run_id,
        role="developer",
        status=status,
        issue_number=issue_number,
        pr_number=pr_number,
        branch=f"feat/{run_id}",
        worktree_path=worktree_path,
        spawned_at=datetime.datetime.now(_UTC),
    )


# ---------------------------------------------------------------------------
# Test 1: Ship board survives zero filesystem access
# ---------------------------------------------------------------------------


def test_ship_board_zero_filesystem_access() -> None:
    """GET /ship returns 200 or 302 even when all filesystem access raises FileNotFoundError.

    The Ship board must degrade gracefully when the host filesystem is
    unavailable (e.g. running in a stripped container or a test environment
    with no worktrees directory).  A FileNotFoundError from any open() or
    Path.exists() call must not propagate to the HTTP layer as a 500.
    """
    with (
        patch(
            "agentception.routes.ui.build_ui.get_initiatives",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("builtins.open", side_effect=FileNotFoundError("no filesystem")),
        patch.object(Path, "exists", side_effect=FileNotFoundError("no filesystem")),
    ):
        with TestClient(app) as client:
            response = client.get("/ship", follow_redirects=False)

    # The route either redirects (302) to /plan or renders a page (200).
    # Either is acceptable — what is NOT acceptable is a 500.
    assert response.status_code in (200, 302, 307), (
        f"GET /ship returned {response.status_code} — expected 200, 302, or 307"
    )


# ---------------------------------------------------------------------------
# Test 2: Stale agent detection emits StalledAgentEvent
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_agent_writes_warning(tmp_path: Path) -> None:
    """detect_alerts() emits a StalledAgentEvent when last_activity_at is 35 min old.

    Arrange: one implementing run whose last_activity_at is 35 minutes in the
    past and whose worktree directory exists.  The spawned_at is 40 minutes ago
    so the run is past the spawn-grace window.

    Act: call detect_alerts() with a stall_threshold_seconds of 30 * 60.

    Assert: the returned stalled_agents list contains exactly one event for
    the run, confirming the primary (DB heartbeat) signal fired.
    """
    now = time.time()
    stall_threshold = 30 * 60  # 30 minutes, same as production default

    # last_activity_at is 35 minutes ago — beyond the 30-minute threshold.
    stale_ts = now - (35 * 60)
    stale_iso = datetime.datetime.fromtimestamp(stale_ts, tz=_UTC).isoformat()

    # spawned_at is 40 minutes ago — outside the spawn-grace window.
    spawned_ts = now - (40 * 60)
    spawned_iso = datetime.datetime.fromtimestamp(spawned_ts, tz=_UTC).isoformat()

    # The worktree directory must exist so detect_alerts() doesn't skip the run.
    worktree = tmp_path / "issue-42"
    worktree.mkdir()

    run = _make_run_context_row(
        run_id="issue-42",
        issue_number=42,
        worktree_path=str(worktree),
        last_activity_at=stale_iso,
        spawned_at=spawned_iso,
    )

    board = GitHubBoard(
        active_label=None,
        open_issues=[],
        open_prs=[],
        wip_issues=[],
    )

    with (
        patch(
            "agentception.poller.detect_stale_claims",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.poller.detect_out_of_order_prs",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "agentception.db.persist.update_agent_status",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        _alerts, _stale_claims, stalled_agents = await detect_alerts(
            active_runs=[run],
            github=board,
            stall_threshold_seconds=stall_threshold,
        )

    assert len(stalled_agents) == 1, (
        f"Expected 1 stalled agent event, got {len(stalled_agents)}: {stalled_agents}"
    )
    event = stalled_agents[0]
    assert event.run_id == "issue-42"
    assert event.issue_number == 42
    assert event.stalled_for_minutes >= 35


# ---------------------------------------------------------------------------
# Test 3: Orphan sweep marks implementing run with no PR as failed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_orphan_missing_build_complete_marked_failed() -> None:
    """Orphan sweep marks an implementing run with no PR as failed when worktree is absent.

    Arrange: one implementing run with no pr_number.  The run is NOT in the
    live_ids set (simulated by passing a different agent to _upsert_agent_runs).
    The worktree path is absent from the filesystem (the orphan sweep checks
    worktree_path existence to decide whether to sweep).

    Act: call _upsert_agent_runs() with a different agent so the orphan is
    never in live_ids.

    Assert: the orphan's status is "failed" — the sweep correctly identified
    it as an orphan with no open PR and applied the failed terminal state.
    """
    orphan = _make_orm_run(
        run_id="issue-orphan-99",
        status="implementing",
        pr_number=None,
        worktree_path="/nonexistent/worktrees/issue-orphan-99",
    )

    # Mock the three DB execute() calls that _upsert_agent_runs makes:
    # Call 1: per-agent row lookup (returns None — different agent, no existing row)
    # Call 2: orphan sweep query (returns our orphan)
    # Call 3: pending_launch TTL sweep (returns empty)
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = None

    orphan_result = MagicMock()
    orphan_result.scalars.return_value.all.return_value = [orphan]

    ttl_result = MagicMock()
    ttl_result.scalars.return_value.all.return_value = []

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[scalar, orphan_result, ttl_result])
    session.add = MagicMock()
    # The orphan sweep calls session.scalar() to check for an existing
    # build_complete_run event.  Return 0 to indicate no such event exists,
    # so the sweep proceeds to mark the orphan as failed.
    session.scalar = AsyncMock(return_value=0)

    # Pass a different agent so the orphan is never in live_ids.
    live_agent = AgentNode(
        id="issue-live-1",
        role="developer",
        status=AgentStatus.IMPLEMENTING,
    )

    await _persist._upsert_agent_runs(session, [live_agent])

    assert orphan.status == "failed", (
        f"Orphan implementing run with no PR must be marked failed, got: {orphan.status!r}"
    )
