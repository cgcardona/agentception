from __future__ import annotations

"""Tests for the logical_tier / parent_run_id lineage fields added in migration 0006.

Covers:
  - TaskFile parser picks up LOGICAL_TIER and PARENT_RUN_ID from .agent-task content.
  - AgentNode carries logical_tier and parent_run_id through from TaskFile.
  - PendingLaunchRow TypedDict includes both fields.
  - AgentRunRow TypedDict includes both fields.
  - Migration file 0006 exists and references the expected columns.
  - dispatch-label .agent-task writer includes LOGICAL_TIER.
  - Regression: PARENT_RUN_ID empty string is normalised to None in the task parser.
"""

import re
import textwrap
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


def test_task_file_parses_logical_tier(tmp_path: Path) -> None:
    """_build_task_file extracts LOGICAL_TIER into TaskFile.logical_tier."""
    tf = _make_task_file(
        {
            "WORKFLOW": "pr-review",
            "ROLE": "pr-reviewer",
            "LOGICAL_TIER": "reviewer",
            "PARENT_RUN_ID": "issue-42",
        },
        tmp_path,
    )
    assert tf.logical_tier == "reviewer"
    assert tf.parent_run_id == "issue-42"


def test_task_file_logical_tier_defaults_none(tmp_path: Path) -> None:
    """_build_task_file leaves logical_tier None when LOGICAL_TIER is absent."""
    tf = _make_task_file(
        {"WORKFLOW": "issue-to-pr", "ROLE": "python-developer"},
        tmp_path,
    )
    assert tf.logical_tier is None
    assert tf.parent_run_id is None


def test_task_file_empty_parent_run_id_is_none(tmp_path: Path) -> None:
    """Regression: PARENT_RUN_ID= (empty string) normalises to None."""
    tf = _make_task_file(
        {
            "WORKFLOW": "pr-review",
            "ROLE": "pr-reviewer",
            "LOGICAL_TIER": "reviewer",
            "PARENT_RUN_ID": "",
        },
        tmp_path,
    )
    # Empty string normalised to None by the 'or None' guard in worktrees.py
    assert tf.parent_run_id is None


def test_task_file_executive_tier(tmp_path: Path) -> None:
    """_build_task_file handles LOGICAL_TIER=executive correctly."""
    tf = _make_task_file(
        {"RUN_ID": "label-ac-ui-0-critical-a1b2", "ROLE": "cto", "LOGICAL_TIER": "executive"},
        tmp_path,
    )
    assert tf.logical_tier == "executive"


# ---------------------------------------------------------------------------
# AgentNode — carries lineage fields through from TaskFile
# ---------------------------------------------------------------------------


def test_agent_node_carries_logical_tier() -> None:
    """AgentNode stores logical_tier and parent_run_id passed at construction."""
    node = AgentNode(
        id="pr-99",
        role="pr-reviewer",
        status=AgentStatus.REVIEWING,
        logical_tier="reviewer",
        parent_run_id="issue-42",
    )
    assert node.logical_tier == "reviewer"
    assert node.parent_run_id == "issue-42"


def test_agent_node_lineage_fields_default_none() -> None:
    """AgentNode.logical_tier and parent_run_id default to None (backward-compat)."""
    node = AgentNode(
        id="issue-1",
        role="python-developer",
        status=AgentStatus.IMPLEMENTING,
    )
    assert node.logical_tier is None
    assert node.parent_run_id is None


def test_agent_node_serialises_lineage_fields() -> None:
    """model_dump() includes logical_tier and parent_run_id keys."""
    node = AgentNode(
        id="pr-99",
        role="pr-reviewer",
        status=AgentStatus.REVIEWING,
        logical_tier="reviewer",
        parent_run_id="issue-42",
    )
    d = node.model_dump()
    assert d["logical_tier"] == "reviewer"
    assert d["parent_run_id"] == "issue-42"


# ---------------------------------------------------------------------------
# TypedDict shape checks (static — catch regressions in the dict definitions)
# ---------------------------------------------------------------------------


def test_pending_launch_row_has_lineage_keys() -> None:
    """PendingLaunchRow TypedDict declares logical_tier and parent_run_id."""
    from agentception.db.queries import PendingLaunchRow

    keys = PendingLaunchRow.__required_keys__ | PendingLaunchRow.__optional_keys__
    assert "logical_tier" in keys
    assert "parent_run_id" in keys


def test_agent_run_row_has_lineage_keys() -> None:
    """AgentRunRow TypedDict declares logical_tier and parent_run_id."""
    from agentception.db.queries import AgentRunRow

    keys = AgentRunRow.__required_keys__ | AgentRunRow.__optional_keys__
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
# .agent-task writer — LOGICAL_TIER in dispatch-label output
# ---------------------------------------------------------------------------


def test_dispatch_label_agent_task_contains_logical_tier() -> None:
    """The .agent-task file written by dispatch-label includes LOGICAL_TIER=<tier>."""
    # Verify the source code constructs the LOGICAL_TIER line.
    source_path = (
        Path(__file__).parent.parent / "routes" / "api" / "build.py"
    )
    source = source_path.read_text()
    assert "LOGICAL_TIER=" in source, (
        "dispatch_label_agent should write LOGICAL_TIER to the .agent-task file"
    )


# ---------------------------------------------------------------------------
# Engineering-coordinator role — reviewer .agent-task includes LOGICAL_TIER
# ---------------------------------------------------------------------------


def test_engineering_coordinator_reviewer_task_has_logical_tier() -> None:
    """The reviewer .agent-task written by engineering-coordinator STEP 6 sets LOGICAL_TIER=reviewer."""
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "engineering-coordinator.md"
    )
    assert role_path.exists(), f"Role file missing: {role_path}"
    content = role_path.read_text()
    # LOGICAL_TIER=reviewer must appear inside the heredoc for the reviewer .agent-task
    assert re.search(r"LOGICAL_TIER=reviewer", content), (
        "engineering-coordinator STEP 6 must write LOGICAL_TIER=reviewer to reviewer .agent-task"
    )
    assert re.search(r"PARENT_RUN_ID=\$\{RUN_ID", content), (
        "engineering-coordinator STEP 6 must write PARENT_RUN_ID=${RUN_ID:-} to reviewer .agent-task"
    )


# ---------------------------------------------------------------------------
# CTO role — QA VP not spawned when ISSUES > 0
# ---------------------------------------------------------------------------


def test_cto_role_no_qa_vp_when_issues_present() -> None:
    """CTO role table: when ISSUES > 0, no QA VP is spawned (engineers chain-spawn reviewers)."""
    role_path = (
        Path(__file__).parent.parent.parent
        / ".agentception" / "roles" / "cto.md"
    )
    assert role_path.exists(), f"CTO role file missing: {role_path}"
    content = role_path.read_text()
    # The new allocation table should NOT have the "otherwise → 1 QA VP" row
    assert "otherwise" not in content or "chain" in content, (
        "CTO allocation table must not unconditionally spawn QA VP when issues remain"
    )
    # The explanation for no concurrent QA VP must be present
    assert "chain-spawn" in content, (
        "CTO role must explain that engineers chain-spawn their own reviewers"
    )
