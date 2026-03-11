from __future__ import annotations

"""Context assembler — deterministic dispatch-time code context injection.

Replaces the LLM-based planning loop with a zero-LLM Python function that:

1. Runs 3 targeted Qdrant queries in parallel based on the issue title and body.
2. Merges results with the Qdrant matches already computed at dispatch time.
3. For each unique match, uses Python ``ast`` to extract the exact enclosing
   function or class scope (not just a raw 800-char chunk).
4. Prepends the relevant import statements from each file so the executor
   can reason about types without reading entire files.

Total elapsed time: ~300 ms (parallel Qdrant + local AST parsing, zero LLM calls).
Compare with the old LLM planner loop: 30–90 s, 8–16 turns, 1–2 API calls.
"""

import ast as _ast
import asyncio
import logging
from pathlib import Path

from agentception.services.code_indexer import SearchMatch, search_codebase

logger = logging.getLogger(__name__)

_MAX_SCOPE_CHARS: int = 3_000    # max chars per extracted scope body
_MAX_SCOPES: int = 8             # cap on number of scopes to include
_MAX_CONTEXT_CHARS: int = 24_000 # hard cap on total assembled output


# ---------------------------------------------------------------------------
# Pure-Python AST helpers (no I/O — safe to call in asyncio.to_thread)
# ---------------------------------------------------------------------------


def _ast_imports(source: str) -> str:
    """Return all import/from-import lines from *source* (deduplicated, ordered).

    Skips files with syntax errors (returns ``""``).
    """
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return ""
    lines = source.splitlines(keepends=True)
    seen: set[str] = set()
    result: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            node_end = node.end_lineno or node.lineno
            for i in range(node.lineno - 1, node_end):
                if i < len(lines):
                    line = lines[i]
                    if line not in seen:
                        seen.add(line)
                        result.append(line)
    return "".join(result)


def _ast_enclosing_scope(
    source: str,
    target_line: int,
) -> tuple[int, int, str]:
    """Return ``(start_line, end_line, name)`` of the innermost scope containing *target_line*.

    Lines are 1-indexed.  Falls back to a ±20-line window around *target_line*
    when no enclosing function or class is found (e.g. module-level code).
    """
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        n = target_line
        return (max(1, n - 20), n + 20, f"line {n}")

    best: _ast.FunctionDef | _ast.AsyncFunctionDef | _ast.ClassDef | None = None
    best_span: int = 0
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            continue
        end = node.end_lineno or node.lineno
        if not (node.lineno <= target_line <= end):
            continue
        span = end - node.lineno
        if best is None or span < best_span:
            best = node
            best_span = span

    if best is None:
        n = target_line
        return (max(1, n - 20), n + 20, f"line {n}")
    return (best.lineno, best.end_lineno or best.lineno, best.name)


def _scope_section(
    worktree_path: Path,
    file_rel: str,
    target_line: int,
) -> tuple[str, str]:
    """Extract the enclosing scope + import header for *file_rel* at *target_line*.

    Reads the file from disk and applies AST analysis.  Designed to be called
    via ``asyncio.to_thread`` — performs blocking I/O.

    Returns:
        ``(label, code_block)`` where *label* is a Markdown heading fragment
        (e.g. ``"`agentception/services/foo.py` — `my_function`"``) and
        *code_block* contains fenced code blocks ready to embed in the briefing.
        Returns ``("", "")`` on any I/O or parse failure — callers skip empty pairs.
    """
    path = worktree_path / file_rel
    if not path.exists():
        return ("", "")
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("", "")

    if not file_rel.endswith(".py"):
        src_lines = source.splitlines(keepends=True)
        start = max(0, target_line - 20)
        end = min(len(src_lines), target_line + 20)
        body = "".join(src_lines[start:end])[:_MAX_SCOPE_CHARS]
        return (
            f"`{file_rel}` (line {target_line})",
            f"```\n{body}\n```",
        )

    src_lines = source.splitlines(keepends=True)
    start_line, end_line, scope_name = _ast_enclosing_scope(source, target_line)
    scope_body = "".join(src_lines[start_line - 1 : end_line])[:_MAX_SCOPE_CHARS]
    imports = _ast_imports(source)

    parts: list[str] = []
    if imports:
        parts.append(f"```python\n{imports}\n```")
    parts.append(f"```python\n{scope_body}\n```")

    return (
        f"`{file_rel}` — `{scope_name}`",
        "\n\n".join(parts),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def assemble_executor_context(
    issue_title: str,
    issue_body: str,
    worktree_path: Path,
    existing_matches: list[SearchMatch],
) -> str:
    """Build a rich code context block for the executor — zero LLM calls.

    Runs 3 targeted Qdrant queries in parallel based on the issue title and
    body sections, merges and deduplicates the results with *existing_matches*
    already computed at dispatch time, then extracts the exact enclosing AST
    scope body (function or class) for each unique match.

    The resulting string is appended to the executor's task briefing so it
    starts implementation from turn 1 with precise, relevant code context —
    no file reads needed, no LLM planning loop.

    Args:
        issue_title: GitHub issue title.
        issue_body: Raw Markdown issue body.
        worktree_path: Absolute path to the git worktree on disk.
        existing_matches: Qdrant results already computed at dispatch time
            (reused to avoid duplicate API calls).

    Returns:
        Formatted Markdown string ready to append to the task briefing, or
        ``""`` if no relevant matches were found or all extractions fail.
    """
    queries: list[str] = [
        q
        for q in [
            issue_title[:300],
            issue_body[:400].strip(),
            issue_body[-400:].strip(),
        ]
        if q
    ]

    all_matches: list[SearchMatch] = list(existing_matches)

    if queries:
        try:
            gathered = await asyncio.gather(
                *[search_codebase(q, n_results=5) for q in queries],
                return_exceptions=True,
            )
            for item in gathered:
                if isinstance(item, list):
                    all_matches.extend(item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ context_assembler: parallel search failed — %s", exc)

    # Deduplicate by (file, start_line), preserving insertion order so that
    # the already-computed existing_matches (highest-confidence hits) appear first.
    seen: set[tuple[str, int]] = set()
    unique: list[SearchMatch] = []
    for m in all_matches:
        key = (m["file"], m["start_line"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    sections: list[str] = []
    total_chars = 0
    for m in unique[:_MAX_SCOPES]:
        label, code_block = await asyncio.to_thread(
            _scope_section, worktree_path, m["file"], m["start_line"]
        )
        if not label or not code_block:
            continue
        section = f"### {label}\n\n{code_block}"
        if total_chars + len(section) > _MAX_CONTEXT_CHARS:
            break
        sections.append(section)
        total_chars += len(section)

    if not sections:
        return ""

    logger.info(
        "✅ context_assembler: assembled %d scope sections (%d chars) for worktree=%s",
        len(sections),
        total_chars,
        worktree_path.name,
    )

    return (
        "## Pre-extracted Code Context\n\n"
        "_Exact function/class scope bodies assembled at dispatch time. "
        "No file reads needed — start implementing directly._\n\n"
        + "\n\n".join(sections)
    )
