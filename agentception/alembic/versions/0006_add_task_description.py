from __future__ import annotations

"""Add task_description column to agent_runs.

Stores the inline task description for ad-hoc agent runs that are spawned
directly via POST /api/runs/adhoc rather than through a GitHub issue.  When
present, the agent loop uses this field as the initial message instead of
directing the agent via the DB row, eliminating any file
as a mandatory indirection layer.

Revision ID: 0006
Revises: 0005
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
        sa.Column("task_description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "task_description")
