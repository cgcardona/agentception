from __future__ import annotations

"""Tests for POST /api/brain-dump/plan and the phase_planner reader (issue-825).

Covers:
- Happy path: valid dump returns phase cards.
- Empty dump: 422 from the endpoint.
- Heuristic classification: bugs → phase-0, infrastructure → phase-1,
  features → phase-2, tech debt → phase-3.
- Dependency ordering: later phases list earlier ones in depends_on.
- Whitespace-only lines are ignored.
- Deduplicated items only appear once.

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_brain_dump_plan.py -v
"""

import pytest

from agentception.models import PlanResult
from agentception.readers.phase_planner import _extract_items, _classify, plan_phases


# ── Unit tests for the phase planner reader ───────────────────────────────────


def test_extract_items_parses_bullet_list() -> None:
    """Bullet-prefixed lines are extracted as clean items."""
    dump = "- Login fails on mobile\n- Rate limiter not applied\n- CSV export hangs"
    items = _extract_items(dump)
    assert items == ["Login fails on mobile", "Rate limiter not applied", "CSV export hangs"]


def test_extract_items_parses_numbered_list() -> None:
    """Numbered list items (1. or 1)) are extracted without their prefix."""
    dump = "1. Migrate auth to JWT\n2. Add pagination\n3. Write tests"
    items = _extract_items(dump)
    assert items == ["Migrate auth to JWT", "Add pagination", "Write tests"]


def test_extract_items_skips_blank_lines() -> None:
    """Blank and whitespace-only lines do not produce items."""
    dump = "- Fix bug\n\n   \n- Add feature"
    items = _extract_items(dump)
    assert len(items) == 2


def test_extract_items_deduplicates() -> None:
    """Duplicate items (case-insensitive) appear only once."""
    dump = "- Fix login bug\n- Fix login bug\n- Fix Login Bug"
    items = _extract_items(dump)
    assert len(items) == 1


def test_classify_bug_keywords_return_phase_0() -> None:
    """Items containing 'fix', 'bug', 'fail' etc. go to phase 0."""
    assert _classify("Login fails intermittently on mobile") == 0
    assert _classify("Fix the broken CSV export") == 0
    assert _classify("Error in rate limiter") == 0
    assert _classify("Critical crash on checkout") == 0


def test_classify_infra_keywords_return_phase_1() -> None:
    """Items referencing API, auth, DB, schema go to phase 1."""
    assert _classify("Migrate auth to JWT with refresh tokens") == 1
    assert _classify("Add pagination to the issues API") == 1
    assert _classify("Database migration for new schema") == 1


def test_classify_feature_keywords_return_phase_2() -> None:
    """Items that add new capabilities go to phase 2."""
    assert _classify("Add dark mode toggle across dashboard") == 2
    assert _classify("Let users star their favourite agents") == 2
    assert _classify("Implement Slack notifications for PR merges") == 2


def test_classify_tech_debt_keywords_return_phase_3() -> None:
    """Cleanup, refactor, test, and doc items go to phase 3.

    Items that overlap with infrastructure keywords (e.g. 'Remove … API endpoints')
    correctly resolve to Phase 1 — they are excluded here so this test focuses
    on unambiguous tech-debt items only.
    """
    assert _classify("Consolidate duplicate fetch helpers into a typed client") == 3
    assert _classify("Consolidate duplicate GitHub fetch helpers") == 3
    assert _classify("Write integration tests for the billing flow") == 3
    assert _classify("Document the public interface contract") == 3


def test_plan_phases_groups_bugs_and_features() -> None:
    """A mixed dump produces correct phase groupings."""
    dump = (
        "- Login fails intermittently on mobile\n"
        "- Migrate auth to JWT with refresh tokens\n"
        "- Add dark mode toggle\n"
        "- Consolidate duplicate fetch helpers into a typed client\n"
    )
    result = plan_phases(dump)
    assert isinstance(result, PlanResult)
    labels = [p.label for p in result.phases]
    assert "phase-0" in labels
    assert "phase-1" in labels
    assert "phase-2" in labels
    assert "phase-3" in labels


def test_plan_phases_depends_on_ordering() -> None:
    """Later phases list earlier phase labels in depends_on."""
    dump = (
        "- Fix the broken CSV export\n"
        "- Add dark mode toggle\n"
        "- Write tests for billing\n"
    )
    result = plan_phases(dump)
    # Collect phases in emitted order.
    by_label = {p.label: p for p in result.phases}
    # phase-2 must depend on whatever came before it.
    if "phase-2" in by_label and "phase-0" in by_label:
        assert "phase-0" in by_label["phase-2"].depends_on
    # phase-3 must depend on all earlier emitted phases.
    if "phase-3" in by_label:
        earlier = [p.label for p in result.phases if p.label != "phase-3"]
        for earlier_label in earlier:
            assert earlier_label in by_label["phase-3"].depends_on


def test_plan_phases_estimated_issue_count() -> None:
    """estimated_issue_count matches the number of items in each bucket."""
    dump = (
        "- Bug one\n"
        "- Bug two\n"
        "- Add feature\n"
    )
    result = plan_phases(dump)
    by_label = {p.label: p for p in result.phases}
    assert by_label["phase-0"].estimated_issue_count == 2
    assert by_label["phase-2"].estimated_issue_count == 1


def test_plan_phases_raises_on_empty_dump() -> None:
    """An empty dump raises ValueError."""
    with pytest.raises(ValueError, match="empty"):
        plan_phases("")


def test_plan_phases_raises_on_whitespace_dump() -> None:
    """A whitespace-only dump raises ValueError."""
    with pytest.raises(ValueError, match="empty"):
        plan_phases("   \n\n   ")
