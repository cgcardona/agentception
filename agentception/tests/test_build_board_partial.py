from __future__ import annotations

"""Tests for build board partial enrichment with agent status, current step,
and progress data (issue #82).

Coverage:
- _compute_agent_status() returns normalized status for non-active runs
- _compute_agent_status() marks active run stale when last_activity_at is old
- _compute_agent_status() keeps active status for recent activity
- _compute_agent_status() handles None last_activity_at safely
- get_runs_for_issue_numbers() returns empty dict for empty input
- build board partial (GET /build/board) suppresses status badge for
  "implementing" and "reviewing" active-lane cards
- build board partial renders ⚠ stale badge for "stale" active-lane cards
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
from agentception.types import JsonValue


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
) -> dict[str, JsonValue]:
    """Build a minimal RunForIssueRow-shaped dict for mock patching."""
    return {
        "id": "issue-82",
        "role": "developer",
        "status": status,
        "agent_status": agent_status,
        "pr_number": None,
        "branch": "agent/issue-82",
        "spawned_at": "2026-03-04T23:00:00+00:00",
        "last_activity_at": None,
        "current_step": current_step,
        "steps_completed": steps_completed,
    }


def _mock_issue(number: int = 82, title: str = "Enrich build board") -> dict[str, JsonValue]:
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
    issues: list[dict[str, JsonValue]] | None = None,
) -> list[dict[str, JsonValue]]:
    issue_list: list[JsonValue] = []
    issue_list.extend(issues or [_mock_issue()])
    return [
        {
            "label": "phase-1",
            "issues": issue_list,
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


def test_compute_agent_status_utc_aware_stale_detected() -> None:
    """UTC-aware datetimes older than the threshold are detected as stale."""
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        seconds=_STALE_THRESHOLD_SECONDS + 120
    )
    result = _compute_agent_status("implementing", old)
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
    """GET /build/board suppresses the status badge for 'implementing' active-lane cards."""
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
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={82: {"lane": "active", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    # The status badge is suppressed for 'implementing' — the Active lane position is signal enough.
    assert "build-issue__status--implementing" not in resp.text


def test_build_board_partial_reviewing_suppresses_status_badge(client: TestClient) -> None:
    """GET /build/board must NOT render a status badge for a 'reviewing' active-lane card.

    The template suppresses the badge for both 'implementing' and 'reviewing'
    statuses — only 'stale' renders a visible badge.
    """
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={82: _mock_run_dict(agent_status="reviewing", status="reviewing")},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={82: {"lane": "active", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    # The status badge is suppressed for 'reviewing' — the Active lane position is signal enough.
    assert "build-issue__status--reviewing" not in resp.text


def test_build_board_partial_stale_renders_status_badge(client: TestClient) -> None:
    """GET /build/board must render the ⚠ stale badge for a 'stale' active-lane card.

    The template only renders a status badge when agent_status is 'stale'.
    """
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={82: _mock_run_dict(agent_status="stale", status="stale")},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={82: {"lane": "active", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    assert "build-issue__status--stale" in resp.text
    assert "⚠ stale" in resp.text


def test_pending_launch_badge_visible_in_active_lane(client: TestClient) -> None:
    """GET /build/board must render the pending_launch badge in the active lane."""
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={82: _mock_run_dict(agent_status="pending_launch", status="pending_launch")},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={82: {"lane": "active", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    assert "build-issue__status--pending_launch" in resp.text
    assert "pending_launch" in resp.text


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
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={82: {"lane": "active", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    assert "Running mypy checks" in resp.text
    assert "build-issue__step-badge" in resp.text
    # steps_completed counter is removed from the template
    assert "3 steps" not in resp.text


def test_build_board_partial_step_badge_renders_step_text(client: TestClient) -> None:
    """GET /build/board renders current_step inside the build-issue__step-badge element.

    When current_step is "Step 7", the rendered HTML must contain "Step 7" and
    that text must appear inside a tag with class="build-issue__step-badge".
    """
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={82: _mock_run_dict(current_step="Step 7")},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={82: {"lane": "active", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    assert "Step 7" in resp.text
    # "Step 7" must appear inside the step-badge element, not just anywhere in the page.
    assert 'class="build-issue__step-badge"' in resp.text
    badge_start = resp.text.find('class="build-issue__step-badge"')
    badge_end = resp.text.find("</span>", badge_start)
    assert "Step 7" in resp.text[badge_start:badge_end], (
        "Step 7 must appear inside the build-issue__step-badge span"
    )


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
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    assert "Unassigned issue" in resp.text
    # No run → no status badge present, just the plain card.
    assert "implementing" not in resp.text


def test_build_board_active_lane_no_run_is_clickable_inspect_issue(
    client: TestClient,
) -> None:
    """Active-lane cards without a run must dispatch inspect-issue so Mission Control is not stuck Idle."""
    issue = _mock_issue(number=100, title="Active but no run yet")
    with (
        patch(
            "agentception.routes.ui.build_ui.get_issues_grouped_by_phase",
            new_callable=AsyncMock,
            return_value=_mock_group(issues=[issue]),
        ),
        patch(
            "agentception.routes.ui.build_ui.get_runs_for_issue_numbers",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={100: {"lane": "active", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/phase-1/board")

    assert resp.status_code == 200
    assert "inspect-issue" in resp.text
    assert "build-issue--selectable" in resp.text


# ---------------------------------------------------------------------------
# Regression: complete phase — no Launch button, no @click on cards
# ---------------------------------------------------------------------------


def test_build_board_partial_complete_phase_hides_launch_button(
    client: TestClient,
) -> None:
    """GET /build/board must not render a Launch button for a complete phase."""
    complete_group: list[dict[str, JsonValue]] = [
        {
            "label": "phase-0",
            "issues": [
                {
                    "number": 10,
                    "title": "Done issue",
                    "body_excerpt": "",
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
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={10: {"lane": "done", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/my-initiative/board")

    assert resp.status_code == 200
    assert "Launch" not in resp.text, "Launch button must not appear on a complete phase"


def test_build_board_partial_complete_phase_cards_not_clickable(
    client: TestClient,
) -> None:
    """Issue cards in a complete phase must not have an inspect-issue @click handler."""
    complete_group: list[dict[str, JsonValue]] = [
        {
            "label": "phase-0",
            "issues": [
                {
                    "number": 11,
                    "title": "Completed task",
                    "body_excerpt": "",
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
            "agentception.routes.ui.build_ui.get_workflow_states_by_issue",
            new_callable=AsyncMock,
            return_value={11: {"lane": "done", "pr_number": None}},
        ),
    ):
        resp = client.get("/ship/agentception/my-initiative/board")

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
        patch("agentception.db.queries.board.get_session") as mock_session,
        patch(
            "agentception.db.queries.board.get_initiative_phase_meta",
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
        patch("agentception.db.queries.board.get_session") as mock_session,
        patch(
            "agentception.db.queries.board.get_initiative_phase_meta",
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
# Regression: build_report_done wires the PR→issue link immediately
#
# When an agent calls build_report_done(issue_number=N, pr_url=".../pull/M"),
# persist_pr_link_and_recompute must be called with the correct pr_number and
# issue_number so the board card moves to pr_open on the next refresh —
# without waiting for the poller.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_persist_agent_event_done_calls_link_and_recompute() -> None:
    """build_report_done triggers persist_pr_link_and_recompute with correct args."""
    from unittest.mock import AsyncMock, patch

    from agentception.db.persist import persist_agent_event

    with patch(
        "agentception.db.persist.persist_pr_link_and_recompute",
        new_callable=AsyncMock,
    ) as mock_recompute:
        await persist_agent_event(
            issue_number=161,
            event_type="done",
            payload={"pr_url": "https://github.com/cgcardona/agentception/pull/169"},
        )

    mock_recompute.assert_awaited_once()
    call_args = mock_recompute.call_args
    assert call_args.args[0] == 169   # pr_number
    assert call_args.args[1] == 161   # issue_number


@pytest.mark.anyio
async def test_persist_agent_event_non_done_does_not_call_recompute() -> None:
    """step_start and blocker events must not trigger persist_pr_link_and_recompute."""
    from unittest.mock import AsyncMock, patch

    from agentception.db.persist import persist_agent_event

    with patch(
        "agentception.db.persist.persist_pr_link_and_recompute",
        new_callable=AsyncMock,
    ) as mock_recompute:
        await persist_agent_event(
            issue_number=161,
            event_type="step_start",
            payload={"step": "Reading codebase"},
        )

    mock_recompute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Regression: closed deps must not appear as blockers on board cards
#
# Issue #175 depends on #176. #176 is closed. The board was still showing
# "⊘ blocked by #176" because get_issues_grouped_by_phase used the raw
# depends_on_json without filtering out closed issues.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_issues_grouped_by_phase_filters_closed_deps() -> None:
    """depends_on chips must only list deps that are still open.

    Regression: issue A depends on B. B closes. A's card must clear the
    blocked-by chip — closed deps are resolved and must not dim the card.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    from agentception.db.models import ACIssue
    from agentception.db.queries import get_issues_grouped_by_phase

    initiative = "test-init"
    phase = f"{initiative}/0-work"

    def _make_row(number: int, state: str, depends_on: list[int]) -> MagicMock:
        row = MagicMock(spec=ACIssue)
        row.github_number = number
        row.title = f"Issue {number}"
        row.state = state
        row.labels_json = json.dumps([initiative, phase])
        row.phase_label = None
        row.depends_on_json = json.dumps(depends_on)
        row.body = None
        return row

    # #10 depends on #11; #11 is closed.
    rows = [_make_row(10, "open", [11]), _make_row(11, "closed", [])]
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows

    with (
        patch("agentception.db.queries.board.get_session") as mock_session,
        patch(
            "agentception.db.queries.board.get_initiative_phase_meta",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_session_obj = MagicMock()
        mock_session_obj.execute = AsyncMock(return_value=mock_result)
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = mock_cm

        groups = await get_issues_grouped_by_phase("owner/repo", initiative=initiative)

    issue_10 = next(i for g in groups for i in g["issues"] if i["number"] == 10)
    assert issue_10["depends_on"] == [], (
        "Closed dep #11 must not appear in depends_on — it is resolved"
    )


# ---------------------------------------------------------------------------
# _body_excerpt: section headers must be skipped so cards show prose, not labels
# ---------------------------------------------------------------------------


def test_body_excerpt_skips_leading_section_header() -> None:
    """Cards must show prose text, not the markdown section label.

    Regression: bodies structured as '## Context\\nActual text' were excerpted
    as 'Context Actual text' because the regex stripped '#' chars but kept the
    header word. The fix skips any leading header lines so the first prose
    paragraph leads.
    """
    from agentception.db.queries import _body_excerpt

    body = "## Context\nEven after the field is propagated the architecture may not be woven in.\n\n## Objective\nFix it."
    result = _body_excerpt(body)
    assert not result.startswith("Context"), f"Header word leaked into excerpt: {result!r}"
    assert "Even after" in result


def test_body_excerpt_stops_at_first_paragraph() -> None:
    """Only the first prose paragraph is excerpted, not subsequent sections."""
    from agentception.db.queries import _body_excerpt

    body = "## Context\nFirst paragraph text.\n\n## Objective\nSecond section should not appear."
    result = _body_excerpt(body)
    assert "Second section" not in result


def test_body_excerpt_no_headers_returns_first_paragraph() -> None:
    """Bodies without section headers return the first paragraph verbatim."""
    from agentception.db.queries import _body_excerpt

    body = "Plain description here.\n\nSecond paragraph ignored."
    result = _body_excerpt(body)
    assert result == "Plain description here."


def test_body_excerpt_all_headers_returns_empty() -> None:
    """A body consisting only of headers returns an empty string."""
    from agentception.db.queries import _body_excerpt

    body = "## Context\n## Objective\n## Notes"
    assert _body_excerpt(body) == ""
