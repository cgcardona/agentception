from __future__ import annotations

"""Tests for build board partial enrichment with agent status, current step,
and progress data (issue #82).

Coverage:
- _compute_agent_status() returns normalized status for non-active runs
- _compute_agent_status() marks active run stale when last_activity_at is old
- _compute_agent_status() keeps active status for recent activity
- _compute_agent_status() handles None last_activity_at safely
- get_runs_for_issue_numbers() returns empty dict for empty input
- build board partial (GET /build/board) includes status badge text for a
  mocked run in "implementing" state
- build board partial renders without error for a card with no agent run
- _get_step_data_for_runs() returns empty dict for empty input

Run targeted:
    pytest agentception/tests/test_build_board_partial.py -v
"""

import datetime
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.db.queries import (
    _STALE_THRESHOLD_SECONDS,
    _compute_agent_status,
    _get_step_data_for_runs,
    get_runs_for_issue_numbers,
    OpenPRForIssueRow,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_run_dict(
    *,
    status: str = "implementing",
    agent_status: str = "implementing",
    current_step: str | None = None,
    steps_completed: int = 0,
) -> dict[str, object]:
    """Build a minimal RunForIssueRow-shaped dict for mock patching."""
    return {
        "id": "issue-82",
        "role": "python-developer",
        "status": status,
        "agent_status": agent_status,
        "pr_number": None,
        "branch": "feat/issue-82",
        "spawned_at": "2026-03-04T23:00:00+00:00",
        "last_activity_at": None,
        "current_step": current_step,
        "steps_completed": steps_completed,
        "steps_total": None,
    }


def _mock_issue(number: int = 82, title: str = "Enrich build board") -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "body_excerpt": "",
        "state": "open",
        "url": f"https://github.com/cgcardona/agentception/issues/{number}",
        "labels": ["phase-1"],
        "depends_on": [],
    }


def _mock_group(
    issues: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    return [
        {
            "label": "phase-1",
            "issues": issues or [_mock_issue()],
            "locked": False,
            "complete": False,
            "depends_on": [],
        }
    ]


# ---------------------------------------------------------------------------
# Unit tests — _compute_agent_status
# ---------------------------------------------------------------------------


def test_compute_agent_status_non_active_normalises() -> None:
    """Non-active statuses are simply lower-cased, never marked stale."""
    assert _compute_agent_status("DONE", None) == "done"
    assert _compute_agent_status("STALE", None) == "stale"
    assert _compute_agent_status("UNKNOWN", None) == "unknown"


def test_compute_agent_status_active_no_activity_stays_active() -> None:
    """Active run with None last_activity_at is never promoted to stale."""
    assert _compute_agent_status("implementing", None) == "implementing"
    assert _compute_agent_status("reviewing", None) == "reviewing"


def test_compute_agent_status_active_recent_stays_active() -> None:
    """Active run with recent activity keeps its status."""
    recent = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
        seconds=60
    )
    assert _compute_agent_status("implementing", recent) == "implementing"


def test_compute_agent_status_active_old_becomes_stale() -> None:
    """Active run whose last_activity_at exceeds the threshold becomes stale."""
    old = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
        seconds=_STALE_THRESHOLD_SECONDS + 60
    )
    assert _compute_agent_status("implementing", old) == "stale"
    assert _compute_agent_status("reviewing", old) == "stale"
    assert _compute_agent_status("pending_launch", old) == "stale"


def test_compute_agent_status_naive_datetime_handled() -> None:
    """Naive datetimes (no tzinfo) are treated as UTC — no TypeError raised."""
    old_naive = datetime.datetime.utcnow() - datetime.timedelta(
        seconds=_STALE_THRESHOLD_SECONDS + 120
    )
    # Must not raise; must detect staleness.
    result = _compute_agent_status("implementing", old_naive)
    assert result == "stale"


# ---------------------------------------------------------------------------
# Unit tests — _get_step_data_for_runs (fast-path only)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_step_data_for_runs_empty_input() -> None:
    """Empty run_ids list returns an empty dict without touching the DB."""
    result = await _get_step_data_for_runs([])
    assert result == {}


# ---------------------------------------------------------------------------
# Unit tests — get_runs_for_issue_numbers fast-path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_runs_for_issue_numbers_empty_input() -> None:
    """Empty issue_numbers returns {} without opening a DB session."""
    result = await get_runs_for_issue_numbers([])
    assert result == {}


# ---------------------------------------------------------------------------
# Integration tests — GET /build/board
# ---------------------------------------------------------------------------


def test_build_board_partial_shows_status_badge(client: TestClient) -> None:
    """GET /build/board includes the agent_status badge text in HTML."""
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={82: _mock_run_dict()},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_open_prs_by_issue",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        resp = client.get("/ship/phase-1/board")

    assert resp.status_code == 200
    # The agent_status "implementing" must appear as a badge in the card.
    assert "implementing" in resp.text


def test_build_board_partial_shows_current_step(client: TestClient) -> None:
    """GET /build/board renders current_step text and step count when provided."""
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={
                82: _mock_run_dict(current_step="Running mypy checks", steps_completed=3)
            },
        ),
        patch(
            "agentception.routes.ui.build_ui.get_open_prs_by_issue",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        resp = client.get("/ship/phase-1/board")

    assert resp.status_code == 200
    assert "Running mypy checks" in resp.text
    assert "3 steps" in resp.text


def test_build_board_partial_no_run_renders_without_error(
    client: TestClient,
) -> None:
    """GET /build/board renders correctly when a card has no agent run."""
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(issues=[_mock_issue(number=99, title="Unassigned issue")]),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_open_prs_by_issue",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        resp = client.get("/ship/phase-1/board")

    assert resp.status_code == 200
    assert "Unassigned issue" in resp.text
    # No run → no status badge present, just the plain card.
    assert "implementing" not in resp.text


# ---------------------------------------------------------------------------
# Regression: complete phase — no Launch button, no @click on cards
# ---------------------------------------------------------------------------


def test_build_board_partial_complete_phase_hides_launch_button(
    client: TestClient,
) -> None:
    """GET /build/board must not render a Launch button for a complete phase."""
    complete_group: list[dict[str, object]] = [
        {
            "label": "phase-0",
            "issues": [
                {
                    "number": 10,
                    "title": "Done issue",
                    "state": "closed",
                    "url": "https://github.com/cgcardona/agentception/issues/10",
                    "labels": ["phase-0"],
                    "depends_on": [],
                    "run": None,
                }
            ],
            "locked": False,
            "complete": True,
            "depends_on": [],
        }
    ]
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=complete_group,
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_open_prs_by_issue",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        resp = client.get("/ship/my-initiative/board")

    assert resp.status_code == 200
    assert "Launch" not in resp.text, "Launch button must not appear on a complete phase"


def test_build_board_partial_complete_phase_cards_not_clickable(
    client: TestClient,
) -> None:
    """Issue cards in a complete phase must not have an inspect-issue @click handler."""
    complete_group: list[dict[str, object]] = [
        {
            "label": "phase-0",
            "issues": [
                {
                    "number": 11,
                    "title": "Completed task",
                    "state": "closed",
                    "url": "https://github.com/cgcardona/agentception/issues/11",
                    "labels": ["phase-0"],
                    "depends_on": [],
                    "run": None,
                }
            ],
            "locked": False,
            "complete": True,
            "depends_on": [],
        }
    ]
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=complete_group,
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_open_prs_by_issue",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        resp = client.get("/ship/my-initiative/board")

    assert resp.status_code == 200
    # The card must carry the done modifier
    assert "build-issue--done" in resp.text
    # The inspect-issue dispatch must not be rendered for this card
    assert "inspect-issue" not in resp.text


# ---------------------------------------------------------------------------
# Regression: initiative-scoped phase grouping (bug: blank board when config
# phase_order belongs to a different initiative)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_issues_grouped_by_phase_initiative_scoped_labels() -> None:
    """Issues with '{initiative}/{phase}' labels must appear in phase groups.

    Regression for the bug where the phase_key lookup only matched old-style
    'phase-N' labels, so initiative-scoped labels like
    'agentception-ux-phase1b-to-phase3/2-ux-implementation' were lost and the
    board rendered empty buckets.

    The fix: when no explicit phase_order is passed and no DB rows exist for
    the initiative, the function falls back to lexicographic sort of the actual
    {initiative}/* labels found on issues, making all issues visible.
    """
    from agentception.db.queries import get_issues_grouped_by_phase
    from agentception.db.models import ACIssue
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    initiative = "agentception-ux-phase1b-to-phase3"
    phase_a = f"{initiative}/0-critical-bugs"
    phase_b = f"{initiative}/1-design-tokens"

    def _make_row(number: int, phase: str) -> ACIssue:
        row = MagicMock(spec=ACIssue)
        row.github_number = number
        row.title = f"Issue {number}"
        row.state = "open"
        row.labels_json = json.dumps([initiative, phase])
        row.phase_label = None
        row.depends_on_json = "[]"
        return row

    mock_rows = [_make_row(1, phase_a), _make_row(2, phase_a), _make_row(3, phase_b)]
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows

    with (
        patch("agentception.db.queries.get_session") as mock_session,
        patch(
            "agentception.db.queries.get_initiative_phase_meta",
            new_callable=AsyncMock,
            return_value=[],  # no DB rows → fall back to lexicographic sort
        ),
    ):
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_cm.execute = AsyncMock(return_value=mock_result)
        mock_session.return_value = mock_cm

        # No explicit phase_order — function must discover from actual issue labels.
        groups = await get_issues_grouped_by_phase(
            "cgcardona/agentception",
            initiative=initiative,
        )

    labels = [g["label"] for g in groups]
    assert phase_a in labels, f"Expected {phase_a!r} in phase groups, got {labels}"
    assert phase_b in labels, f"Expected {phase_b!r} in phase groups, got {labels}"
    issues_in_a = next(g["issues"] for g in groups if g["label"] == phase_a)
    assert len(issues_in_a) == 2
    issues_in_b = next(g["issues"] for g in groups if g["label"] == phase_b)
    assert len(issues_in_b) == 1


@pytest.mark.anyio
async def test_get_issues_grouped_by_phase_phase_key_initiative_prefix() -> None:
    """phase_key must resolve to '{initiative}/{phase}' when no 'phase-N' label exists.

    Regression: the old lookup only checked lbl.startswith('phase-') so
    initiative-scoped phase labels were invisible to the grouper.
    """
    from agentception.db.queries import get_issues_grouped_by_phase
    from agentception.db.models import ACIssue
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    initiative = "my-feature"
    phase = f"{initiative}/0-setup"

    row = MagicMock(spec=ACIssue)
    row.github_number = 42
    row.title = "Setup task"
    row.state = "open"
    row.labels_json = json.dumps([initiative, phase, "enhancement"])
    row.phase_label = None
    row.depends_on_json = "[]"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [row]

    with (
        patch("agentception.db.queries.get_session") as mock_session,
        patch(
            "agentception.db.queries.get_initiative_phase_meta",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_cm.execute = AsyncMock(return_value=mock_result)
        mock_session.return_value = mock_cm

        groups = await get_issues_grouped_by_phase(
            "owner/repo",
            initiative=initiative,
            phase_order=None,
        )

    assert len(groups) == 1
    assert groups[0]["label"] == phase
    assert groups[0]["issues"][0]["number"] == 42


# ---------------------------------------------------------------------------
# State machine unit tests — _compute_swim_lane
#
# These tests encode the formal state machine invariants.  Every transition
# trigger must have at least one test; any code change that breaks an
# invariant will break a test here, making regressions visible immediately.
#
# Invariants:
# I1. closed issue → 'done'  (no exceptions)
# I2. open PR in ac_pull_requests → swim_lane in ('pr_open', 'reviewing', 'done')
# I3. no worktree AND no open PR → swim_lane in ('todo', 'done')
# I4. active run with no open PR → 'active'
# I5. active run with open PR → 'pr_open'  (PR signal wins)
# I6. active reviewer run + open PR → 'reviewing'
# ---------------------------------------------------------------------------


from agentception.db.queries import RunForIssueRow
from agentception.routes.ui.build_ui import _compute_swim_lane


def _run(agent_status: str, pr_number: int | None = None) -> RunForIssueRow:
    """Build a minimal RunForIssueRow for state machine tests."""
    return RunForIssueRow(
        id="run-test",
        role="python-developer",
        cognitive_arch=None,
        status=agent_status,
        agent_status=agent_status,
        pr_number=pr_number,
        branch="feat/issue-99-test",
        spawned_at="2026-03-06T12:00:00+00:00",
        last_activity_at=None,
        current_step=None,
        steps_completed=0,
        steps_total=None,
        tier="engineer",
        org_domain="engineering",
        batch_id="issue-99-batch",
    )


def _open_pr(pr_number: int = 200) -> OpenPRForIssueRow:
    return OpenPRForIssueRow(pr_number=pr_number, head_ref="feat/issue-99-test")


# ── Invariant I1: closed issue always → done ─────────────────────────────────


def test_swim_lane_closed_issue_no_run() -> None:
    """I1: closed issue with no run → done."""
    assert _compute_swim_lane("closed", None, None) == "done"


def test_swim_lane_closed_issue_with_active_run() -> None:
    """I1: closed issue even with an active run → done (state wins over run)."""
    assert _compute_swim_lane("closed", _run("implementing"), None) == "done"


def test_swim_lane_closed_issue_with_open_pr() -> None:
    """I1: closed issue even with an open PR → done (issue close wins over PR)."""
    assert _compute_swim_lane("closed", None, _open_pr()) == "done"


def test_swim_lane_closed_issue_with_run_and_open_pr() -> None:
    """I1: closed issue with both active run and open PR → done."""
    assert _compute_swim_lane("closed", _run("reviewing"), _open_pr()) == "done"


# ── Invariant I2: open PR → pr_open or reviewing or done ─────────────────────


def test_swim_lane_open_pr_no_run() -> None:
    """I2: open PR + no run → pr_open."""
    assert _compute_swim_lane("open", None, _open_pr()) == "pr_open"


def test_swim_lane_open_pr_with_implementing_run() -> None:
    """I2: open PR + implementing run → pr_open (PR signal takes precedence)."""
    assert _compute_swim_lane("open", _run("implementing"), _open_pr()) == "pr_open"


def test_swim_lane_open_pr_with_stale_run() -> None:
    """I2: open PR + stale run → pr_open (PR signal takes precedence)."""
    assert _compute_swim_lane("open", _run("stale"), _open_pr()) == "pr_open"


def test_swim_lane_open_pr_with_done_run() -> None:
    """I2: open PR + done run → pr_open (PR open means issue is not closed)."""
    assert _compute_swim_lane("open", _run("done"), _open_pr()) == "pr_open"


def test_swim_lane_open_pr_with_reviewing_run() -> None:
    """I2+I6: open PR + reviewing run → reviewing."""
    assert _compute_swim_lane("open", _run("reviewing"), _open_pr()) == "reviewing"


# ── Invariant I3: no worktree AND no PR → todo or done ───────────────────────


def test_swim_lane_no_run_no_pr() -> None:
    """I3: no run and no PR → todo."""
    assert _compute_swim_lane("open", None, None) == "todo"


def test_swim_lane_unknown_run_no_pr() -> None:
    """I3: unknown-status run (treated as no effective run) + no PR → todo."""
    assert _compute_swim_lane("open", _run("unknown"), None) == "todo"


def test_swim_lane_done_run_no_pr() -> None:
    """I3: done run with no PR → todo (orphan sweep set unknown; issue not closed)."""
    assert _compute_swim_lane("open", _run("done"), None) == "todo"


# ── Invariant I4: active run + no PR → active ────────────────────────────────


def test_swim_lane_implementing_no_pr() -> None:
    """I4: implementing run + no PR → active."""
    assert _compute_swim_lane("open", _run("implementing"), None) == "active"


def test_swim_lane_pending_launch_no_pr() -> None:
    """I4: pending_launch run + no PR → active."""
    assert _compute_swim_lane("open", _run("pending_launch"), None) == "active"


def test_swim_lane_stale_no_pr() -> None:
    """I4: stale run (active but frozen) + no PR → active."""
    assert _compute_swim_lane("open", _run("stale"), None) == "active"


def test_swim_lane_reviewing_no_pr() -> None:
    """I4: reviewing run but no open PR in ac_pull_requests → active (degraded)."""
    assert _compute_swim_lane("open", _run("reviewing"), None) == "active"


# ── Invariant I5: active run + open PR → pr_open (PR signal wins) ────────────


def test_swim_lane_implementing_with_open_pr() -> None:
    """I5: agent still implementing but PR already open → pr_open."""
    assert _compute_swim_lane("open", _run("implementing"), _open_pr()) == "pr_open"


# ── Invariant I6: reviewer run + open PR → reviewing ─────────────────────────


def test_swim_lane_reviewing_with_open_pr() -> None:
    """I6: reviewer agent active + PR open → reviewing."""
    assert _compute_swim_lane("open", _run("reviewing"), _open_pr()) == "reviewing"


# ── Priority ordering: closed > pr > active > todo ───────────────────────────


def test_swim_lane_priority_closed_beats_pr() -> None:
    """Closed issue beats open PR — issue closure is final."""
    assert _compute_swim_lane("closed", _run("reviewing"), _open_pr()) == "done"


def test_swim_lane_priority_pr_beats_active_run() -> None:
    """Open PR beats active run — PR is the more advanced state."""
    assert _compute_swim_lane("open", _run("implementing"), _open_pr()) == "pr_open"


def test_swim_lane_priority_active_beats_todo() -> None:
    """Active run beats todo — agent is working even without a PR."""
    assert _compute_swim_lane("open", _run("implementing"), None) == "active"
