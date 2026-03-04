"""Tests for EnrichedIssue, EnrichedPhase, and EnrichedManifest Pydantic models (AC-868).

Invariants verified:
- parallel_groups consistency: no issue in a group may depends_on another title
  in the same group.
- total_issues equals the actual sum of issues across all phases.
- estimated_waves equals the longest dependency chain (critical path).
- JSON round-trip is lossless: model_dump_json() → parse back → equal.
- Empty phases raises ValueError.
- Invalid parallel_groups (dep within group) raises ValidationError.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentception.models import EnrichedIssue, EnrichedManifest, EnrichedPhase


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _issue(
    title: str,
    depends_on: list[str] | None = None,
    phase: str = "0-foundation",
) -> EnrichedIssue:
    return EnrichedIssue(
        title=title,
        body=f"## {title}\n\nDo the work.",
        labels=["enhancement"],
        phase=phase,
        depends_on=depends_on or [],
        can_parallel=True,
        acceptance_criteria=["AC 1", "AC 2"],
        tests_required=["test_something"],
        docs_required=["docs/reference/api.md"],
    )


def _phase(
    label: str = "0-foundation",
    issues: list[EnrichedIssue] | None = None,
    parallel_groups: list[list[str]] | None = None,
    depends_on: list[str] | None = None,
) -> EnrichedPhase:
    if issues is None:
        issues = [_issue("Issue A")]
    if parallel_groups is None:
        parallel_groups = [[iss.title for iss in issues]]
    return EnrichedPhase(
        label=label,
        description=f"Phase {label}",
        depends_on=depends_on or [],
        issues=issues,
        parallel_groups=parallel_groups,
    )


def _manifest(phases: list[EnrichedPhase] | None = None) -> EnrichedManifest:
    if phases is None:
        phases = [_phase()]
    return EnrichedManifest(initiative="test-initiative", phases=phases)


# ---------------------------------------------------------------------------
# parallel_groups consistency
# ---------------------------------------------------------------------------


def test_parallel_groups_valid_independent_issues() -> None:
    """Issues with no shared deps can be in the same parallel group."""
    a = _issue("A")
    b = _issue("B")
    phase = _phase(issues=[a, b], parallel_groups=[["A", "B"]])
    assert phase.parallel_groups == [["A", "B"]]


def test_parallel_groups_valid_sequential_groups() -> None:
    """B depends on A — placing them in separate groups is valid."""
    a = _issue("A")
    b = _issue("B", depends_on=["A"])
    phase = _phase(issues=[a, b], parallel_groups=[["A"], ["B"]])
    assert len(phase.parallel_groups) == 2


def test_parallel_groups_invalid_dep_within_group_raises() -> None:
    """B depends on A — placing both in the same group raises ValidationError."""
    a = _issue("A")
    b = _issue("B", depends_on=["A"])
    with pytest.raises(ValidationError, match="same parallel group"):
        _phase(issues=[a, b], parallel_groups=[["A", "B"]])


def test_parallel_groups_invalid_mutual_dep_raises() -> None:
    """Two issues that depend on each other cannot share a group."""
    a = _issue("A", depends_on=["B"])
    b = _issue("B", depends_on=["A"])
    with pytest.raises(ValidationError, match="same parallel group"):
        _phase(issues=[a, b], parallel_groups=[["A", "B"]])


def test_parallel_groups_dep_in_different_group_is_valid() -> None:
    """Dep in a different group than the depender is fine."""
    a = _issue("A")
    b = _issue("B", depends_on=["A"])
    c = _issue("C", depends_on=["A"])
    phase = _phase(issues=[a, b, c], parallel_groups=[["A"], ["B", "C"]])
    assert phase.parallel_groups == [["A"], ["B", "C"]]


# ---------------------------------------------------------------------------
# total_issues
# ---------------------------------------------------------------------------


def test_total_issues_single_phase() -> None:
    """total_issues reflects count of issues in a single phase."""
    a = _issue("A")
    b = _issue("B")
    manifest = _manifest([_phase(issues=[a, b], parallel_groups=[["A", "B"]])])
    assert manifest.total_issues == 2


def test_total_issues_multiple_phases() -> None:
    """total_issues sums across all phases."""
    phase0 = _phase("0-foundation", issues=[_issue("A"), _issue("B")], parallel_groups=[["A", "B"]])
    phase1 = _phase(
        "1-api",
        issues=[_issue("C", phase="1-api"), _issue("D", phase="1-api"), _issue("E", phase="1-api")],
        parallel_groups=[["C", "D", "E"]],
    )
    manifest = _manifest([phase0, phase1])
    assert manifest.total_issues == 5


def test_total_issues_cannot_be_overridden_by_caller() -> None:
    """Caller-supplied total_issues is overwritten by the model_validator."""
    manifest = EnrichedManifest(
        initiative="override-test",
        phases=[_phase(issues=[_issue("Solo")])],
        total_issues=999,  # wrong — should be overwritten to 1
    )
    assert manifest.total_issues == 1


# ---------------------------------------------------------------------------
# estimated_waves
# ---------------------------------------------------------------------------


def test_estimated_waves_no_deps_is_one() -> None:
    """All independent issues → one wave."""
    a = _issue("A")
    b = _issue("B")
    manifest = _manifest([_phase(issues=[a, b], parallel_groups=[["A", "B"]])])
    assert manifest.estimated_waves == 1


def test_estimated_waves_linear_chain() -> None:
    """A → B → C chain requires 3 waves."""
    a = _issue("A")
    b = _issue("B", depends_on=["A"])
    c = _issue("C", depends_on=["B"])
    phase = _phase(issues=[a, b, c], parallel_groups=[["A"], ["B"], ["C"]])
    manifest = _manifest([phase])
    assert manifest.estimated_waves == 3


def test_estimated_waves_diamond_dep() -> None:
    """Diamond: A→B, A→C, B+C→D — critical path is A→B→D (or A→C→D), length 3."""
    a = _issue("A")
    b = _issue("B", depends_on=["A"])
    c = _issue("C", depends_on=["A"])
    d = _issue("D", depends_on=["B", "C"])
    phase = _phase(
        issues=[a, b, c, d],
        parallel_groups=[["A"], ["B", "C"], ["D"]],
    )
    manifest = _manifest([phase])
    assert manifest.estimated_waves == 3


def test_estimated_waves_cross_phase_deps() -> None:
    """estimated_waves is computed across all phases, not per phase."""
    phase0_issue = _issue("Foundation", phase="0-foundation")
    phase1_issue = _issue("API", depends_on=["Foundation"], phase="1-api")
    phase2_issue = _issue("UI", depends_on=["API"], phase="2-ui")

    phase0 = _phase("0-foundation", issues=[phase0_issue], parallel_groups=[["Foundation"]])
    phase1 = _phase(
        "1-api",
        issues=[phase1_issue],
        parallel_groups=[["API"]],
        depends_on=["0-foundation"],
    )
    phase2 = _phase(
        "2-ui",
        issues=[phase2_issue],
        parallel_groups=[["UI"]],
        depends_on=["1-api"],
    )
    manifest = _manifest([phase0, phase1, phase2])
    assert manifest.estimated_waves == 3


def test_estimated_waves_cannot_be_overridden_by_caller() -> None:
    """Caller-supplied estimated_waves is overwritten by the model_validator."""
    manifest = EnrichedManifest(
        initiative="waves-override",
        phases=[_phase(issues=[_issue("Solo")])],
        estimated_waves=42,  # wrong — overwritten
    )
    assert manifest.estimated_waves == 1


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


def test_json_round_trip_lossless() -> None:
    """model_dump_json() → parse back → equal to original."""
    a = _issue("A")
    b = _issue("B", depends_on=["A"])
    phase = _phase(issues=[a, b], parallel_groups=[["A"], ["B"]])
    original = EnrichedManifest(initiative="round-trip-test", phases=[phase])
    json_str = original.model_dump_json()
    restored = EnrichedManifest.model_validate_json(json_str)
    assert restored == original


def test_json_round_trip_preserves_computed_fields() -> None:
    """Round-tripped model has correct total_issues and estimated_waves."""
    a = _issue("A")
    b = _issue("B", depends_on=["A"])
    phase = _phase(issues=[a, b], parallel_groups=[["A"], ["B"]])
    manifest = EnrichedManifest(initiative="computed-rt", phases=[phase])
    restored = EnrichedManifest.model_validate_json(manifest.model_dump_json())
    assert restored.total_issues == 2
    assert restored.estimated_waves == 2


def test_json_round_trip_multi_phase() -> None:
    """Multi-phase manifest survives JSON round-trip."""
    p0 = _phase("0-foundation", issues=[_issue("X")], parallel_groups=[["X"]])
    p1 = _phase(
        "1-api",
        issues=[_issue("Y", depends_on=["X"], phase="1-api")],
        parallel_groups=[["Y"]],
        depends_on=["0-foundation"],
    )
    original = EnrichedManifest(initiative="multi-rt", phases=[p0, p1])
    restored = EnrichedManifest.model_validate_json(original.model_dump_json())
    assert restored.initiative == "multi-rt"
    assert len(restored.phases) == 2
    assert restored.total_issues == 2
    assert restored.estimated_waves == 2


# ---------------------------------------------------------------------------
# Empty phases
# ---------------------------------------------------------------------------


def test_empty_phases_raises_value_error() -> None:
    """Constructing with an empty phases list raises ValueError."""
    with pytest.raises((ValueError, ValidationError)):
        EnrichedManifest(initiative="empty", phases=[])


def test_empty_phases_model_validate_raises() -> None:
    """model_validate with empty phases raises ValidationError."""
    with pytest.raises(ValidationError):
        EnrichedManifest.model_validate({"initiative": "empty", "phases": []})


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


def test_missing_phases_raises_validation_error() -> None:
    """Omitting phases entirely raises ValidationError."""
    with pytest.raises(ValidationError):
        EnrichedManifest.model_validate({"initiative": "no-phases"})


def test_missing_issue_title_raises_validation_error() -> None:
    """An EnrichedIssue without a title raises ValidationError."""
    with pytest.raises(ValidationError):
        EnrichedIssue.model_validate(
            {
                "body": "body",
                "labels": [],
                "phase": "0-foundation",
                "acceptance_criteria": [],
                "tests_required": [],
                "docs_required": [],
            }
        )


def test_missing_acceptance_criteria_raises_validation_error() -> None:
    """An EnrichedIssue without acceptance_criteria raises ValidationError."""
    with pytest.raises(ValidationError):
        EnrichedIssue.model_validate(
            {
                "title": "T",
                "body": "b",
                "labels": [],
                "phase": "0-foundation",
                "tests_required": [],
                "docs_required": [],
            }
        )


# ---------------------------------------------------------------------------
# EnrichedIssue defaults
# ---------------------------------------------------------------------------


def test_enriched_issue_can_parallel_defaults_true() -> None:
    """can_parallel defaults to True when not supplied."""
    issue = _issue("Solo")
    assert issue.can_parallel is True


def test_enriched_issue_depends_on_defaults_empty() -> None:
    """depends_on defaults to an empty list when not supplied."""
    issue = _issue("Solo")
    assert issue.depends_on == []


# ---------------------------------------------------------------------------
# initiative is optional
# ---------------------------------------------------------------------------


def test_initiative_is_optional() -> None:
    """EnrichedManifest can be constructed without an initiative."""
    manifest = EnrichedManifest(phases=[_phase()])
    assert manifest.initiative is None
