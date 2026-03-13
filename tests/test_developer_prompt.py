from __future__ import annotations

"""Tests that the compiled developer role prompt contains required content."""

from pathlib import Path


DEVELOPER_PROMPT = Path(__file__).parent.parent / ".agentception" / "roles" / "developer.md"


def test_stop_directive_present() -> None:
    """The hard STOP directive must appear in Step 3 of the compiled prompt."""
    content = DEVELOPER_PROMPT.read_text()
    assert "When pytest exits 0: STOP." in content, (
        f"STOP directive missing from {DEVELOPER_PROMPT}"
    )


def test_step3_ship_present() -> None:
    """Step 3 must be titled 'Ship', not the old AC audit loop."""
    content = DEVELOPER_PROMPT.read_text()
    assert "### Step 3 — Ship" in content, (
        f"'Step 3 — Ship' heading missing from {DEVELOPER_PROMPT}"
    )
    assert "### Step 3 — Acceptance criteria check" not in content, (
        "Old AC audit loop heading still present — should be removed"
    )


def test_build_complete_run_in_step4() -> None:
    """build_complete_run must be called in Step 4 with the PR URL."""
    content = DEVELOPER_PROMPT.read_text()
    assert "build_complete_run` with the PR URL returned by `create_pull_request`" in content, (
        f"build_complete_run PR-URL call missing from Step 4 in {DEVELOPER_PROMPT}"
    )


def test_reviewer_rejection_in_execution_contract() -> None:
    """The Reviewer Rejection priority note must appear before the Definition of Done."""
    content = DEVELOPER_PROMPT.read_text()
    rejection_pos = content.find("Reviewer Rejection")
    dod_pos = content.find("## Definition of Done")
    assert rejection_pos != -1, "Reviewer Rejection note missing from developer.md"
    assert dod_pos != -1, "Definition of Done section missing from developer.md"
    assert rejection_pos < dod_pos, (
        "Reviewer Rejection note must appear before Definition of Done"
    )


def test_no_verbose_tool_usage_gate() -> None:
    """The verbose 4-condition Tool usage gate must not be present."""
    content = DEVELOPER_PROMPT.read_text()
    assert "only call this when ALL four conditions are met" not in content, (
        "Old 4-condition gate is still present — it was removed to restore the sweet-spot "
        "iteration budget"
    )
