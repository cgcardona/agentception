from __future__ import annotations

"""Add workflow state machine tables and PR columns.

New tables:
- ``pr_issue_links`` — explicit, auditable PR↔Issue linkage with provenance.
- ``issue_workflow_state`` — canonical, persisted swim-lane state per issue.

New columns on ``pull_requests``:
- ``base_ref`` — target branch for base-mismatch detection.
- ``is_draft`` — GitHub draft PR flag.
- ``closes_issue_numbers_json`` — array of all closing references.
- ``body_hash`` — SHA-256 of normalised body text.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New columns on pull_requests ──────────────────────────────────────
    op.add_column(
        "pull_requests",
        sa.Column("base_ref", sa.String(256), nullable=True),
    )
    op.add_column(
        "pull_requests",
        sa.Column("is_draft", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "pull_requests",
        sa.Column(
            "closes_issue_numbers_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "pull_requests",
        sa.Column("body_hash", sa.String(64), nullable=True),
    )

    # ── pr_issue_links ────────────────────────────────────────────────────
    op.create_table(
        "pr_issue_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("link_method", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "repo", "pr_number", "issue_number", "link_method",
            name="uq_pr_issue_links",
        ),
    )
    op.create_index("ix_pr_issue_links_issue", "pr_issue_links", ["repo", "issue_number"])
    op.create_index("ix_pr_issue_links_pr", "pr_issue_links", ["repo", "pr_number"])

    # ── issue_workflow_state ──────────────────────────────────────────────
    op.create_table(
        "issue_workflow_state",
        sa.Column("repo", sa.String(256), nullable=False, primary_key=True),
        sa.Column("issue_number", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("initiative", sa.String(256), nullable=True),
        sa.Column("phase_key", sa.String(256), nullable=True),
        sa.Column("lane", sa.String(32), nullable=False, server_default="todo"),
        sa.Column("issue_state", sa.String(32), nullable=False, server_default="open"),
        sa.Column("run_id", sa.String(512), nullable=True),
        sa.Column("agent_status", sa.String(64), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_state", sa.String(32), nullable=True),
        sa.Column("pr_base", sa.String(256), nullable=True),
        sa.Column("pr_head_ref", sa.String(256), nullable=True),
        sa.Column("pr_link_method", sa.String(64), nullable=True),
        sa.Column("pr_link_confidence", sa.Integer(), nullable=True),
        sa.Column("warnings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_issue_workflow_state_lane", "issue_workflow_state", ["lane"])
    op.create_index("ix_issue_workflow_state_initiative", "issue_workflow_state", ["initiative"])
    op.create_index("ix_issue_workflow_state_phase", "issue_workflow_state", ["phase_key"])


def downgrade() -> None:
    op.drop_table("issue_workflow_state")
    op.drop_table("pr_issue_links")
    op.drop_column("pull_requests", "body_hash")
    op.drop_column("pull_requests", "closes_issue_numbers_json")
    op.drop_column("pull_requests", "is_draft")
    op.drop_column("pull_requests", "base_ref")
