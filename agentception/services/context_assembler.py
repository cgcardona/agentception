from __future__ import annotations

"""Context assembler — deterministic dispatch-time code context injection.

Replaces the LLM-based planning loop with a zero-LLM Python function that:

1. Extracts targeted search queries from the issue body (backtick-wrapped code
   symbols, file paths, gap descriptions) rather than using raw body slices.
2. Runs up to 5 targeted Qdrant queries in parallel.
3. Merges results with the Qdrant matches already computed at dispatch time.
4. For each unique match, uses tree-sitter (via ``tree_sitter_scope``) to
   extract the exact enclosing function or class scope for Python, TypeScript,
   Go, Rust, Java, JavaScript, and Ruby — not just a raw 800-char chunk.
5. Prepends the relevant import statements from each file so the developer
   can reason about types without reading entire files.

Total elapsed time: ~300 ms (parallel Qdrant + local tree-sitter parsing, zero LLM calls).
Compare with the old LLM planner loop: 30–90 s, 8–16 turns, 1–2 API calls.
"""

import asyncio
import logging
import re
from pathlib import Path

from agentception.services.code_indexer import SearchMatch, search_codebase

logger = logging.getLogger(__name__)

_MAX_SCOPE_CHARS: int = 3_000    # max chars per extracted scope body
_MAX_SCOPES: int = 12            # cap on number of scopes to include
_MAX_CONTEXT_CHARS: int = 30_000 # hard cap on total assembled output

# Files named explicitly in an issue body are injected verbatim when they are
# ≤ this many lines.  Larger files are left for the agent to search selectively.
_MAX_INJECT_LINES: int = 400

# Regex that matches a backtick-wrapped code reference in Markdown.
# Captures the inner text (no newlines, 2–80 chars).
_RE_BACKTICK = re.compile(r"`([^`\n]{2,80})`")

# Regex that matches what looks like a file path (contains a '/' and a '.' in the last segment).
_RE_FILE_PATH = re.compile(r"^[\w./\-]+/[\w.\-]+$")

# Heuristic: a backtick-wrapped item is a code artifact (not prose) when it
# contains a path separator, an underscore, or starts with an uppercase letter
# (PascalCase class / type name), and does not start with $ or # (shell vars /
# comments).
_RE_CODE_ARTIFACT = re.compile(r"[_/A-Z]")


def _extract_code_queries(issue_title: str, issue_body: str) -> list[str]:
    """Derive up to 5 targeted Qdrant queries from an issue title and body.

    Strategy (in order of signal quality):
    1. The issue title — the single most signal-dense summary.
    2. Backtick-wrapped code symbols — function names, file paths, class names.
       These are partitioned into two batches so each query stays focused.
    3. The "Files to touch" section — explicit, high-precision file paths.
    4. The first "Gap" description — the primary technical concept for the task.

    This replaces the previous approach of using raw ``issue_body[:400]`` and
    ``issue_body[-400:]`` slices, which for most issues contain prose context /
    acceptance-criteria boilerplate rather than concrete code identifiers.
    """
    queries: list[str] = []

    # 1. Issue title
    if issue_title:
        queries.append(issue_title[:300])

    # 2. Backtick-wrapped code symbols
    raw_refs = _RE_BACKTICK.findall(issue_body)
    symbol_refs: list[str] = []
    seen_syms: set[str] = set()
    for ref in raw_refs:
        if (
            ref not in seen_syms
            and _RE_CODE_ARTIFACT.search(ref)
            and not ref.startswith(("$", "#"))
        ):
            seen_syms.add(ref)
            symbol_refs.append(ref)

    if symbol_refs:
        # First batch: up to 8 symbols (most likely to appear early in the body)
        queries.append(" ".join(symbol_refs[:8]))
        if len(symbol_refs) > 8:
            queries.append(" ".join(symbol_refs[8:16]))

    # 3. "Files to touch" section (explicit file paths)
    files_match = re.search(
        r"##\s*Files?\s+to\s+touch.*?\n((?:[-*•]\s+`[^\n]+\n?)+)",
        issue_body,
        re.IGNORECASE,
    )
    if files_match:
        file_paths = _RE_BACKTICK.findall(files_match.group(0))
        # Keep only file-path-looking items (contain a '/')
        file_paths = [p for p in file_paths if "/" in p]
        if file_paths:
            queries.append(" ".join(file_paths[:6]))

    # 4. First "Gap" description as a concept query (if we still have room)
    if len(queries) < 5:
        gap_match = re.search(
            r"##\s*Gap\s+\d+[^\n]*\n+(.*?)(?=\n##|\Z)",
            issue_body,
            re.DOTALL,
        )
        if gap_match:
            gap_text = gap_match.group(1).strip()
            # Strip Markdown formatting for a cleaner semantic query.
            # Do NOT strip underscores — they are part of Python identifiers.
            gap_text = re.sub(r"[`*#]", "", gap_text)[:400]
            if gap_text:
                queries.append(gap_text)

    return queries[:5]  # hard cap at 5 parallel queries


def _extract_named_file_paths(issue_body: str) -> list[str]:
    """Return file paths explicitly named in backticks inside the issue body.

    Only returns items that look like relative file paths (contain ``/`` and a
    ``.`` extension in the last segment).  Paths are deduplicated and ordered by
    first appearance.
    """
    seen: set[str] = set()
    paths: list[str] = []
    for ref in _RE_BACKTICK.findall(issue_body):
        if ref in seen:
            continue
        seen.add(ref)
        # Must look like agentception/foo/bar.py  (has '/' and extension)
        if "/" in ref and _RE_FILE_PATH.match(ref):
            paths.append(ref)
    return paths


def _read_named_file(worktree_path: Path, rel_path: str) -> tuple[str, str]:
    """Read *rel_path* from the worktree and return ``(rel_path, content)``.

    Returns ``("", "")`` when the file is absent, binary, or exceeds
    ``_MAX_INJECT_LINES`` lines.  Designed to run via ``asyncio.to_thread``.
    """
    full = worktree_path / rel_path
    if not full.exists() or not full.is_file():
        return ("", "")
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("", "")
    lines = text.splitlines()
    if len(lines) > _MAX_INJECT_LINES:
        # File is too large to inject whole — the agent should use search_codebase.
        return ("", "")
    return (rel_path, text)


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
    from agentception.services.tree_sitter_scope import get_enclosing_scope, get_imports

    path = worktree_path / file_rel
    if not path.exists():
        return ("", "")
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("", "")

    file_ext = Path(file_rel).suffix
    src_lines = source.splitlines(keepends=True)
    start_line, end_line, scope_name = get_enclosing_scope(source, file_ext, target_line)
    scope_body = "".join(src_lines[start_line - 1 : end_line])[:_MAX_SCOPE_CHARS]
    imports = get_imports(source, file_ext)

    # Choose a language hint for the fenced code block.
    _EXT_TO_LANG: dict[str, str] = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".rb": "ruby",
    }
    lang = _EXT_TO_LANG.get(file_ext, "")

    parts: list[str] = []
    if imports:
        parts.append(f"```{lang}\n{imports}\n```")
    parts.append(f"```{lang}\n{scope_body}\n```")

    # Use "— `name`" suffix when we found a real scope; plain "(line N)" otherwise.
    if scope_name.startswith("line "):
        label = f"`{file_rel}` ({scope_name})"
    else:
        label = f"`{file_rel}` — `{scope_name}`"

    return (label, "\n\n".join(parts))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def assemble_developer_context(
    issue_title: str,
    issue_body: str,
    worktree_path: Path,
    existing_matches: list[SearchMatch],
) -> str:
    """Build a rich code context block for the developer — zero LLM calls.

    Runs 3 targeted Qdrant queries in parallel based on the issue title and
    body sections, merges and deduplicates the results with *existing_matches*
    already computed at dispatch time, then extracts the exact enclosing AST
    scope body (function or class) for each unique match.

    The resulting string is appended to the developer's task briefing so it
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
    queries = _extract_code_queries(issue_title, issue_body)
    logger.info(
        "✅ context_assembler: %d queries derived — %s",
        len(queries),
        [q[:60] for q in queries],
    )

    # ------------------------------------------------------------------
    # Phase 0 — inject full content of files explicitly named in the issue.
    # These are the highest-signal inputs: the spec literally tells the agent
    # which files to modify, so we hand them over verbatim rather than making
    # the agent search/read them iteratively.
    # ------------------------------------------------------------------
    named_paths = _extract_named_file_paths(issue_body)
    named_file_reads = await asyncio.gather(
        *[asyncio.to_thread(_read_named_file, worktree_path, p) for p in named_paths],
    )

    named_sections: list[str] = []
    injected_files: set[str] = set()
    named_chars = 0
    for rel_path, content in named_file_reads:
        if not rel_path or not content:
            continue
        ext = Path(rel_path).suffix
        lang = "python" if ext == ".py" else ""
        section = f"### `{rel_path}` _(full file — {len(content.splitlines())} lines)_\n\n```{lang}\n{content}\n```"
        if named_chars + len(section) > _MAX_CONTEXT_CHARS // 2:
            # Cap named-file injection at half the total budget so Qdrant
            # results still have room.
            break
        named_sections.append(section)
        injected_files.add(rel_path)
        named_chars += len(section)

    if named_sections:
        logger.info(
            "✅ context_assembler: injected %d named file(s) (%d chars): %s",
            len(named_sections),
            named_chars,
            list(injected_files),
        )

    # ------------------------------------------------------------------
    # Phase 1 — Qdrant semantic search for additional relevant scopes.
    # ------------------------------------------------------------------
    all_matches: list[SearchMatch] = list(existing_matches)

    if queries:
        try:
            gathered = await asyncio.gather(
                *[search_codebase(q, n_results=8) for q in queries],
                return_exceptions=True,
            )
            for item in gathered:
                if isinstance(item, list):
                    all_matches.extend(item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ context_assembler: parallel search failed — %s", exc)

    # Deduplicate by (file, start_line), preserving insertion order so that
    # the already-computed existing_matches (highest-confidence hits) appear first.
    # Skip files already injected verbatim — the agent has the full content.
    seen: set[tuple[str, int]] = set()
    unique: list[SearchMatch] = []
    for m in all_matches:
        if m["file"] in injected_files:
            continue
        key = (m["file"], m["start_line"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    qdrant_sections: list[str] = []
    qdrant_chars = 0
    remaining_budget = _MAX_CONTEXT_CHARS - named_chars
    for m in unique[:_MAX_SCOPES]:
        label, code_block = await asyncio.to_thread(
            _scope_section, worktree_path, m["file"], m["start_line"]
        )
        if not label or not code_block:
            continue
        section = f"### {label}\n\n{code_block}"
        if qdrant_chars + len(section) > remaining_budget:
            break
        qdrant_sections.append(section)
        qdrant_chars += len(section)

    all_sections = named_sections + qdrant_sections
    if not all_sections:
        return ""

    total_chars = named_chars + qdrant_chars
    logger.info(
        "✅ context_assembler: assembled %d section(s) (%d chars, %d named + %d qdrant) worktree=%s",
        len(all_sections),
        total_chars,
        len(named_sections),
        len(qdrant_sections),
        worktree_path.name,
    )

    parts: list[str] = []
    if named_sections:
        parts.append(
            "## Pre-loaded Files\n\n"
            "_These files are named in your task spec. Do NOT re-read them — "
            "the full content is already here._\n\n"
            + "\n\n".join(named_sections)
        )
    if qdrant_sections:
        parts.append(
            "## Pre-extracted Code Context\n\n"
            "_Exact function/class scope bodies assembled at dispatch time. "
            "No file reads needed — start implementing directly._\n\n"
            + "\n\n".join(qdrant_sections)
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Backward-compatibility alias — remove once all call-sites use the new name.
# ---------------------------------------------------------------------------
assemble_executor_context = assemble_developer_context
