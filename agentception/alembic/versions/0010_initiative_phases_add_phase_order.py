from __future__ import annotations

"""Add phase_order column to initiative_phases.

``phase_order`` (INTEGER NOT NULL DEFAULT 0) records the 0-indexed display
position of each phase within its initiative.  It is written by
``persist_initiative_phases`` whenever ``file_issues`` completes, and read by
``get_initiative_phase_meta`` so the Build board can display phases in the
exact order the plan declared them — without inferring order from label strings.

For pre-existing rows (initiatives filed before this migration) the column
defaults to 0.  Those initiatives will fall through to the lexicographic-sort
fallback path in ``get_issues_grouped_by_phase``, which produces the correct
order as long as labels follow the ``{N}-{slug}`` convention.  No data
migration is required.

Revision ID: 0010
Revises: 0009
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "initiative_phases",
        sa.Column("phase_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_initiative_phases_phase_order",
        "initiative_phases",
        ["initiative", "phase_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_initiative_phases_phase_order", table_name="initiative_phases")
    op.drop_column("initiative_phases", "phase_order")
