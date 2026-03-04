from __future__ import annotations

"""ac_initiative_phases — phase dependency graph per initiative

Stores the DAG of phase dependencies that was declared in the PlanSpec
at plan-creation time.  Each row records one phase of one initiative and
which other phases it must wait for before work can begin.

The Build board reads this table to compute the ``locked`` flag for each
phase swim lane.  When no rows exist for an initiative (e.g. issues were
created before this feature shipped), every phase is shown as unlocked.

Revision ID: ac0003
Revises: ac0002
Create Date: 2026-03-04
"""

import sqlalchemy as sa
from alembic import op

revision = "ac0003"
down_revision = "ac0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ac_initiative_phases",
        sa.Column("initiative", sa.String(256), nullable=False, primary_key=True),
        sa.Column("phase_label", sa.String(256), nullable=False, primary_key=True),
        # JSON list of phase label strings this phase depends on, e.g. '["phase-0"]'
        sa.Column("depends_on_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ac_initiative_phases_initiative",
        "ac_initiative_phases",
        ["initiative"],
    )


def downgrade() -> None:
    op.drop_index("ix_ac_initiative_phases_initiative", "ac_initiative_phases")
    op.drop_table("ac_initiative_phases")
