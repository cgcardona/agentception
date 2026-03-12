from __future__ import annotations

"""Tree-sitter-based scope extraction for multiple languages.

This module provides a language-agnostic API for extracting the enclosing
function/class scope and import block from source code.  It replaces the
Python-only ``ast``-based helpers in ``context_assembler.py``.

## Dispatch table

Each supported file extension maps to a 3-tuple:
  ``(grammar_callable, scope_node_types, import_node_types)``

| Extension     | Grammar                                  | Scope nodes                                                          | Import nodes                        |
|---------------|------------------------------------------|----------------------------------------------------------------------|-------------------------------------|
| ``.py``       | ``tree_sitter_python.language``          | function_definition, async_function_definition, class_definition     | import_statement, import_from_statement, future_import_statement |
| ``.ts/.tsx``  | ``tree_sitter_typescript.language_typescript`` | function_declaration, method_definition, arrow_function, class_declaration | import_statement |
| ``.js/.jsx``  | ``tree_sitter_javascript.language``      | function_declaration, method_definition, arrow_function, class_declaration | import_statement |
| ``.go``       | ``tree_sitter_go.language``              | function_declaration, method_declaration                             | import_declaration                  |
| ``.rs``       | ``tree_sitter_rust.language``            | function_item, impl_item                                             | use_declaration                     |
| ``.java``     | ``tree_sitter_java.language``            | method_declaration, class_declaration                                | import_declaration                  |
| ``.rb``       | ``tree_sitter_ruby.language``            | method, singleton_method, class                                      | *(none)*                            |
| all others    | *(fallback)*                             | ±20-line window                                                      | *(none)*                            |

## Fallback behaviour

When the extension is unsupported, the source cannot be parsed, or no
enclosing scope node is found, ``get_enclosing_scope`` returns a ±20-line
window centred on ``target_line`` with the name ``f"line {target_line}"``.
``get_imports`` returns ``""`` for unsupported extensions or on any failure.

## Parser caching

``Parser`` instances are expensive to construct.  A module-level dict
``_parser_cache`` stores one ``Parser`` per extension, initialised lazily on
the first call for that extension.  Subsequent calls reuse the cached instance.
"""

from collections.abc import Callable
from typing import NamedTuple

from tree_sitter import Language, Node, Parser


# ---------------------------------------------------------------------------
# Internal language config
# ---------------------------------------------------------------------------


class _LangConfig(NamedTuple):
    grammar: Callable[[], object]  # returns a Language capsule; typed as object due to missing stubs
    scope_types: frozenset[str]
    import_types: frozenset[str]


def _make_dispatch() -> dict[str, _LangConfig]:
    """Build the extension → language config mapping.

    Imports are deferred inside this function so that a missing grammar package
    raises ImportError only when that language is first requested, not at module
    load time.
    """
    import tree_sitter_python
    import tree_sitter_typescript
    import tree_sitter_javascript
    import tree_sitter_go
    import tree_sitter_rust
    import tree_sitter_java
    import tree_sitter_ruby

    py_cfg = _LangConfig(
        grammar=tree_sitter_python.language,
        scope_types=frozenset(
            {"function_definition", "async_function_definition", "class_definition"}
        ),
        import_types=frozenset(
            {"import_statement", "import_from_statement", "future_import_statement"}
        ),
    )
    ts_cfg = _LangConfig(
        grammar=tree_sitter_typescript.language_typescript,
        scope_types=frozenset(
            {
                "function_declaration",
                "method_definition",
                "arrow_function",
                "class_declaration",
            }
        ),
        import_types=frozenset({"import_statement"}),
    )
    js_cfg = _LangConfig(
        grammar=tree_sitter_javascript.language,
        scope_types=frozenset(
            {
                "function_declaration",
                "method_definition",
                "arrow_function",
                "class_declaration",
            }
        ),
        import_types=frozenset({"import_statement"}),
    )
    go_cfg = _LangConfig(
        grammar=tree_sitter_go.language,
        scope_types=frozenset({"function_declaration", "method_declaration"}),
        import_types=frozenset({"import_declaration"}),
    )
    rs_cfg = _LangConfig(
        grammar=tree_sitter_rust.language,
        scope_types=frozenset({"function_item", "impl_item"}),
        import_types=frozenset({"use_declaration"}),
    )
    java_cfg = _LangConfig(
        grammar=tree_sitter_java.language,
        scope_types=frozenset({"method_declaration", "class_declaration"}),
        import_types=frozenset({"import_declaration"}),
    )
    rb_cfg = _LangConfig(
        grammar=tree_sitter_ruby.language,
        scope_types=frozenset({"method", "singleton_method", "class"}),
        import_types=frozenset(),
    )

    return {
        ".py": py_cfg,
        ".ts": ts_cfg,
        ".tsx": ts_cfg,
        ".js": js_cfg,
        ".jsx": js_cfg,
        ".go": go_cfg,
        ".rs": rs_cfg,
        ".java": java_cfg,
        ".rb": rb_cfg,
    }


# Lazily populated on first use.
_dispatch: dict[str, _LangConfig] | None = None

# Parser cache: one Parser per extension.
_parser_cache: dict[str, Parser] = {}


def _get_config(file_ext: str) -> _LangConfig | None:
    """Return the language config for *file_ext*, or ``None`` if unsupported."""
    global _dispatch
    if _dispatch is None:
        try:
            _dispatch = _make_dispatch()
        except Exception:  # noqa: BLE001 — grammar packages not installed
            _dispatch = {}
    return _dispatch.get(file_ext)


def _get_parser(file_ext: str) -> Parser | None:
    """Return a cached ``Parser`` for *file_ext*, or ``None`` if unsupported."""
    if file_ext in _parser_cache:
        return _parser_cache[file_ext]
    cfg = _get_config(file_ext)
    if cfg is None:
        return None
    try:
        lang = Language(cfg.grammar())
        parser = Parser(lang)
        _parser_cache[file_ext] = parser
        return parser
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Scope walking helpers
# ---------------------------------------------------------------------------


def _byte_offset_for_line(source: str, target_line: int) -> int:
    """Return the byte offset of the first character on *target_line* (1-indexed)."""
    lines = source.encode().split(b"\n")
    offset = 0
    for i, line in enumerate(lines):
        if i + 1 == target_line:
            return offset
        offset += len(line) + 1  # +1 for the newline byte
    return offset


def _walk_tree(node: Node) -> list[Node]:
    """Return all nodes in the subtree rooted at *node* (pre-order)."""
    result: list[Node] = [node]
    for child in node.children:
        result.extend(_walk_tree(child))
    return result


def _node_name(node: Node, source_bytes: bytes) -> str | None:
    """Return the text of the first ``name`` child of *node*, or ``None``."""
    for child in node.children:
        if child.type == "name" or child.type == "identifier":
            return source_bytes[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_enclosing_scope(
    source: str,
    file_ext: str,
    target_line: int,
) -> tuple[int, int, str]:
    """Return ``(start_line, end_line, name)`` of the innermost named scope.

    ``start_line`` and ``end_line`` are 1-indexed.

    Falls back to ``(max(1, target_line - 20), target_line + 20, f"line {target_line}")``
    when no enclosing scope is found or the language is unsupported.
    """
    fallback = (max(1, target_line - 20), target_line + 20, f"line {target_line}")

    cfg = _get_config(file_ext)
    if cfg is None:
        return fallback

    try:
        parser = _get_parser(file_ext)
        if parser is None:
            return fallback

        source_bytes = source.encode()
        tree = parser.parse(source_bytes)

        # Compute the byte offset for the target line so we can test containment.
        target_offset = _byte_offset_for_line(source, target_line)

        best_node: Node | None = None
        best_span: int = 0

        for node in _walk_tree(tree.root_node):
            if node.type not in cfg.scope_types:
                continue
            # Does this node's byte range contain the target offset?
            if not (node.start_byte <= target_offset <= node.end_byte):
                continue
            span = node.end_byte - node.start_byte
            if best_node is None or span < best_span:
                best_node = node
                best_span = span

        if best_node is None:
            return fallback

        name = _node_name(best_node, source_bytes) or f"line {target_line}"
        # Tree-sitter uses 0-indexed rows; convert to 1-indexed.
        start_line = best_node.start_point[0] + 1
        end_line = best_node.end_point[0] + 1
        return (start_line, end_line, name)

    except Exception:  # noqa: BLE001
        return fallback


def get_imports(source: str, file_ext: str) -> str:
    """Return the import/require/use block for the given source.

    Returns ``""`` for unsupported languages or on any parse failure.
    """
    cfg = _get_config(file_ext)
    if cfg is None or not cfg.import_types:
        return ""

    try:
        parser = _get_parser(file_ext)
        if parser is None:
            return ""

        source_bytes = source.encode()
        tree = parser.parse(source_bytes)

        import_lines: list[str] = []
        for node in tree.root_node.children:
            if node.type in cfg.import_types:
                text = source_bytes[node.start_byte : node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                import_lines.append(text)

        return "\n".join(import_lines)

    except Exception:  # noqa: BLE001
        return ""
