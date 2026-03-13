from __future__ import annotations
import tree_sitter_python
import tree_sitter_typescript
import tree_sitter_javascript
import tree_sitter_go
import tree_sitter_rust
import tree_sitter_java
import tree_sitter_ruby
from tree_sitter import Language, Parser

def test_all_grammars_load() -> None:
    cases: list[tuple[object, str]] = [
        (tree_sitter_python, "language"),
        (tree_sitter_typescript, "language_typescript"),
        (tree_sitter_javascript, "language"),
        (tree_sitter_go, "language"),
        (tree_sitter_rust, "language"),
        (tree_sitter_java, "language"),
        (tree_sitter_ruby, "language"),
    ]
    for lang_mod, fn_name in cases:
        Language(getattr(lang_mod, fn_name)())  # must not raise
