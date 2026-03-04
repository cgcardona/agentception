"""add ac_task_runs table

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ac0004"
down_revision = "ac0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ac_task_runs",
        sa.Column("id", sa.String(256), primary_key=True),
        sa.Column("task_type", sa.String(128), nullable=False),
        sa.Column("branch", sa.String(256), nullable=True),
        sa.Column("commit_sha", sa.String(40), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ac_task_runs_task_type", "ac_task_runs", ["task_type"])
    op.create_index("ix_ac_task_runs_status", "ac_task_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ac_task_runs_status", table_name="ac_task_runs")
    op.drop_index("ix_ac_task_runs_task_type", table_name="ac_task_runs")
    op.drop_table("ac_task_runs")
