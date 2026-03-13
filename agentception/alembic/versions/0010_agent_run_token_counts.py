from __future__ import annotations

"""Add token-count columns to agent_runs for real cost tracking.

Replaces the heuristic message-count estimate in telemetry.py with
actual Anthropic API usage data: input, output, cache-write, and
cache-read tokens accumulated across all LLM turns for each run.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("total_input_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("total_output_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("total_cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("total_cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "total_cache_read_tokens")
    op.drop_column("agent_runs", "total_cache_write_tokens")
    op.drop_column("agent_runs", "total_output_tokens")
    op.drop_column("agent_runs", "total_input_tokens")
