from __future__ import annotations

"""Tests for FileEditEvent and other domain models in agentception.models."""

import datetime

import pytest
from pydantic import ValidationError

from agentception.models import FileEditEvent


def _make_event(**overrides: object) -> FileEditEvent:
    defaults: dict[str, object] = {
        "timestamp": datetime.datetime(2024, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc),
        "path": "agentception/models/__init__.py",
        "diff": "@@ -1,3 +1,4 @@\n+from __future__ import annotations\n context\n",
        "lines_omitted": 0,
    }
    defaults.update(overrides)
    return FileEditEvent(**defaults)


def test_file_edit_event_fields() -> None:
    """FileEditEvent instantiates correctly with all four fields present."""
    ts = datetime.datetime(2024, 6, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    event = FileEditEvent(
        timestamp=ts,
        path="agentception/config.py",
        diff="--- a/agentception/config.py\n+++ b/agentception/config.py\n@@ -1 +1 @@\n-old\n+new\n",
        lines_omitted=5,
    )

    assert event.timestamp == ts
    assert event.path == "agentception/config.py"
    assert "old" in event.diff
    assert event.lines_omitted == 5


def test_file_edit_event_lines_omitted_defaults_to_zero() -> None:
    """lines_omitted defaults to 0 when not supplied."""
    event = FileEditEvent(
        timestamp=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
        path="agentception/app.py",
        diff="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
    )
    assert event.lines_omitted == 0


def test_file_edit_event_frozen_raises_on_mutation() -> None:
    """Mutating a frozen FileEditEvent raises TypeError (Pydantic frozen model)."""
    event = _make_event()
    with pytest.raises((ValidationError, TypeError)):
        event.path = "other/path.py"  # type: ignore[misc]
