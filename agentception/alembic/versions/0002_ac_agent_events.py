from __future__ import annotations

"""ac_agent_events — structured MCP callback events per agent run

Stores events that agents push via the build_report_* MCP tools.
Each row is one deliberate signal from a running agent (step start,
blocker, decision, or completion) — distinct from the raw thinking
stream captured in ac_agent_messages.

Revision ID: ac0002
Revises: ac0001
Create Date: 2026-03-03
"""

import sqlalchemy as sa
from alembic import op

revision = "ac0002"
down_revision = "ac0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ac_agent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_run_id",
            sa.String(512),
            sa.ForeignKey("ac_agent_runs.id"),
            nullable=True,
        ),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        # step_start | blocker | decision | done
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        # JSON — varies by event_type
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_index("ix_ac_agent_events_run", "ac_agent_events", ["agent_run_id"])
    op.create_index("ix_ac_agent_events_issue", "ac_agent_events", ["issue_number"])
    op.create_index(
        "ix_ac_agent_events_recorded_at", "ac_agent_events", ["recorded_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ac_agent_events_recorded_at", "ac_agent_events")
    op.drop_index("ix_ac_agent_events_issue", "ac_agent_events")
    op.drop_index("ix_ac_agent_events_run", "ac_agent_events")
    op.drop_table("ac_agent_events")
