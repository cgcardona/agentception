from __future__ import annotations

"""Plan-scoped integration branch: plan_id, plan_branch on runs; plan_issues and plan_branches tables.

When a plan (filing batch) is used for dispatch, the first dispatch creates a plan
branch from origin/dev; all issue worktrees and PRs target that branch. Merges
into dev happen only when the plan is complete (last reviewer merges → plan→dev PR).
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("plan_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("plan_branch", sa.String(256), nullable=True),
    )
    op.create_index("ix_agent_runs_plan_id", "agent_runs", ["plan_id"], unique=False)

    op.create_table(
        "plan_issues",
        sa.Column("plan_id", sa.String(128), nullable=False),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "repo", "issue_number"),
    )
    op.create_index("ix_plan_issues_plan_id", "plan_issues", ["plan_id"], unique=False)
    op.create_index("ix_plan_issues_repo_issue", "plan_issues", ["repo", "issue_number"], unique=False)

    op.create_table(
        "plan_branches",
        sa.Column("plan_id", sa.String(128), nullable=False),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("branch_name", sa.String(256), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "repo"),
    )
    op.create_index("ix_plan_branches_plan_id", "plan_branches", ["plan_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_plan_branches_plan_id", table_name="plan_branches")
    op.drop_table("plan_branches")
    op.drop_index("ix_plan_issues_repo_issue", table_name="plan_issues")
    op.drop_index("ix_plan_issues_plan_id", table_name="plan_issues")
    op.drop_table("plan_issues")
    op.drop_index("ix_agent_runs_plan_id", table_name="agent_runs")
    op.drop_column("agent_runs", "plan_branch")
    op.drop_column("agent_runs", "plan_id")
