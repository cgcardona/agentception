from __future__ import annotations

"""Add depends_on_json column to issues table.

``depends_on_json`` (TEXT NOT NULL DEFAULT '[]') stores a JSON list of GitHub
issue numbers that a given issue depends on (i.e. must be merged before this
one can start).  It is populated by ``persist_issue_depends_on`` when
``file_issues`` finishes resolving ``PlanIssue.depends_on`` references to
real GitHub issue numbers.

For pre-existing rows the column defaults to the empty list ``'[]'``.

Revision ID: 0011
Revises: 0010
Create Date: 2026-03-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "issues",
        sa.Column(
            "depends_on_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("issues", "depends_on_json")
