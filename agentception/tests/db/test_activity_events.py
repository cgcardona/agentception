from __future__ import annotations

"""Tests for agentception.db.activity_events.

Covers:
- test_persist_activity_event: happy-path write with subtype "shell_start".
- test_payload_typeddict_completeness: every ACTIVITY_SUBTYPES entry has a
  corresponding TypedDict class importable from the module.
"""

import importlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agentception.db.base import Base
from agentception.db.activity_events import (
    ACTIVITY_SUBTYPES,
    SUBTYPE_TYPEDDICT_NAMES,
    persist_activity_event,
)
from agentception.db.models import ACAgentEvent


# ---------------------------------------------------------------------------
# In-memory SQLite fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session() -> Session:
    """Yield a synchronous SQLite in-memory session with all tables created."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    # Create only the tables we need — avoids FK issues with missing tables.
    # We create all tables so FK references resolve correctly.
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_persist_activity_event(db_session: Session) -> None:
    """persist_activity_event writes an ACAgentEvent row with correct fields."""
    payload: dict[str, object] = {
        "cmd_preview": "python -m pytest agentception/tests/ -v",
        "cwd": "/worktrees/issue-938",
    }

    persist_activity_event(
        session=db_session,
        run_id="issue-938",
        subtype="shell_start",
        payload=payload,
    )
    db_session.flush()

    rows = db_session.query(ACAgentEvent).all()
    assert len(rows) == 1, "Expected exactly one ACAgentEvent row"

    row = rows[0]
    assert row.event_type == "activity", (
        f"event_type should be 'activity', got {row.event_type!r}"
    )
    assert row.agent_run_id == "issue-938", (
        f"agent_run_id should be 'issue-938', got {row.agent_run_id!r}"
    )

    stored_payload = json.loads(row.payload)
    assert stored_payload["subtype"] == "shell_start", (
        f"payload['subtype'] should be 'shell_start', got {stored_payload['subtype']!r}"
    )
    assert stored_payload["cmd_preview"] == payload["cmd_preview"]
    assert stored_payload["cwd"] == payload["cwd"]


def test_persist_activity_event_invalid_subtype_raises(db_session: Session) -> None:
    """persist_activity_event raises ValueError for unknown subtypes."""
    with pytest.raises(ValueError, match="Unknown activity subtype"):
        persist_activity_event(
            session=db_session,
            run_id="issue-938",
            subtype="not_a_real_subtype",
            payload={},
        )


def test_payload_typeddict_completeness() -> None:
    """Every subtype in ACTIVITY_SUBTYPES has a TypedDict class importable from the module."""
    module = importlib.import_module("agentception.db.activity_events")

    # Verify SUBTYPE_TYPEDDICT_NAMES covers every subtype
    assert set(SUBTYPE_TYPEDDICT_NAMES.keys()) == set(ACTIVITY_SUBTYPES), (
        "SUBTYPE_TYPEDDICT_NAMES keys must match ACTIVITY_SUBTYPES exactly. "
        f"Missing: {ACTIVITY_SUBTYPES - set(SUBTYPE_TYPEDDICT_NAMES.keys())}, "
        f"Extra: {set(SUBTYPE_TYPEDDICT_NAMES.keys()) - ACTIVITY_SUBTYPES}"
    )

    # Verify each TypedDict class is importable from the module
    for subtype, class_name in SUBTYPE_TYPEDDICT_NAMES.items():
        cls = getattr(module, class_name, None)
        assert cls is not None, (
            f"Subtype {subtype!r} maps to {class_name!r} but that class "
            f"is not defined in agentception.db.activity_events"
        )
        assert isinstance(cls, type), (
            f"{class_name} should be a class, got {type(cls)!r}"
        )


def test_activity_subtypes_count() -> None:
    """ACTIVITY_SUBTYPES contains exactly 15 entries."""
    assert len(ACTIVITY_SUBTYPES) == 15, (
        f"Expected 15 activity subtypes, got {len(ACTIVITY_SUBTYPES)}: "
        f"{sorted(ACTIVITY_SUBTYPES)}"
    )


def test_persist_activity_event_payload_includes_subtype(db_session: Session) -> None:
    """The stored payload always includes 'subtype' even when caller omits it."""
    persist_activity_event(
        session=db_session,
        run_id="issue-100",
        subtype="git_push",
        payload={"branch": "feat/issue-100"},
    )
    db_session.flush()

    row = db_session.query(ACAgentEvent).filter_by(agent_run_id="issue-100").one()
    stored = json.loads(row.payload)
    assert stored["subtype"] == "git_push"
    assert stored["branch"] == "feat/issue-100"
