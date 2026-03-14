from __future__ import annotations

"""Unit tests for get_issues_grouped_by_phase.

Covers the three ordering paths:
1. DB-canonical: initiative_phases rows present → order by phase_order ASC.
2. Legacy-sort:  no DB rows, issues present → lexicographic sort of labels.
3. Empty-state:  no DB rows, no issues → return [].

And regression coverage for locking logic.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.db.models import ACIssue
from agentception.db.queries import InitiativePhaseMeta, get_issues_grouped_by_phase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(number: int, initiative: str, phase: str, state: str = "open") -> ACIssue:
    row = MagicMock(spec=ACIssue)
    row.github_number = number
    row.title = f"Issue {number}"
    row.state = state
    row.labels_json = json.dumps([initiative, phase])
    row.phase_label = None
    row.depends_on_json = "[]"
    return row


def _mock_session(rows: list[ACIssue]) -> MagicMock:
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    mock_cm.execute = AsyncMock(return_value=mock_result)
    return mock_cm


# ---------------------------------------------------------------------------
# Path 1: DB-canonical ordering via phase_order
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_db_canonical_order_respected() -> None:
    """When initiative_phases rows exist, phase_order is the source of truth.

    Even if label strings would sort differently, the DB-stored phase_order
    must be used.
    """
    initiative = "ac-auth"
    # Deliberately out of lexicographic order: "3-" before "0-" in DB rows
    meta: list[InitiativePhaseMeta] = [
        InitiativePhaseMeta(label=f"{initiative}/3-polish", order=0, depends_on=[]),
        InitiativePhaseMeta(label=f"{initiative}/0-foundation", order=1, depends_on=[f"{initiative}/3-polish"]),
    ]
    rows = [
        _make_issue(1, initiative, f"{initiative}/3-polish"),
        _make_issue(2, initiative, f"{initiative}/0-foundation"),
    ]

    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock, return_value=meta),
    ):
        mock_get_session.return_value = _mock_session(rows)
        groups = await get_issues_grouped_by_phase("owner/repo", initiative=initiative)

    assert len(groups) == 2
    # DB says phase_order=0 is "3-polish", phase_order=1 is "0-foundation"
    assert groups[0]["label"] == f"{initiative}/3-polish"
    assert groups[1]["label"] == f"{initiative}/0-foundation"


@pytest.mark.anyio
async def test_db_canonical_order_dep_graph_used_for_locking() -> None:
    """phase_deps from the DB meta are used to compute locked state."""
    initiative = "ac-auth"
    meta: list[InitiativePhaseMeta] = [
        InitiativePhaseMeta(label=f"{initiative}/0-foundation", order=0, depends_on=[]),
        InitiativePhaseMeta(
            label=f"{initiative}/1-api",
            order=1,
            depends_on=[f"{initiative}/0-foundation"],
        ),
    ]
    # 0-foundation has one open issue (not complete) → 1-api must be locked
    rows = [
        _make_issue(1, initiative, f"{initiative}/0-foundation", state="open"),
        _make_issue(2, initiative, f"{initiative}/1-api", state="open"),
    ]

    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock, return_value=meta),
    ):
        mock_get_session.return_value = _mock_session(rows)
        groups = await get_issues_grouped_by_phase("owner/repo", initiative=initiative)

    assert len(groups) == 2
    foundation = next(g for g in groups if g["label"].endswith("/0-foundation"))
    api = next(g for g in groups if g["label"].endswith("/1-api"))
    assert not foundation["locked"]
    assert api["locked"]


@pytest.mark.anyio
async def test_db_canonical_order_unlocks_when_dep_complete() -> None:
    """1-api unlocks when 0-foundation is complete (all issues closed)."""
    initiative = "ac-auth"
    meta: list[InitiativePhaseMeta] = [
        InitiativePhaseMeta(label=f"{initiative}/0-foundation", order=0, depends_on=[]),
        InitiativePhaseMeta(
            label=f"{initiative}/1-api",
            order=1,
            depends_on=[f"{initiative}/0-foundation"],
        ),
    ]
    rows = [
        _make_issue(1, initiative, f"{initiative}/0-foundation", state="closed"),
        _make_issue(2, initiative, f"{initiative}/1-api", state="open"),
    ]

    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock, return_value=meta),
    ):
        mock_get_session.return_value = _mock_session(rows)
        groups = await get_issues_grouped_by_phase("owner/repo", initiative=initiative)

    foundation = next(g for g in groups if g["label"].endswith("/0-foundation"))
    api = next(g for g in groups if g["label"].endswith("/1-api"))
    assert foundation["complete"]
    assert not api["locked"]


# ---------------------------------------------------------------------------
# Path 2: Legacy-sort fallback (no DB rows)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_legacy_sort_used_when_no_db_rows() -> None:
    """When initiative_phases is empty, labels are sorted lexicographically.

    The {N}-slug convention makes this produce correct order naturally.
    """
    initiative = "ac-reliability"
    rows = [
        _make_issue(1, initiative, f"{initiative}/1-monitoring"),
        _make_issue(2, initiative, f"{initiative}/0-infra"),
        _make_issue(3, initiative, f"{initiative}/3-robustness"),
        _make_issue(4, initiative, f"{initiative}/2-quality"),
    ]

    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock, return_value=[]),
    ):
        mock_get_session.return_value = _mock_session(rows)
        groups = await get_issues_grouped_by_phase("owner/repo", initiative=initiative)

    labels = [g["label"] for g in groups]
    assert labels == [
        f"{initiative}/0-infra",
        f"{initiative}/1-monitoring",
        f"{initiative}/2-quality",
        f"{initiative}/3-robustness",
    ]


@pytest.mark.anyio
async def test_legacy_sort_all_phases_unlocked_when_no_db_deps() -> None:
    """No DB dep graph → all phases unlocked (correct default)."""
    initiative = "ac-legacy"
    rows = [
        _make_issue(1, initiative, f"{initiative}/0-base"),
        _make_issue(2, initiative, f"{initiative}/1-feature"),
    ]

    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock, return_value=[]),
    ):
        mock_get_session.return_value = _mock_session(rows)
        groups = await get_issues_grouped_by_phase("owner/repo", initiative=initiative)

    assert all(not g["locked"] for g in groups)


# ---------------------------------------------------------------------------
# Path 3: Empty-state (no DB rows, no issues)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_empty_state_returns_empty_list() -> None:
    """No issues and no DB rows → empty list, no phantom phase rows."""
    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock, return_value=[]),
    ):
        mock_get_session.return_value = _mock_session([])
        groups = await get_issues_grouped_by_phase("owner/repo", initiative="ac-new")

    assert groups == []


# ---------------------------------------------------------------------------
# Explicit phase_order arg overrides everything
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_explicit_phase_order_arg_overrides_db() -> None:
    """An explicit phase_order list takes precedence over DB meta."""
    initiative = "ac-override"
    explicit_order = [f"{initiative}/0-base", f"{initiative}/1-feature"]
    rows = [
        _make_issue(1, initiative, f"{initiative}/1-feature"),
        _make_issue(2, initiative, f"{initiative}/0-base"),
    ]

    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        # get_initiative_phase_meta should NOT be called when phase_order is given
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock, return_value=[]) as mock_meta,
    ):
        mock_get_session.return_value = _mock_session(rows)
        groups = await get_issues_grouped_by_phase(
            "owner/repo", initiative=initiative, phase_order=explicit_order
        )

    mock_meta.assert_not_called()
    assert [g["label"] for g in groups] == explicit_order


# ---------------------------------------------------------------------------
# No initiative scope (legacy unscoped query)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_initiative_does_not_call_meta_and_needs_explicit_order() -> None:
    """When initiative is None, get_initiative_phase_meta is never called.

    Phase key detection requires an initiative to scope the label lookup, so
    issues without a known initiative are silently dropped.  The caller (the
    board route) always supplies an initiative; this path exists only for
    callers that pass an explicit phase_order instead.
    """
    rows = [_make_issue(1, "ac-x", "ac-x/0-base")]

    with (
        patch("agentception.db.queries.board.get_session") as mock_get_session,
        patch("agentception.db.queries.board.get_initiative_phase_meta", new_callable=AsyncMock) as mock_meta,
    ):
        mock_get_session.return_value = _mock_session(rows)
        groups = await get_issues_grouped_by_phase("owner/repo", initiative=None)

    mock_meta.assert_not_called()
    # Without an initiative the phase key cannot be detected — result is empty.
    assert groups == []


# ---------------------------------------------------------------------------
# PlanPhase label validator
# ---------------------------------------------------------------------------


def test_plan_phase_label_valid_formats() -> None:
    """Valid {N}-{slug} labels pass the validator."""
    from agentception.models import PlanIssue, PlanPhase

    issue = PlanIssue(id="t-p0-001", title="T", body="B")
    valid_labels = ["0-foundation", "1-api-layer", "2-ui", "3-polish", "10-observability", "0-a"]
    for label in valid_labels:
        phase = PlanPhase(label=label, description="desc", issues=[issue])
        assert phase.label == label


def test_plan_phase_label_invalid_formats_raise() -> None:
    """Labels not matching {N}-{slug} raise ValidationError."""
    from pydantic import ValidationError

    from agentception.models import PlanIssue, PlanPhase

    issue = PlanIssue(id="t-p0-001", title="T", body="B")
    invalid_labels = [
        "phase-0",      # old convention — not allowed
        "foundation",   # missing numeric prefix
        "0",            # no slug
        "0-",           # empty slug
        "0-UPPER",      # uppercase not allowed
        "-foundation",  # missing number
    ]
    for label in invalid_labels:
        with pytest.raises(ValidationError):
            PlanPhase(label=label, description="desc", issues=[issue])
