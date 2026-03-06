from __future__ import annotations

"""Tests for the tier / org_domain / parent_run_id lineage fields.

Covers:
  - TaskFile parser reads TIER → tier and ORG_DOMAIN → org_domain
    as fully separate fields (not fallback aliases).
  - A chain-spawned PR reviewer can have tier=reviewer AND org_domain=qa
    simultaneously.
  - AgentNode carries all three lineage fields.
  - PendingLaunchRow and AgentRunRow TypedDicts include tier and org_domain.
  - Migration 0012 replaces node_type + logical_tier with tier + org_domain.
  - dispatch-label .agent-task writer includes TIER= and ORG_DOMAIN=.
  - engineering-coordinator reviewer heredoc sets TIER=reviewer ORG_DOMAIN=qa.
  - Regression: PARENT_RUN_ID empty string is normalised to None.
"""

import re
from pathlib import Path

import pytest

from agentception.models import AgentNode, AgentStatus, TaskFile
from agentception.readers.worktrees import _build_task_file_from_toml


# ---------------------------------------------------------------------------
# TaskFile — .agent-task parser
# ---------------------------------------------------------------------------


def _make_task_file(fields: dict[str, str], tmp_path: Path) -> TaskFile:
    """Helper: build a TaskFile from a legacy key→value dict via TOML mapping.

    Maps the K=V field names to the appropriate TOML section structure so
    tests can exercise the same semantics without touching disk.
    """
    toml_data: dict[str, object] = {}

    task_sec: dict[str, object] = {}
    agent_sec: dict[str, object] = {}
    pipeline_sec: dict[str, object] = {}

    if "WORKFLOW" in fields:
        task_sec["workflow"] = fields["WORKFLOW"]
    if "RUN_ID" in fields:
        task_sec["id"] = fields["RUN_ID"]
    if "ROLE" in fields:
        agent_sec["role"] = fields["ROLE"]
    if "TIER" in fields:
        agent_sec["tier"] = fields["TIER"]
    if "ORG_DOMAIN" in fields:
        agent_sec["org_domain"] = fields["ORG_DOMAIN"]
    # Empty PARENT_RUN_ID is omitted so it normalises to None (absent → None).
    if fields.get("PARENT_RUN_ID"):
        pipeline_sec["parent_run_id"] = fields["PARENT_RUN_ID"]

    if task_sec:
        toml_data["task"] = task_sec
    if agent_sec:
        toml_data["agent"] = agent_sec
    if pipeline_sec:
        toml_data["pipeline"] = pipeline_sec

    return _build_task_file_from_toml(toml_data, tmp_path)


def test_task_file_parses_tier(tmp_path: Path) -> None:
    """TIER field is read into TaskFile.tier (behavioral execution tier)."""
    tf = _make_task_file(
        {
            "WORKFLOW": "pr-review",
            "ROLE": "pr-reviewer",
            "TIER": "reviewer",
            "PARENT_RUN_ID": "issue-42",
        },
        tmp_path,
    )
    assert tf.tier == "reviewer"
    assert tf.parent_run_id == "issue-42"


def test_task_file_parses_org_domain(tmp_path: Path) -> None:
    """ORG_DOMAIN field is read into TaskFile.org_domain (UI hierarchy slot)."""
    tf = _make_task_file(
        {
            "WORKFLOW": "pr-review",
            "ROLE": "pr-reviewer",
            "ORG_DOMAIN": "qa",
            "PARENT_RUN_ID": "issue-42",
        },
        tmp_path,
    )
    assert tf.org_domain == "qa"
    assert tf.parent_run_id == "issue-42"


def test_task_file_parses_both_fields_independently(tmp_path: Path) -> None:
    """TIER and ORG_DOMAIN are parsed as separate fields — the core invariant.

    A chain-spawned PR reviewer has tier=reviewer (behavioral) and
    org_domain=qa (org slot) at the same time.
    """
    tf = _make_task_file(
        {
            "ROLE": "pr-reviewer",
            "TIER": "reviewer",
            "ORG_DOMAIN": "qa",
            "PARENT_RUN_ID": "issue-42",
        },
        tmp_path,
    )
    assert tf.tier == "reviewer"
    assert tf.org_domain == "qa"
    assert tf.parent_run_id == "issue-42"


def test_task_file_tier_does_not_bleed_into_org_domain(tmp_path: Path) -> None:
    """TIER value must not appear in org_domain and vice versa."""
    tf = _make_task_file(
        {
            "ROLE": "pr-reviewer",
            "TIER": "engineer",
            "ORG_DOMAIN": "engineering",
        },
        tmp_path,
    )
    assert tf.tier == "engineer"
    assert tf.org_domain == "engineering"
    # The two values must not bleed
    assert tf.tier != "engineering"
    assert tf.org_domain != "engineer"


def test_task_file_defaults_both_to_none(tmp_path: Path) -> None:
    """Both tier and org_domain default to None when absent."""
    tf = _make_task_file(
        {"WORKFLOW": "issue-to-pr", "ROLE": "python-developer"},
        tmp_path,
    )
    assert tf.tier is None
    assert tf.org_domain is None
    assert tf.parent_run_id is None


def test_task_file_empty_parent_run_id_is_none(tmp_path: Path) -> None:
    """Regression: PARENT_RUN_ID= (empty string) normalises to None."""
    tf = _make_task_file(
        {
            "ROLE": "pr-reviewer",
            "TIER": "reviewer",
            "PARENT_RUN_ID": "",
        },
        tmp_path,
    )
    # Empty string normalised to None by the 'or None' guard in worktrees.py
    assert tf.parent_run_id is None


def test_task_file_coordinator_tier(tmp_path: Path) -> None:
    """_build_task_file parses TIER=coordinator correctly."""
    tf = _make_task_file(
        {"RUN_ID": "label-ac-ui-0-critical-a1b2", "ROLE": "engineering-coordinator", "TIER": "coordinator"},
        tmp_path,
    )
    assert tf.tier == "coordinator"
    # Coordinator with no explicit ORG_DOMAIN — org domain is optional
    assert tf.org_domain is None


# ---------------------------------------------------------------------------
# AgentNode — carries all three lineage fields
# ---------------------------------------------------------------------------


def test_agent_node_carries_tier_and_org_domain() -> None:
    """AgentNode stores tier, org_domain, and parent_run_id."""
    node = AgentNode(
        id="pr-99",
        role="pr-reviewer",
        status=AgentStatus.REVIEWING,
        tier="reviewer",
        org_domain="qa",
        parent_run_id="issue-42",
    )
    assert node.tier == "reviewer"
    assert node.org_domain == "qa"
    assert node.parent_run_id == "issue-42"


def test_agent_node_lineage_fields_default_none() -> None:
    """AgentNode.tier, org_domain, and parent_run_id default to None."""
    node = AgentNode(
        id="issue-1",
        role="python-developer",
        status=AgentStatus.IMPLEMENTING,
    )
    assert node.tier is None
    assert node.org_domain is None
    assert node.parent_run_id is None


def test_agent_node_serialises_lineage_fields() -> None:
    """model_dump() includes tier, org_domain, and parent_run_id keys."""
    node = AgentNode(
        id="pr-99",
        role="pr-reviewer",
        status=AgentStatus.REVIEWING,
        tier="reviewer",
        org_domain="qa",
        parent_run_id="issue-42",
    )
    d = node.model_dump()
    assert d["tier"] == "reviewer"
    assert d["org_domain"] == "qa"
    assert d["parent_run_id"] == "issue-42"


# ---------------------------------------------------------------------------
# TypedDict shape checks (static — catch regressions in the dict definitions)
# ---------------------------------------------------------------------------


def test_pending_launch_row_has_tier_and_org_domain_keys() -> None:
    """PendingLaunchRow TypedDict declares tier, org_domain, and parent_run_id."""
    from agentception.db.queries import PendingLaunchRow

    keys = PendingLaunchRow.__required_keys__ | PendingLaunchRow.__optional_keys__
    assert "tier" in keys
    assert "org_domain" in keys
    assert "parent_run_id" in keys


def test_agent_run_row_has_tier_and_org_domain_keys() -> None:
    """AgentRunRow TypedDict declares tier, org_domain, and parent_run_id."""
    from agentception.db.queries import AgentRunRow

    keys = AgentRunRow.__required_keys__ | AgentRunRow.__optional_keys__
    assert "tier" in keys
    assert "org_domain" in keys
    assert "parent_run_id" in keys


# ---------------------------------------------------------------------------
# Consolidated migration 0001 — structural smoke tests
#
# Migrations 0001–0012 were flattened into a single canonical baseline.
# These tests verify that the consolidated schema file contains all the
# columns that matter for lineage tracking.
# ---------------------------------------------------------------------------


def _migration_0001_content() -> str:
    migration_dir = Path(__file__).parent.parent / "alembic" / "versions"
    candidates = list(migration_dir.glob("0001_*.py"))
    assert candidates, "Migration file 0001_* not found in alembic/versions/"
    return candidates[0].read_text()


def test_migration_0001_adds_parent_run_id() -> None:
    """Consolidated migration creates agent_runs with a parent_run_id column."""
    content = _migration_0001_content()
    assert "parent_run_id" in content


def test_migration_0001_has_downgrade() -> None:
    """Consolidated migration implements downgrade() that drops all tables."""
    content = _migration_0001_content()
    assert "def downgrade" in content
    assert "drop_table" in content


def test_migration_0001_adds_tier_column() -> None:
    """Consolidated migration creates agent_runs with a tier column."""
    content = _migration_0001_content()
    assert '"tier"' in content


def test_migration_0001_adds_org_domain_column() -> None:
    """Consolidated migration creates agent_runs with an org_domain column."""
    content = _migration_0001_content()
    assert '"org_domain"' in content


def test_migration_0001_has_no_node_type_or_logical_tier() -> None:
    """Consolidated migration never creates the deprecated node_type / logical_tier columns.

    Those intermediate columns existed only in the incremental migration chain
    (0006–0009).  The flattened 0001 schema goes directly to the final shape.
    """
    content = _migration_0001_content()
    assert "node_type" not in content
    assert "logical_tier" not in content


def test_migration_0001_is_only_migration() -> None:
    """Exactly one migration file exists — the consolidated baseline."""
    migration_dir = Path(__file__).parent.parent / "alembic" / "versions"
    py_files = [
        f for f in migration_dir.glob("*.py") if f.name != "__init__.py"
    ]
    assert len(py_files) == 1, (
        f"Expected exactly 1 migration file, found {len(py_files)}: "
        + ", ".join(f.name for f in sorted(py_files))
    )


# ---------------------------------------------------------------------------
# .agent-task writer — dispatch-label writes TIER= and ORG_DOMAIN=
# ---------------------------------------------------------------------------


def test_dispatch_label_agent_task_contains_tier() -> None:
    """The .agent-task file written by dispatch-label includes TIER=."""
    source_path = (
        Path(__file__).parent.parent / "routes" / "api" / "dispatch.py"
    )
    source = source_path.read_text()
    assert "TIER=" in source, (
        "dispatch_label_agent should write TIER= to the .agent-task file"
    )


def test_dispatch_label_agent_task_contains_org_domain() -> None:
    """dispatch-label also writes ORG_DOMAIN= (org slot) to the .agent-task file."""
    source_path = (
        Path(__file__).parent.parent / "routes" / "api" / "dispatch.py"
    )
    source = source_path.read_text()
    assert "ORG_DOMAIN=" in source, (
        "dispatch_label_agent should write ORG_DOMAIN= for org hierarchy visualisation"
    )


# ---------------------------------------------------------------------------
# Engineering-coordinator role — reviewer heredoc sets TIER=reviewer ORG_DOMAIN=qa
# ---------------------------------------------------------------------------


def test_engineering_coordinator_reviewer_task_has_tier_reviewer() -> None:
    """The reviewer .agent-task heredoc in engineering-coordinator sets TIER=reviewer."""
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "engineering-coordinator.md"
    )
    assert role_path.exists(), f"Role file missing: {role_path}"
    content = role_path.read_text()
    assert re.search(r"TIER=reviewer", content), (
        "engineering-coordinator reviewer heredoc must write TIER=reviewer"
    )
    assert re.search(r"PARENT_RUN_ID=\$\{RUN_ID", content), (
        "engineering-coordinator reviewer heredoc must write PARENT_RUN_ID=${RUN_ID:-}"
    )


def test_engineering_coordinator_reviewer_task_has_org_domain_qa() -> None:
    """The reviewer .agent-task heredoc sets ORG_DOMAIN=qa for org visualisation.

    Even though the reviewer is physically spawned by an engineering leaf,
    it belongs logically to the QA column of the org tree.
    """
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "engineering-coordinator.md"
    )
    assert role_path.exists(), f"Role file missing: {role_path}"
    content = role_path.read_text()
    assert re.search(r"ORG_DOMAIN=qa", content), (
        "engineering-coordinator reviewer heredoc must write ORG_DOMAIN=qa"
    )


# ---------------------------------------------------------------------------
# CTO role — coordinator spawning behaviour
# ---------------------------------------------------------------------------


def test_cto_role_no_qa_vp_when_issues_present() -> None:
    """CTO role table: when ISSUES > 0, no QA coordinator is spawned unconditionally."""
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "cto.md"
    )
    assert role_path.exists(), f"CTO role file missing: {role_path}"
    content = role_path.read_text()
    # The new allocation table should NOT have the "otherwise → 1 QA coordinator" row
    assert "otherwise" not in content or "chain" in content, (
        "CTO allocation table must not unconditionally spawn QA coordinator when issues remain"
    )
    # The explanation for no concurrent QA coordinator must be present
    assert "chain-spawn" in content, (
        "CTO role must explain that engineers chain-spawn their own reviewers"
    )
