from __future__ import annotations

"""agent_events — structured MCP callback events per agent run

Stores events that agents push via the build_report_* MCP tools.
Each row is one deliberate signal from a running agent (step start,
blocker, decision, or completion) — distinct from the raw thinking
stream captured in agent_messages.

Revision ID: ac0002
Revises: 0001
Create Date: 2026-03-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_run_id",
            sa.String(512),
            sa.ForeignKey("agent_runs.id"),
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
    op.create_index("ix_agent_events_run", "agent_events", ["agent_run_id"])
    op.create_index("ix_agent_events_issue", "agent_events", ["issue_number"])
    op.create_index(
        "ix_agent_events_recorded_at", "agent_events", ["recorded_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_events_recorded_at", "agent_events")
    op.drop_index("ix_agent_events_issue", "agent_events")
    op.drop_index("ix_agent_events_run", "agent_events")
    op.drop_table("agent_events")
