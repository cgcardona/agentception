"""Unit tests for the symbol-aware file navigation tools.

Tests for read_symbol, read_window, and find_call_sites added in the
Tier-2 context improvements.  All I/O uses tmp_path; no network required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentception.tools.file_tools import read_symbol, read_window


# ---------------------------------------------------------------------------
# Shared fixture — a small Python module written to tmp_path
# ---------------------------------------------------------------------------

SAMPLE_PY = """\
\"\"\"Sample module for testing.\"\"\"

from __future__ import annotations


def helper(x: int) -> int:
    return x + 1


class Processor:
    \"\"\"A processor class.\"\"\"

    def run(self, value: int) -> int:
        return helper(value) * 2

    def reset(self) -> None:
        pass


async def async_handler(payload: str) -> str:
    return payload.strip()
"""


@pytest.fixture()
def sample_py(tmp_path: Path) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(SAMPLE_PY, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# read_symbol
# ---------------------------------------------------------------------------


def test_read_symbol_finds_top_level_function(sample_py: Path) -> None:
    result = read_symbol(sample_py, "helper")
    assert result["ok"] is True
    content = result["content"]
    assert isinstance(content, str)
    assert "def helper" in content
    assert "return x + 1" in content


def test_read_symbol_finds_class(sample_py: Path) -> None:
    result = read_symbol(sample_py, "Processor")
    assert result["ok"] is True
    content = result["content"]
    assert isinstance(content, str)
    assert "class Processor" in content
    assert "def run" in content
    assert "def reset" in content


def test_read_symbol_finds_async_function(sample_py: Path) -> None:
    result = read_symbol(sample_py, "async_handler")
    assert result["ok"] is True
    content = result["content"]
    assert isinstance(content, str)
    assert "async def async_handler" in content
    assert "payload.strip()" in content


def test_read_symbol_returns_error_for_missing_symbol(sample_py: Path) -> None:
    result = read_symbol(sample_py, "nonexistent_function")
    assert result["ok"] is False
    assert "not found" in str(result.get("error", "")).lower()


def test_read_symbol_returns_error_for_missing_file(tmp_path: Path) -> None:
    result = read_symbol(tmp_path / "ghost.py", "anything")
    assert result["ok"] is False
    assert "not found" in str(result.get("error", "")).lower()


def test_read_symbol_returns_line_numbers(sample_py: Path) -> None:
    result = read_symbol(sample_py, "helper")
    assert result["ok"] is True
    assert isinstance(result["start_line"], int)
    assert isinstance(result["end_line"], int)
    assert result["start_line"] >= 1
    assert result["end_line"] >= result["start_line"]


def test_read_symbol_total_lines_correct(sample_py: Path) -> None:
    result = read_symbol(sample_py, "Processor")
    assert result["ok"] is True
    total = result["total_lines"]
    assert isinstance(total, int)
    assert total == len(SAMPLE_PY.splitlines())


# ---------------------------------------------------------------------------
# read_window
# ---------------------------------------------------------------------------


def test_read_window_returns_centered_lines(sample_py: Path) -> None:
    result = read_window(sample_py, 8, before=2, after=3)
    assert result["ok"] is True
    content = result["content"]
    assert isinstance(content, str)
    # Line 8 of SAMPLE_PY is `    return x + 1`; window should include it.
    assert "return x + 1" in content


def test_read_window_clamps_to_file_boundaries(sample_py: Path) -> None:
    total = len(SAMPLE_PY.splitlines())
    # Center near end — should not exceed file.
    result = read_window(sample_py, total - 1, before=5, after=100)
    assert result["ok"] is True
    assert result["end_line"] == total


def test_read_window_clamps_start_to_1(sample_py: Path) -> None:
    result = read_window(sample_py, 2, before=50, after=5)
    assert result["ok"] is True
    assert result["start_line"] == 1


def test_read_window_returns_center_line(sample_py: Path) -> None:
    result = read_window(sample_py, 10, before=3, after=3)
    assert result["ok"] is True
    assert result["center_line"] == 10


def test_read_window_returns_error_for_missing_file(tmp_path: Path) -> None:
    result = read_window(tmp_path / "ghost.py", 5)
    assert result["ok"] is False
    assert "not found" in str(result.get("error", "")).lower()


def test_read_window_full_file_when_before_after_large(sample_py: Path) -> None:
    total = len(SAMPLE_PY.splitlines())
    result = read_window(sample_py, total // 2, before=10_000, after=10_000)
    assert result["ok"] is True
    assert result["start_line"] == 1
    assert result["end_line"] == total


# ---------------------------------------------------------------------------
# _parse_recon_json — recon plan parser
# ---------------------------------------------------------------------------


def test_parse_recon_json_valid() -> None:
    from agentception.services.agent_loop import _parse_recon_json

    raw = json.dumps({
        "files": ["agentception/foo.py", "tests/test_foo.py"],
        "searches": ["how is Foo defined"],
        "plan": "Implement X by patching Y",
    })
    plan = _parse_recon_json(raw)
    assert plan is not None
    assert plan.files == ["agentception/foo.py", "tests/test_foo.py"]
    assert plan.searches == ["how is Foo defined"]
    assert plan.plan == "Implement X by patching Y"


def test_parse_recon_json_with_markdown_fences() -> None:
    from agentception.services.agent_loop import _parse_recon_json

    raw = '```json\n{"files": ["a.py"], "searches": [], "plan": "do stuff"}\n```'
    plan = _parse_recon_json(raw)
    assert plan is not None
    assert plan.files == ["a.py"]


def test_parse_recon_json_caps_at_limits() -> None:
    from agentception.services.agent_loop import _parse_recon_json

    raw = json.dumps({
        "files": [f"file{i}.py" for i in range(20)],
        "searches": [f"query {i}" for i in range(20)],
        "plan": "do lots",
    })
    plan = _parse_recon_json(raw)
    assert plan is not None
    assert len(plan.files) == 8   # cap raised to 8
    assert len(plan.searches) == 5


def test_parse_recon_json_returns_none_for_garbage() -> None:
    from agentception.services.agent_loop import _parse_recon_json

    assert _parse_recon_json("not json at all") is None
    assert _parse_recon_json('{"files": [], "searches": []}') is None  # both empty


def test_parse_recon_json_json_in_prose() -> None:
    from agentception.services.agent_loop import _parse_recon_json

    raw = 'Sure! Here is the plan:\n{"files": ["x.py"], "searches": ["q"], "plan": "p"}\nDone.'
    plan = _parse_recon_json(raw)
    assert plan is not None
    assert plan.files == ["x.py"]


def test_extract_explicit_file_paths_finds_named_files() -> None:
    from agentception.services.agent_loop import _extract_explicit_file_paths

    text = (
        "Modify `agentception/services/code_indexer.py` and add tests to "
        "`agentception/tests/test_code_indexer.py`. Also see "
        "docs/guides/setup.md for context."
    )
    paths = _extract_explicit_file_paths(text)
    assert "agentception/services/code_indexer.py" in paths
    assert "agentception/tests/test_code_indexer.py" in paths
    assert "docs/guides/setup.md" in paths


def test_extract_explicit_file_paths_deduplicates() -> None:
    from agentception.services.agent_loop import _extract_explicit_file_paths

    text = (
        "Edit agentception/services/llm.py. Then edit agentception/services/llm.py again."
    )
    paths = _extract_explicit_file_paths(text)
    assert paths.count("agentception/services/llm.py") == 1


def test_extract_explicit_file_paths_ignores_unknown_trees() -> None:
    from agentception.services.agent_loop import _extract_explicit_file_paths

    text = "Look at /etc/hosts and /usr/bin/python3 — nothing from there."
    paths = _extract_explicit_file_paths(text)
    assert paths == []
