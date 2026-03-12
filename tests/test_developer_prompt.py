from __future__ import annotations

from pathlib import Path


COMPILED_PROMPT = Path(__file__).parent.parent / ".agentception" / "roles" / "developer.md"


def test_build_complete_run_checklist_present() -> None:
    """Compiled developer role prompt must contain the build_complete_run pre-flight checklist."""
    content = COMPILED_PROMPT.read_text()
    assert '`build_complete_run` — only call this when ALL four conditions are met' in content, (
        f"Pre-flight checklist not found in {COMPILED_PROMPT}"
    )
