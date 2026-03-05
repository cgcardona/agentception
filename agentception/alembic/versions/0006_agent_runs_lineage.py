from __future__ import annotations

"""Add logical_tier and parent_run_id to agent_runs.

logical_tier  — organisational tier the run reports to in the virtual org chart
                (executive | coordinator | engineer | reviewer).  Null for legacy rows.
parent_run_id — run_id of the agent that physically spawned this one.
                Null for top-level dispatches and legacy rows.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("logical_tier", sa.String(64), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("parent_run_id", sa.String(512), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_logical_tier",
        "agent_runs",
        ["logical_tier"],
    )
    op.create_index(
        "ix_agent_runs_parent_run_id",
        "agent_runs",
        ["parent_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_parent_run_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_logical_tier", table_name="agent_runs")
    op.drop_column("agent_runs", "parent_run_id")
    op.drop_column("agent_runs", "logical_tier")
