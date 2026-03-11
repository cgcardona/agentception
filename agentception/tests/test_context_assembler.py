from __future__ import annotations

"""Unit tests for agentception.services.context_assembler.

Covers:
- _ast_imports: extracts import statements from Python source.
- _ast_enclosing_scope: finds the tightest enclosing function/class.
- _scope_section: builds (label, code_block) from a real file on disk.
- assemble_executor_context: integration with mocked search_codebase.
"""

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.services.context_assembler import (
    _ast_enclosing_scope,
    _ast_imports,
    _scope_section,
    assemble_executor_context,
)
from agentception.services.code_indexer import SearchMatch


# ---------------------------------------------------------------------------
# _ast_imports
# ---------------------------------------------------------------------------


def test_ast_imports_extracts_import_statements() -> None:
    """_ast_imports returns import lines from valid Python source."""
    source = textwrap.dedent("""\
        from __future__ import annotations
        import os
        import sys

        def foo() -> None:
            pass
    """)
    result = _ast_imports(source)
    assert "from __future__ import annotations" in result
    assert "import os" in result
    assert "import sys" in result
    assert "def foo" not in result


def test_ast_imports_deduplicates_lines() -> None:
    """_ast_imports deduplicates repeated import lines."""
    source = "import os\nimport os\n"
    result = _ast_imports(source)
    assert result.count("import os") == 1


def test_ast_imports_returns_empty_on_syntax_error() -> None:
    """_ast_imports returns '' for unparseable source."""
    result = _ast_imports("def broken(:")
    assert result == ""


def test_ast_imports_empty_file() -> None:
    """_ast_imports returns '' for source with no imports."""
    result = _ast_imports("x = 1\n")
    assert result == ""


# ---------------------------------------------------------------------------
# _ast_enclosing_scope
# ---------------------------------------------------------------------------


def test_ast_enclosing_scope_finds_function() -> None:
    """Returns the function boundaries when target_line is inside a function."""
    source = textwrap.dedent("""\
        def foo() -> None:
            x = 1
            y = 2

        def bar() -> None:
            z = 3
    """)
    start, end, name = _ast_enclosing_scope(source, 2)
    assert name == "foo"
    assert start == 1
    assert end == 3


def test_ast_enclosing_scope_finds_innermost_scope() -> None:
    """Returns the innermost (smallest) scope when nested inside a class."""
    source = textwrap.dedent("""\
        class MyClass:
            def my_method(self) -> None:
                x = 1
    """)
    start, end, name = _ast_enclosing_scope(source, 3)
    assert name == "my_method"


def test_ast_enclosing_scope_falls_back_to_window_at_module_level() -> None:
    """Falls back to a ±20-line window when no enclosing scope exists."""
    source = "x = 1\ny = 2\n"
    start, end, name = _ast_enclosing_scope(source, 1)
    assert start >= 1
    assert end >= 1
    assert "line 1" in name


def test_ast_enclosing_scope_syntax_error_falls_back() -> None:
    """Falls back gracefully on unparseable source."""
    start, end, name = _ast_enclosing_scope("def broken(:", 1)
    assert start >= 1
    assert end >= 1


# ---------------------------------------------------------------------------
# _scope_section
# ---------------------------------------------------------------------------


def test_scope_section_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """Returns ('', '') when the file does not exist."""
    label, block = _scope_section(tmp_path, "nonexistent.py", 1)
    assert label == ""
    assert block == ""


def test_scope_section_extracts_python_scope(tmp_path: Path) -> None:
    """Returns (label, code_block) with scope body and imports for a Python file."""
    src = textwrap.dedent("""\
        from __future__ import annotations
        import os

        def my_func() -> str:
            return "hello"
    """)
    (tmp_path / "mymodule.py").write_text(src)
    label, block = _scope_section(tmp_path, "mymodule.py", 4)
    assert "my_func" in label
    assert "mymodule.py" in label
    # Imports should appear in the code block.
    assert "import os" in block
    # Scope body should appear.
    assert "return" in block


def test_scope_section_non_python_returns_window(tmp_path: Path) -> None:
    """Returns a line-range window for non-Python files."""
    content = "\n".join(f"line {i}" for i in range(1, 50))
    (tmp_path / "README.md").write_text(content)
    label, block = _scope_section(tmp_path, "README.md", 25)
    assert "README.md" in label
    assert "line 25" in block


# ---------------------------------------------------------------------------
# assemble_executor_context
# ---------------------------------------------------------------------------


def _make_match(file: str, start_line: int = 1, end_line: int = 10) -> SearchMatch:
    return SearchMatch(
        file=file,
        chunk="def foo(): ...",
        start_line=start_line,
        end_line=end_line,
        score=0.9,
    )


@pytest.mark.anyio
async def test_assemble_executor_context_returns_sections(tmp_path: Path) -> None:
    """assemble_executor_context returns formatted Markdown with scope bodies."""
    src = textwrap.dedent("""\
        from __future__ import annotations

        def target_function() -> int:
            return 42
    """)
    (tmp_path / "myfile.py").write_text(src)

    match = _make_match("myfile.py", start_line=3)

    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await assemble_executor_context(
            issue_title="Fix target_function",
            issue_body="Please fix target_function to return 99.",
            worktree_path=tmp_path,
            existing_matches=[match],
        )

    assert "Pre-extracted Code Context" in result
    assert "myfile.py" in result
    assert "target_function" in result


@pytest.mark.anyio
async def test_assemble_executor_context_deduplicates_matches(tmp_path: Path) -> None:
    """Duplicate (file, start_line) pairs appear only once in the output."""
    src = textwrap.dedent("""\
        def dup() -> None:
            pass
    """)
    (tmp_path / "dup.py").write_text(src)

    match = _make_match("dup.py", start_line=1)
    duplicate = _make_match("dup.py", start_line=1)

    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        return_value=[duplicate],
    ):
        result = await assemble_executor_context(
            issue_title="Test dedup",
            issue_body="Body text.",
            worktree_path=tmp_path,
            existing_matches=[match],
        )

    # Even though the match appears twice, the section appears once.
    assert result.count("`dup.py`") == 1


@pytest.mark.anyio
async def test_assemble_executor_context_returns_empty_string_when_no_matches(
    tmp_path: Path,
) -> None:
    """Returns '' when no matches exist and Qdrant returns nothing."""
    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await assemble_executor_context(
            issue_title="Title",
            issue_body="Body",
            worktree_path=tmp_path,
            existing_matches=[],
        )
    assert result == ""


@pytest.mark.anyio
async def test_assemble_executor_context_merges_parallel_results(tmp_path: Path) -> None:
    """Qdrant results from parallel queries are merged with existing_matches."""
    src = textwrap.dedent("""\
        def from_parallel() -> None:
            pass
    """)
    (tmp_path / "parallel.py").write_text(src)

    parallel_match = _make_match("parallel.py", start_line=1)

    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        return_value=[parallel_match],
    ):
        result = await assemble_executor_context(
            issue_title="Title",
            issue_body="Body",
            worktree_path=tmp_path,
            existing_matches=[],
        )

    assert "parallel.py" in result


@pytest.mark.anyio
async def test_assemble_executor_context_tolerates_search_failure(tmp_path: Path) -> None:
    """Parallel search failures do not raise; existing_matches still produce output."""
    src = textwrap.dedent("""\
        def fallback() -> None:
            pass
    """)
    (tmp_path / "fallback.py").write_text(src)
    match = _make_match("fallback.py", start_line=1)

    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Qdrant unavailable"),
    ):
        result = await assemble_executor_context(
            issue_title="Title",
            issue_body="Body",
            worktree_path=tmp_path,
            existing_matches=[match],
        )

    # Falls back to existing_matches only.
    assert "fallback.py" in result


@pytest.mark.anyio
async def test_assemble_executor_context_skips_missing_files(tmp_path: Path) -> None:
    """Matches pointing to non-existent files are skipped gracefully."""
    missing_match = _make_match("does_not_exist.py", start_line=1)

    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await assemble_executor_context(
            issue_title="Title",
            issue_body="Body",
            worktree_path=tmp_path,
            existing_matches=[missing_match],
        )

    assert result == ""
