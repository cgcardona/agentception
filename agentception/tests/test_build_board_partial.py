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
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
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
        "state": "open",
        "url": f"https://github.com/cgcardona/agentception/issues/{number}",
        "labels": ["phase-1"],
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
            "agentception.routes.ui.build_ui._phase_order",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        resp = client.get("/build/board?initiative=phase-1")

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
            "agentception.routes.ui.build_ui._phase_order",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        resp = client.get("/build/board?initiative=phase-1")

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
            "agentception.routes.ui.build_ui._phase_order",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        resp = client.get("/build/board?initiative=phase-1")

    assert resp.status_code == 200
    assert "Unassigned issue" in resp.text
    # No run → Assign button should appear instead of a status badge.
    assert "Assign" in resp.text


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
        return row

    mock_rows = [_make_row(1, phase_a), _make_row(2, phase_a), _make_row(3, phase_b)]
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows

    with (
        patch("agentception.db.queries.get_session") as mock_session,
        patch(
            "agentception.db.queries.get_initiative_phase_deps",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_cm.execute = AsyncMock(return_value=mock_result)
        mock_session.return_value = mock_cm

        # Simulate config phase_order belonging to a DIFFERENT initiative.
        wrong_phase_order = ["ac-ui/0-critical-bugs", "ac-ui/1-design-tokens"]
        groups = await get_issues_grouped_by_phase(
            "cgcardona/agentception",
            initiative=initiative,
            phase_order=wrong_phase_order,
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

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [row]

    with (
        patch("agentception.db.queries.get_session") as mock_session,
        patch(
            "agentception.db.queries.get_initiative_phase_deps",
            new_callable=AsyncMock,
            return_value={},
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
