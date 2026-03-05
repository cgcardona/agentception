from __future__ import annotations

"""Tests for the cognitive architecture resolution system.

Covers:
- ROLE_DEFAULT_FIGURE completeness (all 45 roles map to a loadable figure)
- _resolve_cognitive_arch with skills_hint, embedded skills, and keyword fallback
- _extract_skills_from_body parses embedded HTML comments correctly
- _derive_skills_from_body keyword matching
- PlanIssue.skills field validation
- issue_creator._embed_skills embeds skills correctly
"""

import pytest

from agentception.models import PlanIssue
from agentception.readers.issue_creator import _embed_skills
from agentception.routes.api._shared import (
    ROLE_DEFAULT_FIGURE,
    _derive_skills_from_body,
    _extract_skills_from_body,
    _resolve_cognitive_arch,
)

# ---------------------------------------------------------------------------
# All role slugs defined in .agentception/roles/
# ---------------------------------------------------------------------------

_ALL_ROLE_SLUGS = {
    "python-developer",
    "frontend-developer",
    "full-stack-developer",
    "typescript-developer",
    "api-developer",
    "database-architect",
    "data-engineer",
    "devops-engineer",
    "site-reliability-engineer",
    "security-engineer",
    "test-engineer",
    "architect",
    "ml-engineer",
    "ml-researcher",
    "data-scientist",
    "systems-programmer",
    "rust-developer",
    "go-developer",
    "react-developer",
    "ios-developer",
    "android-developer",
    "mobile-developer",
    "rails-developer",
    "technical-writer",
    "muse-specialist",
    "engineering-coordinator",
    "qa-coordinator",
    "coordinator",
    "pr-reviewer",
    "cto",
    "ceo",
    "cpo",
    "coo",
    "cdo",
    "cfo",
    "ciso",
    "cmo",
    "vp-platform",
    "vp-infrastructure",
    "vp-data",
    "vp-ml",
    "vp-design",
    "vp-mobile",
    "vp-security",
    "vp-product",
}


# ---------------------------------------------------------------------------
# ROLE_DEFAULT_FIGURE — completeness
# ---------------------------------------------------------------------------


def test_role_default_figure_covers_all_roles() -> None:
    """Every role slug must map to a figure (or fall back to 'hopper')."""
    for role in _ALL_ROLE_SLUGS:
        figure = ROLE_DEFAULT_FIGURE.get(role, "hopper")
        assert figure, f"Role {role!r} resolved to an empty figure string"


def test_role_default_figure_no_placeholder_strings() -> None:
    """Old placeholder strings 'coordinator' and 'conductor' must not appear as values."""
    bad_values = {"coordinator", "conductor"}
    for role, figure in ROLE_DEFAULT_FIGURE.items():
        assert figure not in bad_values, (
            f"Role {role!r} still maps to placeholder figure {figure!r}. "
            "All roles must map to real, loadable figure IDs."
        )


def test_qa_coordinator_uses_deming() -> None:
    assert ROLE_DEFAULT_FIGURE["qa-coordinator"] == "w_edwards_deming"


def test_pr_reviewer_uses_fagan() -> None:
    assert ROLE_DEFAULT_FIGURE["pr-reviewer"] == "michael_fagan"


def test_test_engineer_uses_kent_beck() -> None:
    assert ROLE_DEFAULT_FIGURE["test-engineer"] == "kent_beck"


def test_cto_uses_jeff_dean() -> None:
    assert ROLE_DEFAULT_FIGURE["cto"] == "jeff_dean"


def test_unknown_role_falls_back_to_hopper() -> None:
    result = _resolve_cognitive_arch("some issue body", "nonexistent-role")
    assert result.startswith("hopper:")


# ---------------------------------------------------------------------------
# _resolve_cognitive_arch — skills_hint
# ---------------------------------------------------------------------------


def test_resolve_skills_hint_overrides_keyword_extraction() -> None:
    """Explicit skills_hint must win over keyword extraction from the body."""
    body = "Fix the postgres migration and alembic schema"
    result = _resolve_cognitive_arch(body, "python-developer", skills_hint=["htmx", "jinja2"])
    assert result == "guido_van_rossum:htmx:jinja2"


def test_resolve_skills_hint_single_skill() -> None:
    result = _resolve_cognitive_arch("anything", "frontend-developer", skills_hint=["alpine"])
    assert result == "don_norman:alpine"


def test_resolve_figure_always_from_role_not_body() -> None:
    """Figure must come from ROLE_DEFAULT_FIGURE, not from issue body keywords."""
    body = "overview dashboard pipeline tree"  # would have triggered lovelace in old code
    result = _resolve_cognitive_arch(body, "python-developer")
    figure, _skills = result.split(":", 1)
    assert figure == "guido_van_rossum", (
        f"Figure should come from role map (guido_van_rossum), got {figure!r}"
    )


def test_resolve_coordinator_gets_real_figure() -> None:
    result = _resolve_cognitive_arch("", "coordinator")
    assert not result.startswith("coordinator:"), (
        "coordinator role must resolve to a real figure, not the placeholder 'coordinator'"
    )


def test_resolve_engineering_coordinator_gets_von_neumann() -> None:
    result = _resolve_cognitive_arch("", "engineering-coordinator")
    assert result.startswith("von_neumann:")


# ---------------------------------------------------------------------------
# _extract_skills_from_body — embedded HTML comment
# ---------------------------------------------------------------------------


def test_extract_skills_from_comment() -> None:
    body = "Some issue body\n\n<!-- ac:skills: python, fastapi, postgresql -->"
    result = _extract_skills_from_body(body)
    assert result == ["python", "fastapi", "postgresql"]


def test_extract_skills_single_skill() -> None:
    body = "Body text<!-- ac:skills: htmx -->"
    result = _extract_skills_from_body(body)
    assert result == ["htmx"]


def test_extract_skills_missing_comment_returns_none() -> None:
    body = "No skills comment here."
    result = _extract_skills_from_body(body)
    assert result is None


def test_extract_skills_used_in_resolve() -> None:
    """_resolve_cognitive_arch auto-extracts embedded skills when no hint given."""
    body = "Body\n\n<!-- ac:skills: htmx, jinja2, alpine -->"
    result = _resolve_cognitive_arch(body, "frontend-developer")
    assert result == "don_norman:htmx:jinja2:alpine"


# ---------------------------------------------------------------------------
# _derive_skills_from_body — keyword fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,expected", [
    ("fix the htmx polling and hx-ext issues", "htmx"),
    ("migrate the postgres schema with alembic", "postgresql:python"),
    ("add sse async broadcast fanout endpoint", "python"),
    ("update dockerfile FROM python compose service", "devops"),
    ("refactor midi muse variation generator", "midi:python"),
    ("integrate llm openrouter claude embedding", "llm:python"),
    ("add fastapi route with response_model", "fastapi:python"),
    ("fix undefined typescript tsx type", "typescript:javascript"),
    ("no special keywords here", "python"),
])
def test_derive_skills_from_body(body: str, expected: str) -> None:
    result = _derive_skills_from_body(body)
    assert result == expected, f"Body {body!r}: expected {expected!r}, got {result!r}"


def test_derive_skills_htmx_with_jinja2_and_alpine() -> None:
    body = "htmx hx- partial with jinja2 template and x-data alpine"
    result = _derive_skills_from_body(body)
    assert result == "htmx:jinja2:alpine"


# ---------------------------------------------------------------------------
# _embed_skills — issue creator helper
# ---------------------------------------------------------------------------


def test_embed_skills_appends_comment() -> None:
    body = "## Context\nSome body text."
    result = _embed_skills(body, ["python", "fastapi"])
    assert result.endswith("<!-- ac:skills: python, fastapi -->")
    assert "## Context" in result


def test_embed_skills_empty_list_returns_body_unchanged() -> None:
    body = "Body text."
    result = _embed_skills(body, [])
    assert result == body


def test_embed_skills_roundtrip_with_extract() -> None:
    """Skills embedded by _embed_skills must be extractable by _extract_skills_from_body."""
    skills = ["htmx", "jinja2", "alpine"]
    body = _embed_skills("Issue body.", skills)
    extracted = _extract_skills_from_body(body)
    assert extracted == skills


# ---------------------------------------------------------------------------
# PlanIssue.skills field
# ---------------------------------------------------------------------------


def test_plan_issue_skills_defaults_to_empty_list() -> None:
    issue = PlanIssue(id="p0-001", title="Fix something", body="Body text.")
    assert issue.skills == []


def test_plan_issue_skills_accepts_list() -> None:
    issue = PlanIssue(
        id="p0-002",
        title="Add HTMX polling",
        body="Body text.",
        skills=["htmx", "jinja2"],
    )
    assert issue.skills == ["htmx", "jinja2"]


def test_plan_issue_skills_flows_to_embed() -> None:
    """Verify the full pipeline: PlanIssue.skills → _embed_skills → _extract_skills_from_body."""
    issue = PlanIssue(
        id="p0-003",
        title="Enrich build board",
        body="## Context\nFix the board.",
        skills=["python", "postgresql"],
    )
    enriched_body = _embed_skills(issue.body, issue.skills)
    extracted = _extract_skills_from_body(enriched_body)
    assert extracted == ["python", "postgresql"]
