from __future__ import annotations

"""Add prompt_variant column to agent_runs for A/B prompt testing."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("prompt_variant", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "prompt_variant")
