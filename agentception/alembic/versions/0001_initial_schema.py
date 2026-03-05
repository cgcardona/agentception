from __future__ import annotations

"""ac initial schema

THIS IS THE ONLY MIGRATION for AgentCeption during the monorepo phase.
New ac_* tables are added here directly (upgrade/downgrade pair).
When AgentCeption is extracted to its own repo, this becomes its migration 0001.

Revision ID: ac0001
Revises:
Create Date: 2026-03-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── waves ─────────────────────────────────────────────────────────────
    op.create_table(
        "waves",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("phase_label", sa.String(256), nullable=False),
        sa.Column("role", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("spawn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skip_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # ── agent_runs ─────────────────────────────────────────────────────────
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("wave_id", sa.String(128), sa.ForeignKey("waves.id"), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("branch", sa.String(256), nullable=True),
        sa.Column("worktree_path", sa.String(512), nullable=True),
        sa.Column("role", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spawn_mode", sa.String(64), nullable=True),
        sa.Column("batch_id", sa.String(128), nullable=True),
        sa.Column("spawned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_runs_wave_id", "agent_runs", ["wave_id"])
    op.create_index("ix_agent_runs_issue_number", "agent_runs", ["issue_number"])
    op.create_index("ix_agent_runs_pr_number", "agent_runs", ["pr_number"])
    op.create_index("ix_agent_runs_batch_id", "agent_runs", ["batch_id"])

    # ── issues ─────────────────────────────────────────────────────────────
    op.create_table(
        "issues",
        sa.Column("github_number", sa.Integer(), primary_key=True),
        sa.Column("repo", sa.String(256), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("phase_label", sa.String(256), nullable=True),
        sa.Column("labels_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_issues_state", "issues", ["state"])
    op.create_index("ix_issues_phase_label", "issues", ["phase_label"])

    # ── pull_requests ──────────────────────────────────────────────────────
    op.create_table(
        "pull_requests",
        sa.Column("github_number", sa.Integer(), primary_key=True),
        sa.Column("repo", sa.String(256), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("head_ref", sa.String(256), nullable=True),
        sa.Column("closes_issue_number", sa.Integer(), nullable=True),
        sa.Column("labels_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pull_requests_state", "pull_requests", ["state"])

    # ── agent_messages ─────────────────────────────────────────────────────
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_run_id",
            sa.String(512),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(256), nullable=True),
        sa.Column("sequence_index", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_messages_run_seq",
        "agent_messages",
        ["agent_run_id", "sequence_index"],
    )

    # ── role_versions ──────────────────────────────────────────────────────
    op.create_table(
        "role_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("role_name", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("role_name", "content_hash", name="uq_role_versions"),
    )
    op.create_index("ix_role_versions_role_name", "role_versions", ["role_name"])

    # ── pipeline_snapshots ─────────────────────────────────────────────────
    op.create_table(
        "pipeline_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_label", sa.String(256), nullable=True),
        sa.Column("issues_open", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prs_open", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agents_active", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alerts_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index(
        "ix_pipeline_snapshots_polled_at", "pipeline_snapshots", ["polled_at"]
    )


def downgrade() -> None:
    op.drop_table("pipeline_snapshots")
    op.drop_table("role_versions")
    op.drop_table("agent_messages")
    op.drop_table("pull_requests")
    op.drop_table("issues")
    op.drop_table("agent_runs")
    op.drop_table("waves")
