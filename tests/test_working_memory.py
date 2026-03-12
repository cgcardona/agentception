"""Tests for the new FileEditEvent integration in WorkingMemory.

Covers _auto_track_file_write diff computation, the 120-line cap, and the
before="" creation-style diff convention.
"""

from __future__ import annotations

from agentception.services.working_memory import _auto_track_file_write


def test_file_edit_event_populated() -> None:
    """_auto_track_file_write produces a FileEditEvent with correct diff content."""
    event = _auto_track_file_write("foo.py", "old\n", "new\n")

    assert event.path == "foo.py"
    assert "-old\n" in event.diff
    assert "+new\n" in event.diff
    assert event.lines_omitted == 0


def test_file_edit_event_lines_omitted_zero_for_short_diff() -> None:
    """lines_omitted is 0 when the diff fits within 120 lines."""
    before = "line\n" * 5
    after = "line\n" * 4 + "changed\n"
    event = _auto_track_file_write("small.py", before, after)

    assert event.lines_omitted == 0
    assert event.diff != ""


def test_file_edit_event_truncated_when_diff_exceeds_120_lines() -> None:
    """Diffs longer than 120 lines are truncated; lines_omitted reflects the hidden count."""
    # Produce a diff that is definitely > 120 lines: replace 200 distinct lines.
    before = "".join(f"old line {i}\n" for i in range(200))
    after = "".join(f"new line {i}\n" for i in range(200))
    event = _auto_track_file_write("big.py", before, after)

    assert event.lines_omitted > 0
    # The diff string itself must be shorter than the full diff.
    assert event.diff.count("\n") <= 120


def test_file_edit_event_creation_diff() -> None:
    """Passing before='' produces a diff where every content line is an addition."""
    event = _auto_track_file_write("new_file.py", "", "hello\nworld\n")

    assert event.path == "new_file.py"
    assert "+hello\n" in event.diff
    assert "+world\n" in event.diff
    # No removal lines for a creation diff.
    assert "-hello\n" not in event.diff
    assert "-world\n" not in event.diff
