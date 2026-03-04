"""Tests for PlanSpec, PlanPhase, and PlanIssue Pydantic models (AC-867).

These tests verify:
- Round-trip fidelity: PlanSpec → YAML → PlanSpec produces identical models.
- Cyclic / forward phase dependency detection raises ValueError.
- Missing required fields raise ValidationError.
- from_yaml() rejects malformed YAML strings.
- Empty phases / empty issues lists are rejected.
"""
from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from agentception.models import PlanIssue, PlanPhase, PlanSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_issue(id: str = "test-p0-001") -> PlanIssue:
    return PlanIssue(id=id, title="Do the thing", body="## Context\nDo it well.")


def _minimal_phase(
    label: str = "0-foundation",
    depends_on: list[str] | None = None,
    id_suffix: str = "001",
) -> PlanPhase:
    return PlanPhase(
        label=label,
        description="Foundation work",
        depends_on=depends_on or [],
        issues=[_minimal_issue(f"test-p0-{id_suffix}")],
    )


def _minimal_spec() -> PlanSpec:
    return PlanSpec(initiative="test-initiative", phases=[_minimal_phase()])


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


def test_round_trip_single_phase() -> None:
    """PlanSpec → YAML → PlanSpec reproduces an identical model."""
    original = _minimal_spec()
    yaml_text = original.to_yaml()
    restored = PlanSpec.from_yaml(yaml_text)
    assert restored.initiative == original.initiative
    assert len(restored.phases) == len(original.phases)
    restored_phase = restored.phases[0]
    original_phase = original.phases[0]
    assert restored_phase.label == original_phase.label
    assert restored_phase.description == original_phase.description
    assert restored_phase.depends_on == original_phase.depends_on
    assert len(restored_phase.issues) == len(original_phase.issues)
    assert restored_phase.issues[0].title == original_phase.issues[0].title
    assert restored_phase.issues[0].body == original_phase.issues[0].body


def test_round_trip_multi_phase_with_deps() -> None:
    """Multi-phase PlanSpec with dependencies round-trips cleanly."""
    spec = PlanSpec(
        initiative="auth-rewrite",
        phases=[
            PlanPhase(
                label="0-foundation",
                description="Core types",
                depends_on=[],
                issues=[
                    PlanIssue(
                        id="auth-rewrite-p0-001",
                        title="Define AuthToken model",
                        body="Token model body.",
                    ),
                    PlanIssue(
                        id="auth-rewrite-p0-002",
                        title="Add JWT validation",
                        body="JWT body.",
                        depends_on=["auth-rewrite-p0-001"],
                    ),
                ],
            ),
            PlanPhase(
                label="1-api",
                description="API endpoints",
                depends_on=["0-foundation"],
                issues=[
                    PlanIssue(
                        id="auth-rewrite-p1-001",
                        title="POST /auth/login endpoint",
                        body="Login endpoint body.",
                    ),
                ],
            ),
        ],
    )
    restored = PlanSpec.from_yaml(spec.to_yaml())
    assert restored == spec


def test_round_trip_preserves_unicode() -> None:
    """Unicode characters in title/body survive the YAML round-trip."""
    spec = PlanSpec(
        initiative="unicode-test",
        phases=[
            PlanPhase(
                label="0-base",
                description="Résumé des tâches — 日本語",
                depends_on=[],
                issues=[
                    PlanIssue(id="unicode-test-p0-001", title="Titre: résumé", body="Corps: 日本語テスト 🎵"),
                ],
            )
        ],
    )
    restored = PlanSpec.from_yaml(spec.to_yaml())
    assert restored.phases[0].description == "Résumé des tâches — 日本語"
    assert restored.phases[0].issues[0].body == "Corps: 日本語テスト 🎵"


def test_to_yaml_produces_clean_output() -> None:
    """to_yaml() output does not contain Pydantic internal field names."""
    yaml_text = _minimal_spec().to_yaml()
    assert "model_fields" not in yaml_text
    assert "model_config" not in yaml_text
    assert "__fields__" not in yaml_text
    assert "initiative:" in yaml_text
    assert "phases:" in yaml_text


# ---------------------------------------------------------------------------
# DAG / dependency validation
# ---------------------------------------------------------------------------


def test_forward_reference_raises_value_error() -> None:
    """A phase that depends_on a label not yet seen raises ValueError."""
    with pytest.raises(ValueError, match="forward reference or cycle"):
        PlanSpec(
            initiative="bad-deps",
            phases=[
                PlanPhase(
                    label="0-foundation",
                    description="Phase A",
                    depends_on=["1-api"],  # forward reference — 1-api hasn't been defined yet
                    issues=[_minimal_issue("bad-deps-p0-001")],
                ),
                PlanPhase(
                    label="1-api",
                    description="Phase B",
                    depends_on=[],
                    issues=[_minimal_issue("bad-deps-p1-001")],
                ),
            ],
        )


def test_self_reference_raises_value_error() -> None:
    """A phase that lists its own label in depends_on raises ValueError."""
    with pytest.raises(ValueError, match="forward reference or cycle"):
        PlanSpec(
            initiative="self-dep",
            phases=[
                PlanPhase(
                    label="0-foundation",
                    description="Phase A",
                    depends_on=["0-foundation"],  # self-reference
                    issues=[_minimal_issue()],
                ),
            ],
        )


def test_nonexistent_dep_label_raises_value_error() -> None:
    """Depending on a completely unknown label raises ValueError."""
    with pytest.raises(ValueError, match="forward reference or cycle"):
        PlanSpec(
            initiative="phantom-dep",
            phases=[
                PlanPhase(
                    label="0-foundation",
                    description="Phase A",
                    depends_on=["ghost-phase"],
                    issues=[_minimal_issue()],
                ),
            ],
        )


def test_valid_dag_with_multiple_deps() -> None:
    """A phase may depend on multiple earlier phases without error."""
    spec = PlanSpec(
        initiative="multi-dep",
        phases=[
            _minimal_phase("0-a"),
            _minimal_phase("0-b", id_suffix="002"),
            PlanPhase(
                label="1-combined",
                description="Depends on both",
                depends_on=["0-a", "0-b"],
                issues=[_minimal_issue("test-p1-001")],
            ),
        ],
    )
    assert len(spec.phases) == 3


# ---------------------------------------------------------------------------
# Required field validation
# ---------------------------------------------------------------------------


def test_missing_initiative_raises_validation_error() -> None:
    """Omitting initiative raises ValidationError."""
    with pytest.raises(ValidationError):
        PlanSpec.model_validate({"phases": [{"label": "x", "description": "d", "issues": [{"title": "t", "body": "b"}]}]})


def test_missing_phases_raises_validation_error() -> None:
    """Omitting phases raises ValidationError."""
    with pytest.raises(ValidationError):
        PlanSpec.model_validate({"initiative": "x"})


def test_missing_phase_label_raises_validation_error() -> None:
    """Omitting phase label raises ValidationError."""
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(
            {
                "initiative": "x",
                "phases": [{"description": "d", "issues": [{"title": "t", "body": "b"}]}],
            }
        )


def test_missing_issue_title_raises_validation_error() -> None:
    """Omitting issue title raises ValidationError."""
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(
            {
                "initiative": "x",
                "phases": [{"label": "0", "description": "d", "issues": [{"body": "b"}]}],
            }
        )


def test_missing_issue_body_raises_validation_error() -> None:
    """Omitting issue body raises ValidationError."""
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(
            {
                "initiative": "x",
                "phases": [{"label": "0", "description": "d", "issues": [{"title": "t"}]}],
            }
        )


# ---------------------------------------------------------------------------
# Empty collection validation
# ---------------------------------------------------------------------------


def test_empty_phases_raises_value_error() -> None:
    """An empty phases list raises ValueError."""
    with pytest.raises((ValueError, ValidationError)):
        PlanSpec(initiative="empty", phases=[])


def test_empty_issues_raises_value_error() -> None:
    """A phase with an empty issues list raises ValueError."""
    with pytest.raises((ValueError, ValidationError)):
        PlanPhase(label="0-empty", description="No issues", issues=[])


# ---------------------------------------------------------------------------
# from_yaml() rejection of malformed input
# ---------------------------------------------------------------------------


def test_from_yaml_rejects_invalid_yaml_syntax() -> None:
    """from_yaml raises ValueError on YAML syntax errors."""
    bad_yaml = "initiative: test\nphases: [\n  unclosed"
    with pytest.raises(ValueError, match="Malformed YAML"):
        PlanSpec.from_yaml(bad_yaml)


def test_from_yaml_rejects_non_mapping_root() -> None:
    """from_yaml raises ValueError when root is a list, not a mapping."""
    with pytest.raises(ValueError, match="Expected a YAML mapping"):
        PlanSpec.from_yaml("- one\n- two\n")


def test_from_yaml_rejects_missing_required_fields() -> None:
    """from_yaml raises ValueError when required fields are absent."""
    with pytest.raises(ValueError, match="PlanSpec validation failed"):
        PlanSpec.from_yaml("initiative: orphan\n")


def test_from_yaml_rejects_empty_string() -> None:
    """from_yaml raises ValueError on an empty string (parses to None)."""
    with pytest.raises(ValueError):
        PlanSpec.from_yaml("")


def test_from_yaml_rejects_plain_scalar() -> None:
    """from_yaml raises ValueError when YAML root is a plain scalar."""
    with pytest.raises(ValueError, match="Expected a YAML mapping"):
        PlanSpec.from_yaml("just a string")


def test_from_yaml_accepts_valid_yaml() -> None:
    """from_yaml successfully parses a well-formed YAML document."""
    yaml_text = textwrap.dedent(
        """\
        initiative: smoke-test
        phases:
          - label: 0-foundation
            description: Foundation phase
            depends_on: []
            issues:
              - id: smoke-test-p0-001
                title: Bootstrap the repo
                body: |
                  Set up the initial project structure.
                depends_on: []
        """
    )
    spec = PlanSpec.from_yaml(yaml_text)
    assert spec.initiative == "smoke-test"
    assert spec.phases[0].label == "0-foundation"
    assert spec.phases[0].issues[0].title == "Bootstrap the repo"
