from __future__ import annotations

"""Add node_type column to agent_runs; separate structural type from org domain.

Background
----------
Migration 0008 normalised ``logical_tier`` values to ``coordinator | leaf``.
This was correct structurally but collapsed two distinct concepts into one field:

  * **Structural position** — is this node a coordinator (spawns children) or a
    leaf (works one issue/PR)?  Now stored in the new ``node_type`` column.

  * **Organisational domain** — which logical branch of the org tree does this
    node belong to (``engineering``, ``qa``, ``c-suite``, …)?  This is what
    ``logical_tier`` was *always meant* to capture.  The column is kept; its
    values are cleared for legacy rows because the domain cannot be inferred
    from the stored ``coordinator | leaf`` strings.

Post-migration state
--------------------
  node_type     — ``coordinator`` | ``leaf`` | NULL (legacy rows pre-0006)
  logical_tier  — free org-domain string (``qa``, ``engineering``, ``c-suite``,
                  …) written by the spawning agent; NULL for all existing rows
                  (recoverable only from role-file knowledge, not DB data)

New runs written after this migration will have both fields populated.

Revision ID: 0009
Revises: 0008
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the new structural column (nullable — pre-0006 rows have no value).
    op.add_column(
        "agent_runs",
        sa.Column("node_type", sa.String(64), nullable=True),
    )
    op.create_index("ix_agent_runs_node_type", "agent_runs", ["node_type"])

    conn = op.get_bind()

    # Copy the coordinator/leaf values written by migration 0008 into node_type.
    conn.execute(
        sa.text(
            "UPDATE agent_runs SET node_type = logical_tier"
            " WHERE logical_tier IN ('coordinator', 'leaf')"
        )
    )

    # Clear logical_tier — those coordinator/leaf values described structure,
    # not org domain.  We cannot recover the original org domain from DB data.
    conn.execute(sa.text("UPDATE agent_runs SET logical_tier = NULL"))


def downgrade() -> None:
    # Restore logical_tier from node_type (loses any org-domain data written
    # by new code, but those rows are edge-case; structural values are correct).
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE agent_runs SET logical_tier = node_type"
            " WHERE node_type IS NOT NULL"
        )
    )

    op.drop_index("ix_agent_runs_node_type", table_name="agent_runs")
    op.drop_column("agent_runs", "node_type")
