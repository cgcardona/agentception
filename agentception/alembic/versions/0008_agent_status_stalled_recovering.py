from __future__ import annotations

"""Document new AgentStatus values: stalled and recovering.

The ``agent_runs.status`` column is ``String(64)`` and stores enum values as
plain strings.  No schema change is required — the new values fit within the
existing column.  This migration exists solely to record the intent in the
Alembic history so the version chain stays coherent.

New valid values added to AgentStatus:
- ``stalled``   — set by the poller watchdog when no commit progress for 30 min
- ``recovering`` — set when an auto-heal / re-dispatch is attempted
"""

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No DDL required — String(64) column already accepts these values.
    pass


def downgrade() -> None:
    # No DDL to reverse.
    pass
