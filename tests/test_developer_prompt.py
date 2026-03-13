from __future__ import annotations

"""Tests that the compiled developer role prompt contains required content."""

from pathlib import Path


DEVELOPER_PROMPT = Path(__file__).parent.parent / ".agentception" / "roles" / "developer.md"


def test_build_complete_run_checklist_present() -> None:
    """The four-condition pre-flight checklist must appear verbatim in the compiled prompt."""
    content = DEVELOPER_PROMPT.read_text()
    assert '"build_complete_run" — only call this when ALL four conditions are met' not in content or \
        "`build_complete_run` — only call this when ALL four conditions are met" in content, \
        "Checklist header not found in developer.md"
    assert "`build_complete_run` — only call this when ALL four conditions are met" in content, \
        f"Pre-flight checklist missing from {DEVELOPER_PROMPT}"


def test_tool_usage_section_present() -> None:
    """The 'Tool usage' section must exist in the compiled developer prompt."""
    content = DEVELOPER_PROMPT.read_text()
    assert "## Tool usage" in content, f"'## Tool usage' section missing from {DEVELOPER_PROMPT}"


def test_four_conditions_present() -> None:
    """All four pre-flight conditions must appear in the compiled developer prompt."""
    content = DEVELOPER_PROMPT.read_text()
    assert "You have written at least one file using `write_file` or `replace_in_file`." in content
    assert "You have run `mypy` and `pytest` locally and both pass." in content
    assert "You have called `git_commit_and_push` and received a success response." in content
    assert "You have an open pull request URL confirmed in the tool response." in content


def test_warning_text_present() -> None:
    """The warning about premature build_complete_run calls must be present."""
    content = DEVELOPER_PROMPT.read_text()
    assert "Calling `build_complete_run` without a committed PR immediately ends your" in content
