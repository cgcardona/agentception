from __future__ import annotations

"""add cognitive_arch column to ac_agent_runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-04
"""

import sqlalchemy as sa
from alembic import op

revision = "ac0005"
down_revision = "ac0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ac_agent_runs",
        sa.Column("cognitive_arch", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ac_agent_runs", "cognitive_arch")
