from __future__ import annotations

"""Normalise logical_tier values to the 2-type coordinator/leaf model.

The 4-level tier vocabulary (executive | coordinator | engineer | reviewer)
is replaced by a simple 2-type structural distinction:

  coordinator — surveys its scope and spawns children (any depth)
  leaf        — works on a single issue or PR

Mapping applied to existing rows:
  executive  → coordinator   (CTO is a coordinator, not a special tier)
  coordinator → coordinator  (no change)
  engineer   → leaf
  reviewer   → leaf

No column added or removed — only stored string values are updated.

Revision ID: 0008
Revises: 0007
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE agent_runs SET logical_tier = 'coordinator'"
            " WHERE logical_tier = 'executive'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE agent_runs SET logical_tier = 'leaf'"
            " WHERE logical_tier IN ('engineer', 'reviewer')"
        )
    )


def downgrade() -> None:
    # Reverse mapping is lossy for the executive→coordinator case.
    # We restore 'leaf' rows based on role name heuristics; 'coordinator'
    # rows that were originally 'executive' cannot be distinguished from
    # genuine coordinator rows — they remain 'coordinator'.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE agent_runs SET logical_tier = 'engineer'"
            " WHERE logical_tier = 'leaf'"
            "   AND role NOT IN ('pr-reviewer')"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE agent_runs SET logical_tier = 'reviewer'"
            " WHERE logical_tier = 'leaf'"
            "   AND role IN ('pr-reviewer')"
        )
    )
