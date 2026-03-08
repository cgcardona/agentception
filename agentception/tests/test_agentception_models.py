from __future__ import annotations

"""Tests for agentception/models.py — VALID_ROLES taxonomy sync (issue #822)
and TaskFile / IssueSub / PRSub TOML expansion (issue #47).

Verifies that ``VALID_ROLES`` is derived from the role taxonomy, and that
TaskFile, IssueSub, and PRSub validate correctly with all TOML fields.

Run targeted:
    pytest agentception/tests/test_agentception_models.py -v
"""

from pathlib import Path

import yaml

from agentception.models import IssueSub, PRSub, TaskFile, VALID_ROLES

# Derive taxonomy path the same way models.py does — two levels up from agentception/.
# This avoids importing the private _TAXONOMY_PATH symbol while still testing
# that the path resolution logic is correct.
_HERE = Path(__file__).parent  # agentception/tests/
_TAXONOMY_PATH = _HERE.parent.parent / "scripts" / "gen_prompts" / "role-taxonomy.yaml"


def _spawnable_slugs_from_taxonomy() -> frozenset[str]:
    """Re-read the taxonomy YAML independently and return spawnable slugs.

    Used as a ground-truth reference in tests so any regression — e.g.
    someone accidentally re-introducing a hardcoded frozenset — is caught.
    """
    raw: object = yaml.safe_load(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "role-taxonomy.yaml must be a YAML mapping"
    slugs: set[str] = set()
    for level in raw.get("levels", []):
        if not isinstance(level, dict):
            continue
        for role in level.get("roles", []):
            if isinstance(role, dict) and role.get("spawnable") is True:
                slug = role.get("slug")
                if isinstance(slug, str):
                    slugs.add(slug)
    return frozenset(slugs)


def test_valid_roles_matches_taxonomy() -> None:
    """VALID_ROLES must equal the set of spawnable slugs in role-taxonomy.yaml.

    This is the regression guard for issue #822: if a new role is added to
    the taxonomy with spawnable: true, VALID_ROLES must automatically include
    it without any manual list update in models.py.
    """
    expected = _spawnable_slugs_from_taxonomy()
    assert VALID_ROLES == expected, (
        f"VALID_ROLES is out of sync with role-taxonomy.yaml.\n"
        f"  Missing from VALID_ROLES: {expected - VALID_ROLES}\n"
        f"  Extra in VALID_ROLES:     {VALID_ROLES - expected}"
    )


def test_valid_roles_taxonomy_file_exists() -> None:
    """role-taxonomy.yaml must exist at the resolved path."""
    assert _TAXONOMY_PATH.exists(), (
        f"Taxonomy file not found: {_TAXONOMY_PATH}. "
        "Did the file move? Update _TAXONOMY_PATH in models.py."
    )


def test_valid_roles_is_nonempty() -> None:
    """VALID_ROLES must contain at least the original leaf agent roles."""
    core_roles = {
        "python-developer",
        "database-architect",
        "pr-reviewer",
    }
    assert core_roles.issubset(VALID_ROLES), (
        f"Core roles missing from VALID_ROLES: {core_roles - VALID_ROLES}"
    )


def test_valid_roles_excludes_non_spawnable() -> None:
    """Orchestration roles (spawnable: false) must not appear in VALID_ROLES."""
    orchestration_roles = {"cto", "engineering-coordinator", "qa-coordinator", "ceo"}
    overlap = orchestration_roles & VALID_ROLES
    assert not overlap, (
        f"Non-spawnable orchestration roles found in VALID_ROLES: {overlap}"
    )


def test_valid_roles_contains_new_taxonomy_roles() -> None:
    """VALID_ROLES must include roles added in the extended taxonomy (issue #822)."""
    new_roles = {
        "rust-developer",
        "go-developer",
        "typescript-developer",
        "ios-developer",
        "android-developer",
        "rails-developer",
        "react-developer",
        "site-reliability-engineer",
        "ml-researcher",
        "data-scientist",
    }
    assert new_roles.issubset(VALID_ROLES), (
        f"New taxonomy roles missing from VALID_ROLES: {new_roles - VALID_ROLES}"
    )


# ── TaskFile / IssueSub / PRSub (issue #47 — TOML spec) ─────────────────────


def test_task_file_with_all_new_fields_validates() -> None:
    """TaskFile accepts all new TOML fields and validates correctly."""
    tf = TaskFile(
        task="issue-to-pr",
        id="3f4a9c2e-1b8d-4e7f-a6c5-9d2e8f0b1a3c",
        domain="engineering",
        draft_id="8b2c4d1e-9f3a-4b7e-c5d8-2e1f6a9b0c3d",
        output_path="/tmp/worktrees/plan-draft/.plan-output.yaml",
        output_format="yaml",
        depends_on=[870, 871],
        file_ownership=["agentception/routes/api/plan.py"],
        closes_issues=[872],
        files_changed=["agentception/readers/worktrees.py"],
        grade_threshold="A",
        has_migration=True,
        wave="5-plan-step-v2",
        vp_fingerprint="eng-20260303T134821Z-a7f2",
        gh_repo="cgcardona/agentception",
        issue_number=872,
        branch="feat/issue-872",
        worktree="/tmp/worktrees/issue-872",
        issue_queue=[
            IssueSub(number=870, title="MCP layer", role="python-developer", cognitive_arch="turing:python"),
            IssueSub(number=871, title="Plan tools", role="python-developer", cognitive_arch="turing:python", depends_on=[870]),
        ],
        pr_queue=[
            PRSub(number=99, title="PR for issue 872", branch="feat/issue-872", role="pr-reviewer", cognitive_arch="turing:python", grade_threshold="B", merge_order=1),
        ],
    )
    assert tf.task == "issue-to-pr"
    assert tf.id == "3f4a9c2e-1b8d-4e7f-a6c5-9d2e8f0b1a3c"
    assert tf.domain == "engineering"
    assert tf.draft_id == "8b2c4d1e-9f3a-4b7e-c5d8-2e1f6a9b0c3d"
    assert tf.output_path == "/tmp/worktrees/plan-draft/.plan-output.yaml"
    assert tf.output_format == "yaml"
    assert tf.depends_on == [870, 871]
    assert tf.file_ownership == ["agentception/routes/api/plan.py"]
    assert tf.closes_issues == [872]
    assert tf.files_changed == ["agentception/readers/worktrees.py"]
    assert tf.grade_threshold == "A"
    assert tf.has_migration is True
    assert tf.wave == "5-plan-step-v2"
    assert tf.vp_fingerprint == "eng-20260303T134821Z-a7f2"
    assert len(tf.issue_queue) == 2
    assert tf.issue_queue[0].number == 870 and tf.issue_queue[1].depends_on == [870]
    assert len(tf.pr_queue) == 1 and tf.pr_queue[0].merge_order == 1


def test_issue_sub_validates_correctly() -> None:
    """IssueSub validates with required and optional fields."""
    sub = IssueSub(
        number=41,
        title="UI: wire plan.js to /api/plan/draft",
        role="python-developer",
        cognitive_arch="turing:python",
        depends_on=[],
        file_ownership=["agentception/static/js/plan.js"],
        branch="feat/issue-41",
    )
    assert sub.number == 41
    assert sub.title == "UI: wire plan.js to /api/plan/draft"
    assert sub.role == "python-developer"
    assert sub.depends_on == []
    assert sub.file_ownership == ["agentception/static/js/plan.js"]
    assert sub.branch == "feat/issue-41"


def test_pr_sub_validates_correctly() -> None:
    """PRSub validates with required and optional fields."""
    sub = PRSub(
        number=642,
        title="Add TaskFile TOML fields",
        branch="feat/issue-47",
        role="pr-reviewer",
        cognitive_arch="turing:python",
        grade_threshold="A",
        merge_order=2,
        closes_issues=[47],
    )
    assert sub.number == 642
    assert sub.title == "Add TaskFile TOML fields"
    assert sub.grade_threshold == "A"
    assert sub.merge_order == 2
    assert sub.closes_issues == [47]


def test_task_file_tier_and_org_domain_fields() -> None:
    """TaskFile tier + org_domain fields are present and work."""
    tf = TaskFile(
        task="issue-to-pr",
        gh_repo="cgcardona/agentception",
        issue_number=47,
        pr_number=None,
        branch="feat/issue-47",
        worktree="/tmp/wt",
        role="python-developer",
        base="dev",
        batch_id="issue-47-20260305T214233Z-b46f",
        closes_issues=[],
        spawn_sub_agents=False,
        spawn_mode="chain",
        merge_after="other-branch",
        attempt_n=0,
        required_output="pr_url",
        on_block="stop",
        cognitive_arch="guido_van_rossum:postgresql:python",
        tier="engineer",
        org_domain="engineering",
        parent_run_id="coord-ac-workflow-feac3d",
    )
    assert tf.task == "issue-to-pr"
    assert tf.gh_repo == "cgcardona/agentception"
    assert tf.issue_number == 47
    assert tf.branch == "feat/issue-47"
    assert tf.role == "python-developer"
    assert tf.batch_id == "issue-47-20260305T214233Z-b46f"
    assert tf.spawn_mode == "chain"
    assert tf.merge_after == "other-branch"
    assert tf.tier == "engineer"
    assert tf.org_domain == "engineering"
    assert tf.parent_run_id == "coord-ac-workflow-feac3d"
    # New fields default as specified
    assert tf.depends_on == []
    assert tf.file_ownership == []
    assert tf.issue_queue == []
    assert tf.pr_queue == []
    assert tf.wave is None
    assert tf.domain is None
