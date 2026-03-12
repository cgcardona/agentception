from __future__ import annotations

"""Unit tests for agentception.services.tree_sitter_scope.

Covers:
- get_enclosing_scope: Python, TypeScript, Go, unsupported extension, syntax error.
- get_imports: Python, TypeScript, unsupported extension.
"""

import textwrap

from agentception.services.tree_sitter_scope import get_enclosing_scope, get_imports


# ---------------------------------------------------------------------------
# get_enclosing_scope
# ---------------------------------------------------------------------------


def test_python_scope_extracts_function() -> None:
    """Two-function Python source; hit inside function B; name == 'B'."""
    source = textwrap.dedent("""\
        def A() -> None:
            x = 1

        def B() -> None:
            y = 2
            z = 3
    """)
    # Line 5 is inside B ("y = 2")
    start, end, name = get_enclosing_scope(source, ".py", 5)
    assert name == "B"
    assert start == 4
    assert end >= 6


def test_typescript_scope_extracts_function() -> None:
    """TypeScript source with two functions; hit inside the second; name matches."""
    source = textwrap.dedent("""\
        function alpha(): void {
            const a = 1;
        }

        function beta(): void {
            const b = 2;
            const c = 3;
        }
    """)
    # Line 6 is inside beta ("const b = 2;")
    start, end, name = get_enclosing_scope(source, ".ts", 6)
    assert name == "beta"
    assert start == 5
    assert end >= 7


def test_go_scope_extracts_function() -> None:
    """Go source with a named function; name matches."""
    source = textwrap.dedent("""\
        package main

        func Hello() string {
            return "hello"
        }
    """)
    # Line 4 is inside Hello ("return ...")
    start, end, name = get_enclosing_scope(source, ".go", 4)
    assert name == "Hello"
    assert start == 3
    assert end >= 4


def test_unsupported_extension_falls_back() -> None:
    """Unsupported extension returns ±20-line window with name starting 'line '."""
    source = "\n".join(f"<p>line {i}</p>" for i in range(1, 60))
    target = 30
    start, end, name = get_enclosing_scope(source, ".html", target)
    assert start <= target
    assert end >= target
    assert end - start <= 41  # ±20 window = at most 41 lines
    assert name.startswith("line ")


def test_syntax_error_falls_back() -> None:
    """Malformed Python source returns fallback window without raising."""
    source = "not valid {{{{{ python"
    start, end, name = get_enclosing_scope(source, ".py", 1)
    # Must not raise; must return a valid window.
    assert start >= 1
    assert end >= 1
    # name is either "line 1" (fallback) or a tree-sitter best-effort result.
    assert isinstance(name, str)
    assert len(name) > 0


# ---------------------------------------------------------------------------
# get_imports
# ---------------------------------------------------------------------------


def test_get_imports_python() -> None:
    """Python source with two import lines; both appear in the returned string."""
    source = textwrap.dedent("""\
        from __future__ import annotations
        import os

        def foo() -> None:
            pass
    """)
    result = get_imports(source, ".py")
    assert "from __future__ import annotations" in result
    assert "import os" in result


def test_get_imports_typescript() -> None:
    """TypeScript source with import statements; they are returned."""
    source = textwrap.dedent("""\
        import { readFile } from 'fs';
        import path from 'path';

        function main(): void {
            console.log('hello');
        }
    """)
    result = get_imports(source, ".ts")
    assert "readFile" in result
    assert "path" in result


def test_get_imports_unsupported_returns_empty() -> None:
    """Unsupported extension returns empty string."""
    source = "<html><body>hello</body></html>"
    result = get_imports(source, ".html")
    assert result == ""
