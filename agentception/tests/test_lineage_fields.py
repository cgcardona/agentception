from __future__ import annotations

"""Tests for the tier / org_domain / parent_run_id lineage fields.

Covers:
  - AgentNode carries all three lineage fields.
  - PendingLaunchRow and AgentRunRow TypedDicts include tier and org_domain.
  - Consolidated migration has the correct lineage columns.
  - dispatch.py includes TIER and ORG_DOMAIN fields.
"""

import re
from pathlib import Path

from agentception.models import AgentNode, AgentStatus


# ---------------------------------------------------------------------------
# AgentNode — carries all three lineage fields
# ---------------------------------------------------------------------------


def test_agent_node_carries_tier_and_org_domain() -> None:
    """AgentNode stores tier, org_domain, and parent_run_id."""
    node = AgentNode(
        id="pr-99",
        role="pr-reviewer",
        status=AgentStatus.REVIEWING,
        tier="worker",
        org_domain="qa",
        parent_run_id="issue-42",
    )
    assert node.tier == "worker"
    assert node.org_domain == "qa"
    assert node.parent_run_id == "issue-42"


def test_agent_node_lineage_fields_default_none() -> None:
    """AgentNode.tier, org_domain, and parent_run_id default to None."""
    node = AgentNode(
        id="issue-1",
        role="developer",
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
        tier="worker",
        org_domain="qa",
        parent_run_id="issue-42",
    )
    d = node.model_dump()
    assert d["tier"] == "worker"
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
# Dispatch — persists TIER and ORG_DOMAIN to the DB row
# ---------------------------------------------------------------------------


def test_dispatch_persists_tier() -> None:
    """dispatch.py persists the agent tier to the DB row."""
    source_path = (
        Path(__file__).parent.parent / "routes" / "api" / "dispatch.py"
    )
    source = source_path.read_text()
    assert '"tier"' in source or "'tier'" in source or "tier" in source, (
        "dispatch should persist agent tier to the DB row"
    )


def test_dispatch_persists_org_domain() -> None:
    """dispatch.py persists org_domain to the DB row for org hierarchy."""
    source_path = (
        Path(__file__).parent.parent / "routes" / "api" / "dispatch.py"
    )
    source = source_path.read_text()
    assert "org_domain" in source, (
        "dispatch should persist org_domain to the DB row for org hierarchy"
    )


# ---------------------------------------------------------------------------
# Engineering-coordinator role — MCP-native dispatch shape
# ---------------------------------------------------------------------------


def test_engineering_coordinator_uses_build_spawn_adhoc_child() -> None:
    """The engineering-coordinator role uses the MCP-native spawn tool, not hardcoded roles."""
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "engineering-coordinator.md"
    )
    assert role_path.exists(), f"Role file missing: {role_path}"
    content = role_path.read_text()
    assert re.search(r"build_spawn_adhoc_child", content), (
        "engineering-coordinator must use build_spawn_adhoc_child for dynamic role dispatch"
    )


def test_engineering_coordinator_uses_query_run_status() -> None:
    """The engineering-coordinator role uses query_run_status to poll child runs."""
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "engineering-coordinator.md"
    )
    assert role_path.exists(), f"Role file missing: {role_path}"
    content = role_path.read_text()
    assert re.search(r"query_run_status", content), (
        "engineering-coordinator must use query_run_status to poll child run completion"
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
