"""Unit tests for the persistent working-memory module.

Covers read/write/merge/render at the unit level.  No DB or network required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentception.services.working_memory import (
    WorkingMemory,
    merge_memory,
    read_memory,
    render_memory,
    write_memory,
)


# ---------------------------------------------------------------------------
# read_memory
# ---------------------------------------------------------------------------


def test_read_memory_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_memory(tmp_path) is None


def test_read_memory_corrupt_json_returns_none(tmp_path: Path) -> None:
    (tmp_path / ".agentception").mkdir()
    (tmp_path / ".agentception" / "memory.json").write_text("not json", encoding="utf-8")
    assert read_memory(tmp_path) is None


def test_read_memory_non_dict_returns_none(tmp_path: Path) -> None:
    (tmp_path / ".agentception").mkdir()
    (tmp_path / ".agentception" / "memory.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert read_memory(tmp_path) is None


def test_read_memory_full_object(tmp_path: Path) -> None:
    (tmp_path / ".agentception").mkdir()
    payload = {
        "plan": "implement X",
        "files_examined": ["a.py", "b.py"],
        "findings": {"a.py": "found bug"},
        "decisions": ["use TypedDict"],
        "next_steps": ["write tests"],
        "blockers": ["unclear spec"],
    }
    (tmp_path / ".agentception" / "memory.json").write_text(json.dumps(payload), encoding="utf-8")
    memory = read_memory(tmp_path)
    assert memory is not None
    assert memory["plan"] == "implement X"
    assert memory["files_examined"] == ["a.py", "b.py"]
    assert memory["findings"] == {"a.py": "found bug"}
    assert memory["decisions"] == ["use TypedDict"]
    assert memory["next_steps"] == ["write tests"]
    assert memory["blockers"] == ["unclear spec"]


def test_read_memory_ignores_wrong_type_fields(tmp_path: Path) -> None:
    (tmp_path / ".agentception").mkdir()
    # plan is an int instead of str — should be silently skipped
    payload = {"plan": 42, "decisions": ["ok"]}
    (tmp_path / ".agentception" / "memory.json").write_text(json.dumps(payload), encoding="utf-8")
    memory = read_memory(tmp_path)
    assert memory is not None
    assert "plan" not in memory
    assert memory["decisions"] == ["ok"]


# ---------------------------------------------------------------------------
# write_memory
# ---------------------------------------------------------------------------


def test_write_memory_creates_agentception_directory(tmp_path: Path) -> None:
    mem: WorkingMemory = WorkingMemory(plan="test plan")
    write_memory(tmp_path, mem)
    assert (tmp_path / ".agentception" / "memory.json").exists()


def test_write_memory_roundtrip(tmp_path: Path) -> None:
    mem: WorkingMemory = WorkingMemory(
        plan="p",
        files_examined=["x.py"],
        findings={"x.py": "note"},
        decisions=["d1"],
        next_steps=["s1"],
        blockers=["b1"],
    )
    write_memory(tmp_path, mem)
    loaded = read_memory(tmp_path)
    assert loaded == mem


# ---------------------------------------------------------------------------
# merge_memory
# ---------------------------------------------------------------------------


def test_merge_memory_no_existing_returns_update() -> None:
    update: WorkingMemory = WorkingMemory(plan="new plan", decisions=["d1"])
    result = merge_memory(None, update)
    assert result["plan"] == "new plan"
    assert result["decisions"] == ["d1"]
    assert "files_examined" not in result


def test_merge_memory_preserves_unmentioned_fields() -> None:
    existing: WorkingMemory = WorkingMemory(plan="old", files_examined=["a.py"])
    update: WorkingMemory = WorkingMemory(next_steps=["step1"])
    result = merge_memory(existing, update)
    assert result["plan"] == "old"
    assert result["files_examined"] == ["a.py"]
    assert result["next_steps"] == ["step1"]


def test_merge_memory_overwrites_plan() -> None:
    existing: WorkingMemory = WorkingMemory(plan="old plan")
    update: WorkingMemory = WorkingMemory(plan="new plan")
    result = merge_memory(existing, update)
    assert result["plan"] == "new plan"


def test_merge_memory_findings_are_union_merged() -> None:
    existing: WorkingMemory = WorkingMemory(findings={"a.py": "note a", "b.py": "note b"})
    update: WorkingMemory = WorkingMemory(findings={"b.py": "updated", "c.py": "note c"})
    result = merge_memory(existing, update)
    assert result["findings"] == {
        "a.py": "note a",
        "b.py": "updated",
        "c.py": "note c",
    }


def test_merge_memory_empty_update_preserves_everything() -> None:
    existing: WorkingMemory = WorkingMemory(plan="keep", decisions=["d"])
    update: WorkingMemory = WorkingMemory()
    result = merge_memory(existing, update)
    assert result["plan"] == "keep"
    assert result["decisions"] == ["d"]


# ---------------------------------------------------------------------------
# render_memory
# ---------------------------------------------------------------------------


def test_render_memory_empty_shows_heading_only() -> None:
    rendered = render_memory(WorkingMemory())
    assert rendered.startswith("## Working Memory")
    assert "Plan" not in rendered
    assert "Findings" not in rendered


def test_render_memory_plan_appears() -> None:
    rendered = render_memory(WorkingMemory(plan="implement caching layer"))
    assert "implement caching layer" in rendered


def test_render_memory_files_examined_appear() -> None:
    rendered = render_memory(WorkingMemory(files_examined=["foo.py", "bar.py"]))
    assert "`foo.py`" in rendered
    assert "`bar.py`" in rendered


def test_render_memory_next_steps_numbered() -> None:
    rendered = render_memory(WorkingMemory(next_steps=["alpha", "beta"]))
    assert "1. alpha" in rendered
    assert "2. beta" in rendered


def test_render_memory_blockers_have_warning_emoji() -> None:
    rendered = render_memory(WorkingMemory(blockers=["spec unclear"]))
    assert "⚠️" in rendered
    assert "spec unclear" in rendered


def test_render_memory_findings_key_value() -> None:
    rendered = render_memory(WorkingMemory(findings={"agent_loop.py": "loop starts at line 157"}))
    assert "`agent_loop.py`" in rendered
    assert "loop starts at line 157" in rendered


def test_render_memory_findings_appear_before_plan() -> None:
    """Findings must render before plan so the agent reads type constraints first."""
    rendered = render_memory(
        WorkingMemory(
            plan="Implement stall detection",
            findings={"agentception/models/__init__.py": "[Type signatures]\nclass PipelineState(BaseModel):"},
        )
    )
    findings_pos = rendered.index("Type signatures")
    plan_pos = rendered.index("Implement stall detection")
    assert findings_pos < plan_pos, (
        "Findings must appear before plan in render_memory output"
    )
