from __future__ import annotations

"""Drop dead tables: role_versions and task_runs.

``role_versions`` was written to by ``persist_role_version()`` but was never
read back by any query function — the A/B role-version tracking reads from the
``role-versions.json`` filesystem file instead.

``task_runs`` was defined for a cognitive-architecture enrichment pipeline that
is no longer active.  No query functions, routes, or MCP tools ever read from
it.

Both tables are dropped in a single migration to keep the history clean.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("role_versions")
    op.drop_table("task_runs")


def downgrade() -> None:
    # Recreate role_versions
    import sqlalchemy as sa

    op.create_table(
        "role_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("role_name", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_name", "content_hash", name="uq_role_versions"),
    )
    op.create_index("ix_role_versions_role_name", "role_versions", ["role_name"])

    # Recreate task_runs
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(256), nullable=False),
        sa.Column("task_type", sa.String(128), nullable=False),
        sa.Column("branch", sa.String(256), nullable=True),
        sa.Column("commit_sha", sa.String(40), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_runs_task_type", "task_runs", ["task_type"])
    op.create_index("ix_task_runs_status", "task_runs", ["status"])
