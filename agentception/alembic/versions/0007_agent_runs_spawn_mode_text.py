from __future__ import annotations

"""Widen agent_runs.spawn_mode from VARCHAR(64) to TEXT.

The column was created as VARCHAR(64) in migration 0001 but the SQLAlchemy
model was later updated to use Text.  The mismatch caused
StringDataRightTruncationError when persist_agent_run_dispatch tried to
store a JSON blob (``{"host_worktree": "/full/host/path/..."}``) that
exceeds 64 characters, silently swallowing the error and leaving every
dispatch without a pending_launch DB record.

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_runs",
        "spawn_mode",
        existing_type=sa.String(64),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "agent_runs",
        "spawn_mode",
        existing_type=sa.Text(),
        type_=sa.String(64),
        existing_nullable=True,
    )
