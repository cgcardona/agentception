from __future__ import annotations

"""Add gh_repo, is_resumed, coord_fingerprint to agent_runs.

These three columns eliminate the last remaining data that was only
available inside the `.agent-task` TOML file.  With this migration every
field an agent or the poller needs is readable directly from the DB row,
making the `.agent-task` file fully redundant.

- ``gh_repo``          — GitHub repository slug (e.g. ``cgcardona/agentception``).
- ``is_resumed``       — True when this is a retry of a cancelled/stale run.
- ``coord_fingerprint``— run_id of the coordinator that spawned this run.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("gh_repo", sa.String(256), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "is_resumed",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column("coord_fingerprint", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "coord_fingerprint")
    op.drop_column("agent_runs", "is_resumed")
    op.drop_column("agent_runs", "gh_repo")
