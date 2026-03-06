from __future__ import annotations

"""Tests for the tier / org_domain / parent_run_id lineage fields.

Covers:
  - TaskFile parser reads tier, org_domain, parent_run_id from TOML sections.
  - A chain-spawned PR reviewer can have tier=reviewer AND org_domain=qa
    simultaneously.
  - AgentNode carries all three lineage fields.
  - PendingLaunchRow and AgentRunRow TypedDicts include tier and org_domain.
  - Migration 0012 replaces node_type + logical_tier with tier + org_domain.
  - dispatch-label .agent-task writer includes TIER= and ORG_DOMAIN=.
  - engineering-coordinator reviewer heredoc sets TIER=reviewer ORG_DOMAIN=qa.
"""

import re
from pathlib import Path

import pytest

from agentception.models import AgentNode, AgentStatus, TaskFile
from agentception.readers.worktrees import parse_agent_task


# ---------------------------------------------------------------------------
# TaskFile — TOML v2 .agent-task parser
# ---------------------------------------------------------------------------


def _toml_task(
    *,
    role: str | None = None,
    tier: str | None = None,
    org_domain: str | None = None,
    parent_run_id: str | None = None,
    workflow: str = "issue-to-pr",
) -> str:
    """Render a minimal TOML .agent-task string with only the given lineage fields."""
    lines: list[str] = [f'[task]\nworkflow = "{workflow}"\n']
    agent_fields: list[str] = []
    if role:
        agent_fields.append(f'role = "{role}"')
    if tier:
        agent_fields.append(f'tier = "{tier}"')
    if org_domain:
        agent_fields.append(f'org_domain = "{org_domain}"')
    if agent_fields:
        lines.append("[agent]\n" + "\n".join(agent_fields))
    pipeline_fields: list[str] = []
    if parent_run_id is not None:
        pipeline_fields.append(f'parent_run_id = "{parent_run_id}"')
    if pipeline_fields:
        lines.append("[pipeline]\n" + "\n".join(pipeline_fields))
    return "\n\n".join(lines) + "\n"


@pytest.mark.anyio
async def test_task_file_parses_tier(tmp_path: Path) -> None:
    """[agent].tier is read into TaskFile.tier (behavioral execution tier)."""
    (tmp_path / ".agent-task").write_text(
        _toml_task(workflow="pr-review", role="pr-reviewer", tier="reviewer", parent_run_id="issue-42")
    )
    tf = await parse_agent_task(tmp_path)
    assert tf is not None
    assert tf.tier == "reviewer"
    assert tf.parent_run_id == "issue-42"


@pytest.mark.anyio
async def test_task_file_parses_org_domain(tmp_path: Path) -> None:
    """[agent].org_domain is read into TaskFile.org_domain (UI hierarchy slot)."""
    (tmp_path / ".agent-task").write_text(
        _toml_task(workflow="pr-review", role="pr-reviewer", org_domain="qa", parent_run_id="issue-42")
    )
    tf = await parse_agent_task(tmp_path)
    assert tf is not None
    assert tf.org_domain == "qa"
    assert tf.parent_run_id == "issue-42"


@pytest.mark.anyio
async def test_task_file_parses_both_fields_independently(tmp_path: Path) -> None:
    """tier and org_domain are parsed as separate fields — the core invariant.

    A chain-spawned PR reviewer has tier=reviewer (behavioral) and
    org_domain=qa (org slot) at the same time.
    """
    (tmp_path / ".agent-task").write_text(
        _toml_task(role="pr-reviewer", tier="reviewer", org_domain="qa", parent_run_id="issue-42")
    )
    tf = await parse_agent_task(tmp_path)
    assert tf is not None
    assert tf.tier == "reviewer"
    assert tf.org_domain == "qa"
    assert tf.parent_run_id == "issue-42"


@pytest.mark.anyio
async def test_task_file_tier_does_not_bleed_into_org_domain(tmp_path: Path) -> None:
    """TIER value must not appear in org_domain and vice versa."""
    (tmp_path / ".agent-task").write_text(
        _toml_task(role="pr-reviewer", tier="engineer", org_domain="engineering")
    )
    tf = await parse_agent_task(tmp_path)
    assert tf is not None
    assert tf.tier == "engineer"
    assert tf.org_domain == "engineering"
    assert tf.tier != "engineering"
    assert tf.org_domain != "engineer"


@pytest.mark.anyio
async def test_task_file_defaults_both_to_none(tmp_path: Path) -> None:
    """Both tier and org_domain default to None when absent."""
    (tmp_path / ".agent-task").write_text(
        _toml_task(workflow="issue-to-pr", role="python-developer")
    )
    tf = await parse_agent_task(tmp_path)
    assert tf is not None
    assert tf.tier is None
    assert tf.org_domain is None
    assert tf.parent_run_id is None


@pytest.mark.anyio
async def test_task_file_coordinator_tier(tmp_path: Path) -> None:
    """[agent].tier = "coordinator" is parsed correctly."""
    (tmp_path / ".agent-task").write_text(
        _toml_task(role="engineering-coordinator", tier="coordinator")
    )
    tf = await parse_agent_task(tmp_path)
    assert tf is not None
    assert tf.tier == "coordinator"
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


def test_migration_chain_is_contiguous() -> None:
    """Migration files form a contiguous 0001→0002 chain."""
    migration_dir = Path(__file__).parent.parent / "alembic" / "versions"
    py_files = sorted(
        f for f in migration_dir.glob("*.py") if f.name != "__init__.py"
    )
    assert len(py_files) >= 2, (
        f"Expected at least 2 migration files, found {len(py_files)}: "
        + ", ".join(f.name for f in py_files)
    )
    assert py_files[0].name.startswith("0001_"), f"First migration should be 0001, got {py_files[0].name}"
    assert py_files[1].name.startswith("0002_"), f"Second migration should be 0002, got {py_files[1].name}"


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
    assert "otherwise" not in content or "chain" in content, (
        "CTO allocation table must not unconditionally spawn QA coordinator when issues remain"
    )
    assert "chain-spawn" in content, (
        "CTO role must explain that engineers chain-spawn their own reviewers"
    )
