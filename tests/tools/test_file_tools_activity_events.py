"""Tests for activity event emission from file_tools.

Verifies that read_file_lines, write_file, replace_in_file, and
insert_after_in_file each call persist_activity_event with the correct
subtype and payload after a successful operation, and that a DB failure
never propagates to the file-tool caller.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy.exc

from agentception.tools.file_tools import (
    insert_after_in_file,
    read_file_lines,
    replace_in_file,
    write_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_ID = "issue-941"
_WORKTREE_PREFIX = f"/worktrees/{_RUN_ID}/"


def _make_session() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# test_file_read_event_emitted
# ---------------------------------------------------------------------------


def test_file_read_event_emitted(tmp_path: Path) -> None:
    """read_file_lines emits a file_read activity event with correct payload."""
    p = tmp_path / "sample.py"
    p.write_text("line one\nline two\nline three\n", encoding="utf-8")

    # Pretend the file lives inside the worktree so _shorten_path works.
    worktree_file = Path(f"/worktrees/{_RUN_ID}") / "sample.py"
    session = _make_session()

    with patch(
        "agentception.tools.file_tools.persist_activity_event"
    ) as mock_persist:
        result = read_file_lines(
            p,
            start_line=1,
            end_line=2,
            run_id=_RUN_ID,
            session=session,
        )

    assert result["ok"] is True
    mock_persist.assert_called_once()

    _session_arg, run_id_arg, subtype_arg, payload_arg = mock_persist.call_args.args
    assert run_id_arg == _RUN_ID
    assert subtype_arg == "file_read"
    assert payload_arg["start_line"] == 1
    assert payload_arg["end_line"] == 2
    assert payload_arg["total_lines"] == 3
    assert "path" in payload_arg


# ---------------------------------------------------------------------------
# test_file_written_event_emitted
# ---------------------------------------------------------------------------


def test_file_written_event_emitted(tmp_path: Path) -> None:
    """write_file emits a file_written activity event with correct payload."""
    p = tmp_path / "output.txt"
    content = "hello world\n"
    session = _make_session()

    with patch(
        "agentception.tools.file_tools.persist_activity_event"
    ) as mock_persist:
        result = write_file(p, content, run_id=_RUN_ID, session=session)

    assert result["ok"] is True
    mock_persist.assert_called_once()

    _session_arg, run_id_arg, subtype_arg, payload_arg = mock_persist.call_args.args
    assert run_id_arg == _RUN_ID
    assert subtype_arg == "file_written"
    assert "path" in payload_arg
    assert isinstance(payload_arg["byte_count"], int)
    assert payload_arg["byte_count"] > 0


# ---------------------------------------------------------------------------
# test_file_replaced_event_emitted
# ---------------------------------------------------------------------------


def test_file_replaced_event_emitted(tmp_path: Path) -> None:
    """replace_in_file emits a file_replaced activity event with correct payload."""
    p = tmp_path / "edit.txt"
    p.write_text("old content here\n", encoding="utf-8")
    session = _make_session()

    with patch(
        "agentception.tools.file_tools.persist_activity_event"
    ) as mock_persist:
        result = replace_in_file(
            p,
            "old content",
            "new content",
            run_id=_RUN_ID,
            session=session,
        )

    assert result["ok"] is True
    mock_persist.assert_called_once()

    _session_arg, run_id_arg, subtype_arg, payload_arg = mock_persist.call_args.args
    assert run_id_arg == _RUN_ID
    assert subtype_arg == "file_replaced"
    assert "path" in payload_arg
    assert payload_arg["replacement_count"] == 1


# ---------------------------------------------------------------------------
# test_file_inserted_event_emitted
# ---------------------------------------------------------------------------


def test_file_inserted_event_emitted(tmp_path: Path) -> None:
    """insert_after_in_file emits a file_inserted activity event with correct payload."""
    p = tmp_path / "insert.txt"
    p.write_text("first line\nsecond line\n", encoding="utf-8")
    session = _make_session()

    with patch(
        "agentception.tools.file_tools.persist_activity_event"
    ) as mock_persist:
        result = insert_after_in_file(
            p,
            "first line\n",
            "inserted line\n",
            run_id=_RUN_ID,
            session=session,
        )

    assert result["ok"] is True
    mock_persist.assert_called_once()

    _session_arg, run_id_arg, subtype_arg, payload_arg = mock_persist.call_args.args
    assert run_id_arg == _RUN_ID
    assert subtype_arg == "file_inserted"
    assert "path" in payload_arg


# ---------------------------------------------------------------------------
# test_persist_failure_does_not_raise
# ---------------------------------------------------------------------------


def test_persist_failure_does_not_raise(tmp_path: Path) -> None:
    """A DB error in persist_activity_event must not propagate to the caller."""
    p = tmp_path / "safe.txt"
    content = "some content\n"
    session = _make_session()

    with patch(
        "agentception.tools.file_tools.persist_activity_event",
        side_effect=sqlalchemy.exc.OperationalError(
            "connection refused", {}, Exception("db down")
        ),
    ):
        result = write_file(p, content, run_id=_RUN_ID, session=session)

    # Tool must succeed despite the DB failure.
    assert result["ok"] is True
    assert "bytes_written" in result


# ---------------------------------------------------------------------------
# test_no_event_when_run_id_is_none
# ---------------------------------------------------------------------------


def test_no_event_when_run_id_is_none(tmp_path: Path) -> None:
    """When run_id is None, persist_activity_event must not be called."""
    p = tmp_path / "no_event.txt"
    content = "data\n"

    with patch(
        "agentception.tools.file_tools.persist_activity_event"
    ) as mock_persist:
        result = write_file(p, content)  # no run_id, no session

    assert result["ok"] is True
    mock_persist.assert_not_called()
