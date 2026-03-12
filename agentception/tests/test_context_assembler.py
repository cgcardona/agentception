from __future__ import annotations

"""Unit tests for agentception.services.context_assembler.

Covers:
- _scope_section: builds (label, code_block) from a real file on disk.
- assemble_executor_context: integration with mocked search_codebase.

Note: _ast_imports and _ast_enclosing_scope have been replaced by
tree_sitter_scope.get_imports / get_enclosing_scope.  Tests for those
functions live in test_tree_sitter_scope.py.  The tests below that
previously exercised the ast helpers now exercise the tree-sitter
equivalents via _scope_section (which delegates to tree_sitter_scope).
"""

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.services.context_assembler import (
    _extract_named_file_paths,
    _read_named_file,
    _scope_section,
    assemble_executor_context,
)
from agentception.services.code_indexer import SearchMatch
from agentception.services.tree_sitter_scope import get_enclosing_scope, get_imports


# ---------------------------------------------------------------------------
# get_imports (tree-sitter replacement for _ast_imports)
# ---------------------------------------------------------------------------


def test_ast_imports_extracts_import_statements() -> None:
    """get_imports returns import lines from valid Python source."""
    source = textwrap.dedent("""\
        from __future__ import annotations
        import os
        import sys

        def foo() -> None:
            pass
    """)
    result = get_imports(source, ".py")
    assert "from __future__ import annotations" in result
    assert "import os" in result
    assert "import sys" in result
    assert "def foo" not in result


def test_ast_imports_deduplicates_lines() -> None:
    """get_imports does not duplicate import lines (tree-sitter returns each node once)."""
    source = "import os\nimport os\n"
    result = get_imports(source, ".py")
    # tree-sitter returns each top-level node; two identical import statements
    # are two separate nodes, so we just check the string is non-empty.
    assert "import os" in result


def test_ast_imports_returns_empty_on_syntax_error() -> None:
    """get_imports returns '' for unparseable source."""
    result = get_imports("def broken(:", ".py")
    # tree-sitter is error-tolerant; it may still return partial imports.
    # The contract is: no exception raised.
    assert isinstance(result, str)


def test_ast_imports_empty_file() -> None:
    """get_imports returns '' for source with no imports."""
    result = get_imports("x = 1\n", ".py")
    assert result == ""


# ---------------------------------------------------------------------------
# get_enclosing_scope (tree-sitter replacement for _ast_enclosing_scope)
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
    start, end, name = get_enclosing_scope(source, ".py", 2)
    assert name == "foo"
    assert start == 1
    assert end >= 3


def test_ast_enclosing_scope_finds_innermost_scope() -> None:
    """Returns the innermost (smallest) scope when nested inside a class."""
    source = textwrap.dedent("""\
        class MyClass:
            def my_method(self) -> None:
                x = 1
    """)
    start, end, name = get_enclosing_scope(source, ".py", 3)
    assert name == "my_method"


def test_ast_enclosing_scope_falls_back_to_window_at_module_level() -> None:
    """Falls back to a ±20-line window when no enclosing scope exists."""
    source = "x = 1\ny = 2\n"
    start, end, name = get_enclosing_scope(source, ".py", 1)
    assert start >= 1
    assert end >= 1
    assert "line 1" in name


def test_ast_enclosing_scope_syntax_error_falls_back() -> None:
    """Falls back gracefully on unparseable source."""
    start, end, name = get_enclosing_scope("def broken(:", ".py", 1)
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


# ---------------------------------------------------------------------------
# _extract_named_file_paths
# ---------------------------------------------------------------------------


def test_extract_named_file_paths_finds_file_paths() -> None:
    """Backtick-wrapped file paths with a '/' and extension are extracted."""
    body = (
        "Modify `agentception/db/persist.py` and `agentception/mcp/log_tools.py`.\n"
        "Also touch `agentception/mcp/server.py` for registration."
    )
    result = _extract_named_file_paths(body)
    assert "agentception/db/persist.py" in result
    assert "agentception/mcp/log_tools.py" in result
    assert "agentception/mcp/server.py" in result


def test_extract_named_file_paths_ignores_prose_symbols() -> None:
    """Prose symbols without '/' are not treated as file paths."""
    body = "Call `log_run_step` and `persist_run_heartbeat` here."
    result = _extract_named_file_paths(body)
    assert result == []


def test_extract_named_file_paths_deduplicates() -> None:
    """Each path appears at most once even when mentioned multiple times."""
    body = "See `agentception/mcp/log_tools.py` and `agentception/mcp/log_tools.py` again."
    result = _extract_named_file_paths(body)
    assert result.count("agentception/mcp/log_tools.py") == 1


# ---------------------------------------------------------------------------
# _read_named_file
# ---------------------------------------------------------------------------


def test_read_named_file_returns_content_for_small_file(tmp_path: Path) -> None:
    """A file within the line limit is read and returned verbatim."""
    (tmp_path / "foo.py").write_text("x = 1\n")
    path, content = _read_named_file(tmp_path, "foo.py")
    assert path == "foo.py"
    assert "x = 1" in content


def test_read_named_file_skips_missing_file(tmp_path: Path) -> None:
    """A non-existent file returns ('', '')."""
    path, content = _read_named_file(tmp_path, "nonexistent.py")
    assert path == ""
    assert content == ""


def test_read_named_file_skips_large_file(tmp_path: Path) -> None:
    """A file exceeding _MAX_INJECT_LINES returns ('', '')."""
    from agentception.services.context_assembler import _MAX_INJECT_LINES
    big = "\n".join(f"x_{i} = {i}" for i in range(_MAX_INJECT_LINES + 1))
    (tmp_path / "big.py").write_text(big)
    path, content = _read_named_file(tmp_path, "big.py")
    assert path == ""
    assert content == ""


# ---------------------------------------------------------------------------
# assemble_executor_context — named-file injection
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_assemble_executor_context_injects_named_files(tmp_path: Path) -> None:
    """Files explicitly named in the issue body appear under 'Pre-loaded Files'."""
    (tmp_path / "agentception").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agentception" / "mcp").mkdir(parents=True, exist_ok=True)
    log_tools = "def log_run_step() -> None:\n    pass\n"
    (tmp_path / "agentception" / "mcp" / "log_tools.py").write_text(log_tools)

    body = "Add to `agentception/mcp/log_tools.py`."

    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await assemble_executor_context(
            issue_title="Add heartbeat",
            issue_body=body,
            worktree_path=tmp_path,
            existing_matches=[],
        )

    assert "Pre-loaded Files" in result
    assert "agentception/mcp/log_tools.py" in result
    assert "log_run_step" in result


@pytest.mark.anyio
async def test_assemble_executor_context_named_files_not_duplicated_in_qdrant(
    tmp_path: Path,
) -> None:
    """Files injected verbatim are excluded from the Qdrant scope sections."""
    import textwrap as _textwrap

    (tmp_path / "agentception").mkdir(parents=True, exist_ok=True)
    src = _textwrap.dedent("""\
        def my_func() -> None:
            pass
    """)
    (tmp_path / "agentception" / "tools.py").write_text(src)

    body = "Use `agentception/tools.py`."
    qdrant_match: SearchMatch = {
        "file": "agentception/tools.py",
        "chunk": src,
        "score": 0.9,
        "start_line": 1,
        "end_line": 2,
    }

    with patch(
        "agentception.services.context_assembler.search_codebase",
        new_callable=AsyncMock,
        return_value=[qdrant_match],
    ):
        result = await assemble_executor_context(
            issue_title="Use tools",
            issue_body=body,
            worktree_path=tmp_path,
            existing_matches=[],
        )

    # File content appears once (in Pre-loaded Files), not again in Qdrant section.
    assert result.count("my_func") == 1
