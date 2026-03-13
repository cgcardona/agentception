from __future__ import annotations

"""Formalise run state machine — rename status values.

Backfills legacy status strings to the new canonical values:

- ``done``    → ``completed``  (clean exit with PR)
- ``unknown`` → ``failed``     (unclean exit or TTL expiry)
- ``stale``   → ``implementing`` (stale is now computed on-demand from
                                   last_activity_at, not stored)

New valid status values introduced by this migration:
- ``blocked``   — agent explicitly blocked (build_block_run MCP)
- ``stopped``   — operator stopped the run (build_stop_run MCP)
- ``failed``    — was ``unknown``; unclean exit or abandoned run
- ``completed`` — was ``done``; clean exit with PR

Values that are unchanged (already exist and remain valid):
- ``pending_launch``
- ``implementing``
- ``reviewing``
- ``cancelled``

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-06
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE agent_runs SET status = 'completed'    WHERE status = 'done'")
    op.execute("UPDATE agent_runs SET status = 'failed'       WHERE status = 'unknown'")
    op.execute("UPDATE agent_runs SET status = 'implementing' WHERE status = 'stale'")


def downgrade() -> None:
    op.execute("UPDATE agent_runs SET status = 'done'    WHERE status = 'completed'")
    op.execute("UPDATE agent_runs SET status = 'unknown' WHERE status = 'failed'")
    # 'blocked' and 'stopped' have no prior equivalent — map to 'unknown' on downgrade
    op.execute("UPDATE agent_runs SET status = 'unknown' WHERE status IN ('blocked', 'stopped')")
