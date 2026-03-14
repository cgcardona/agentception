"""Tests for the developer-streamlined role prompt (variant for A/B testing)."""

from __future__ import annotations

from pathlib import Path

STREAMLINED_PROMPT = (
    Path(__file__).parent.parent / ".agentception" / "roles" / "developer-streamlined.md"
)


def test_streamlined_prompt_exists() -> None:
    """Rendered developer-streamlined.md exists and is non-empty."""
    assert STREAMLINED_PROMPT.is_file(), f"Missing {STREAMLINED_PROMPT}"
    content = STREAMLINED_PROMPT.read_text()
    assert len(content.strip()) > 0, "developer-streamlined.md must not be empty"


def test_streamlined_prompt_contains_mypy_step() -> None:
    """Streamlined prompt includes the mypy clean step."""
    content = STREAMLINED_PROMPT.read_text()
    assert "mypy" in content, "developer-streamlined.md must contain the mypy step"


def test_streamlined_prompt_excludes_ac_reread() -> None:
    """Streamlined variant must not contain AC-item or code-smell phrasing."""
    content = STREAMLINED_PROMPT.read_text()
    assert "AC item" not in content, "developer-streamlined must not contain 'AC item'"
    assert "code smell" not in content, "developer-streamlined must not contain 'code smell'"
