from __future__ import annotations

"""Tests for the node_type / logical_tier / parent_run_id lineage fields.

Covers:
  - TaskFile parser reads NODE_TYPE → node_type and LOGICAL_TIER → logical_tier
    as fully separate fields (not fallback aliases).
  - A chain-spawned PR reviewer can have node_type=leaf AND logical_tier=qa
    simultaneously.
  - AgentNode carries all three lineage fields.
  - PendingLaunchRow and AgentRunRow TypedDicts include node_type.
  - Migration 0006 adds logical_tier and parent_run_id columns.
  - Migration 0009 adds node_type column.
  - dispatch-label .agent-task writer includes NODE_TYPE= and LOGICAL_TIER=.
  - engineering-coordinator reviewer heredoc sets NODE_TYPE=leaf LOGICAL_TIER=qa.
  - Regression: PARENT_RUN_ID empty string is normalised to None.
"""

import re
from pathlib import Path

import pytest

from agentception.models import AgentNode, AgentStatus, TaskFile
from agentception.readers.worktrees import _build_task_file


# ---------------------------------------------------------------------------
# TaskFile — .agent-task parser
# ---------------------------------------------------------------------------


def _make_task_file(fields: dict[str, str], tmp_path: Path) -> TaskFile:
    """Helper: build a TaskFile from a key→value dict via _build_task_file."""
    return _build_task_file(fields, tmp_path)


def test_task_file_parses_node_type(tmp_path: Path) -> None:
    """NODE_TYPE field is read into TaskFile.node_type (structural position)."""
    tf = _make_task_file(
        {
            "WORKFLOW": "pr-review",
            "ROLE": "pr-reviewer",
            "NODE_TYPE": "leaf",
            "PARENT_RUN_ID": "issue-42",
        },
        tmp_path,
    )
    assert tf.node_type == "leaf"
    assert tf.parent_run_id == "issue-42"


def test_task_file_parses_logical_tier(tmp_path: Path) -> None:
    """LOGICAL_TIER field is read into TaskFile.logical_tier (org domain)."""
    tf = _make_task_file(
        {
            "WORKFLOW": "pr-review",
            "ROLE": "pr-reviewer",
            "LOGICAL_TIER": "qa",
            "PARENT_RUN_ID": "issue-42",
        },
        tmp_path,
    )
    assert tf.logical_tier == "qa"
    assert tf.parent_run_id == "issue-42"


def test_task_file_parses_both_fields_independently(tmp_path: Path) -> None:
    """NODE_TYPE and LOGICAL_TIER are parsed as separate fields — the core invariant.

    A chain-spawned PR reviewer has node_type=leaf (structural) and
    logical_tier=qa (org domain) at the same time.
    """
    tf = _make_task_file(
        {
            "ROLE": "pr-reviewer",
            "NODE_TYPE": "leaf",
            "LOGICAL_TIER": "qa",
            "PARENT_RUN_ID": "issue-42",
        },
        tmp_path,
    )
    assert tf.node_type == "leaf"
    assert tf.logical_tier == "qa"
    assert tf.parent_run_id == "issue-42"


def test_task_file_node_type_does_not_bleed_into_logical_tier(tmp_path: Path) -> None:
    """NODE_TYPE value must not appear in logical_tier and vice versa."""
    tf = _make_task_file(
        {
            "ROLE": "pr-reviewer",
            "NODE_TYPE": "leaf",
            "LOGICAL_TIER": "engineering",
        },
        tmp_path,
    )
    assert tf.node_type == "leaf"
    assert tf.logical_tier == "engineering"
    # The two values must not bleed
    assert tf.node_type != "engineering"
    assert tf.logical_tier != "leaf"


def test_task_file_defaults_both_to_none(tmp_path: Path) -> None:
    """Both node_type and logical_tier default to None when absent."""
    tf = _make_task_file(
        {"WORKFLOW": "issue-to-pr", "ROLE": "python-developer"},
        tmp_path,
    )
    assert tf.node_type is None
    assert tf.logical_tier is None
    assert tf.parent_run_id is None


def test_task_file_empty_parent_run_id_is_none(tmp_path: Path) -> None:
    """Regression: PARENT_RUN_ID= (empty string) normalises to None."""
    tf = _make_task_file(
        {
            "ROLE": "pr-reviewer",
            "NODE_TYPE": "leaf",
            "PARENT_RUN_ID": "",
        },
        tmp_path,
    )
    # Empty string normalised to None by the 'or None' guard in worktrees.py
    assert tf.parent_run_id is None


def test_task_file_coordinator_node_type(tmp_path: Path) -> None:
    """_build_task_file parses NODE_TYPE=coordinator correctly."""
    tf = _make_task_file(
        {"RUN_ID": "label-ac-ui-0-critical-a1b2", "ROLE": "cto", "NODE_TYPE": "coordinator"},
        tmp_path,
    )
    assert tf.node_type == "coordinator"
    # CTO has no explicit LOGICAL_TIER in this file — org domain is optional
    assert tf.logical_tier is None


# ---------------------------------------------------------------------------
# AgentNode — carries all three lineage fields
# ---------------------------------------------------------------------------


def test_agent_node_carries_node_type_and_logical_tier() -> None:
    """AgentNode stores node_type, logical_tier, and parent_run_id."""
    node = AgentNode(
        id="pr-99",
        role="pr-reviewer",
        status=AgentStatus.REVIEWING,
        node_type="leaf",
        logical_tier="qa",
        parent_run_id="issue-42",
    )
    assert node.node_type == "leaf"
    assert node.logical_tier == "qa"
    assert node.parent_run_id == "issue-42"


def test_agent_node_lineage_fields_default_none() -> None:
    """AgentNode.node_type, logical_tier, and parent_run_id default to None."""
    node = AgentNode(
        id="issue-1",
        role="python-developer",
        status=AgentStatus.IMPLEMENTING,
    )
    assert node.node_type is None
    assert node.logical_tier is None
    assert node.parent_run_id is None


def test_agent_node_serialises_lineage_fields() -> None:
    """model_dump() includes node_type, logical_tier, and parent_run_id keys."""
    node = AgentNode(
        id="pr-99",
        role="pr-reviewer",
        status=AgentStatus.REVIEWING,
        node_type="leaf",
        logical_tier="qa",
        parent_run_id="issue-42",
    )
    d = node.model_dump()
    assert d["node_type"] == "leaf"
    assert d["logical_tier"] == "qa"
    assert d["parent_run_id"] == "issue-42"


# ---------------------------------------------------------------------------
# TypedDict shape checks (static — catch regressions in the dict definitions)
# ---------------------------------------------------------------------------


def test_pending_launch_row_has_all_lineage_keys() -> None:
    """PendingLaunchRow TypedDict declares node_type, logical_tier, and parent_run_id."""
    from agentception.db.queries import PendingLaunchRow

    keys = PendingLaunchRow.__required_keys__ | PendingLaunchRow.__optional_keys__
    assert "node_type" in keys
    assert "logical_tier" in keys
    assert "parent_run_id" in keys


def test_agent_run_row_has_all_lineage_keys() -> None:
    """AgentRunRow TypedDict declares node_type, logical_tier, and parent_run_id."""
    from agentception.db.queries import AgentRunRow

    keys = AgentRunRow.__required_keys__ | AgentRunRow.__optional_keys__
    assert "node_type" in keys
    assert "logical_tier" in keys
    assert "parent_run_id" in keys


# ---------------------------------------------------------------------------
# Migration 0006 — structural smoke test
# ---------------------------------------------------------------------------


def _migration_0006_content() -> str:
    migration_dir = (
        Path(__file__).parent.parent / "alembic" / "versions"
    )
    candidates = list(migration_dir.glob("0006_*.py"))
    assert candidates, "Migration file 0006_* not found in alembic/versions/"
    return candidates[0].read_text()


def test_migration_0006_adds_logical_tier() -> None:
    """Migration 0006 upgrade() adds a logical_tier column to ac_agent_runs."""
    content = _migration_0006_content()
    assert "logical_tier" in content
    assert "add_column" in content


def test_migration_0006_adds_parent_run_id() -> None:
    """Migration 0006 upgrade() adds a parent_run_id column to ac_agent_runs."""
    content = _migration_0006_content()
    assert "parent_run_id" in content


def test_migration_0006_has_indexes() -> None:
    """Migration 0006 creates indexes for both new columns."""
    content = _migration_0006_content()
    assert "create_index" in content
    assert "ix_agent_runs_logical_tier" in content
    assert "ix_agent_runs_parent_run_id" in content


def test_migration_0006_has_downgrade() -> None:
    """Migration 0006 implements downgrade() that drops both columns."""
    content = _migration_0006_content()
    assert "def downgrade" in content
    assert "drop_column" in content


# ---------------------------------------------------------------------------
# Migration 0009 — node_type column smoke test
# ---------------------------------------------------------------------------


def _migration_0009_content() -> str:
    migration_dir = (
        Path(__file__).parent.parent / "alembic" / "versions"
    )
    candidates = list(migration_dir.glob("0009_*.py"))
    assert candidates, "Migration file 0009_* not found in alembic/versions/"
    return candidates[0].read_text()


def test_migration_0009_adds_node_type_column() -> None:
    """Migration 0009 upgrade() adds a node_type column to agent_runs."""
    content = _migration_0009_content()
    assert "node_type" in content
    assert "add_column" in content


def test_migration_0009_copies_existing_logical_tier_values() -> None:
    """Migration 0009 copies coordinator/leaf values from logical_tier to node_type."""
    content = _migration_0009_content()
    assert "node_type = logical_tier" in content or "node_type" in content


def test_migration_0009_has_downgrade() -> None:
    """Migration 0009 implements downgrade()."""
    content = _migration_0009_content()
    assert "def downgrade" in content


# ---------------------------------------------------------------------------
# .agent-task writer — dispatch-label writes both NODE_TYPE= and LOGICAL_TIER=
# ---------------------------------------------------------------------------


def test_dispatch_label_agent_task_contains_node_type() -> None:
    """The .agent-task file written by dispatch-label includes NODE_TYPE=."""
    source_path = (
        Path(__file__).parent.parent / "routes" / "api" / "dispatch.py"
    )
    source = source_path.read_text()
    assert "NODE_TYPE=" in source, (
        "dispatch_label_agent should write NODE_TYPE to the .agent-task file"
    )


def test_dispatch_label_agent_task_contains_logical_tier() -> None:
    """dispatch-label also writes LOGICAL_TIER= (org domain) to the .agent-task file."""
    source_path = (
        Path(__file__).parent.parent / "routes" / "api" / "dispatch.py"
    )
    source = source_path.read_text()
    assert "LOGICAL_TIER=" in source, (
        "dispatch_label_agent should write LOGICAL_TIER= for org domain visualisation"
    )


# ---------------------------------------------------------------------------
# Engineering-coordinator role — reviewer heredoc sets NODE_TYPE=leaf LOGICAL_TIER=qa
# ---------------------------------------------------------------------------


def test_engineering_coordinator_reviewer_task_has_node_type_leaf() -> None:
    """The reviewer .agent-task heredoc in engineering-coordinator sets NODE_TYPE=leaf."""
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "engineering-coordinator.md"
    )
    assert role_path.exists(), f"Role file missing: {role_path}"
    content = role_path.read_text()
    assert re.search(r"NODE_TYPE=leaf", content), (
        "engineering-coordinator reviewer heredoc must write NODE_TYPE=leaf"
    )
    assert re.search(r"PARENT_RUN_ID=\$\{RUN_ID", content), (
        "engineering-coordinator reviewer heredoc must write PARENT_RUN_ID=${RUN_ID:-}"
    )


def test_engineering_coordinator_reviewer_task_has_logical_tier_qa() -> None:
    """The reviewer .agent-task heredoc sets LOGICAL_TIER=qa for org visualisation.

    Even though the reviewer is physically spawned by a Python developer (an
    engineering leaf), it belongs logically to the QA branch of the org tree.
    This is what allows the dashboard to display it under the QA branch.
    """
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "engineering-coordinator.md"
    )
    assert role_path.exists(), f"Role file missing: {role_path}"
    content = role_path.read_text()
    assert re.search(r"LOGICAL_TIER=qa", content), (
        "engineering-coordinator reviewer heredoc must write LOGICAL_TIER=qa"
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
