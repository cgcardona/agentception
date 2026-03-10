from __future__ import annotations

"""Add execution_plans table for the planner / executor architecture.

Stores the immutable :class:`ExecutionPlan` JSON produced by the planner
agent before the executor agent starts.  One row per agent run that goes
through the two-stage pipeline.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_plans",
        sa.Column("run_id", sa.String(512), primary_key=True),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_execution_plans_issue_number",
        "execution_plans",
        ["issue_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_plans_issue_number", table_name="execution_plans")
    op.drop_table("execution_plans")
