"""File-system tools exposed to the agent loop.

Each public function returns ``{"ok": True, ...}`` on success and
``{"ok": False, "error": "<message>"}`` on failure so the model always
receives structured feedback rather than a Python exception traceback.
"""

from __future__ import annotations

import ast as _ast
import logging
import re as _re
from collections.abc import Mapping
from pathlib import Path
from typing import Union

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from agentception.db.activity_events import persist_activity_event
from agentception.config import settings
from agentception.types import JsonValue

logger = logging.getLogger(__name__)


def _shorten_path(abs_path: Path | str, run_id: str) -> str:
    """Strip the absolute worktree prefix and return the repo-relative path.

    For example, ``/worktrees/issue-941/agentception/db/models.py`` with
    ``run_id="issue-941"`` returns ``"agentception/db/models.py"``.

    Falls back to the string representation of *abs_path* when the prefix
    cannot be stripped (e.g. the path is already relative).
    """
    p = Path(abs_path)
    worktree_root = Path(settings.worktrees_dir) / run_id
    try:
        return str(p.relative_to(worktree_root))
    except ValueError:
        return str(p)


def _emit_activity(
    session: Union[Session, AsyncSession],
    run_id: str,
    subtype: str,
    payload: Mapping[str, str | int | float | bool | None],
) -> None:
    """Persist one activity event, catching and logging any DB error.

    Wraps ``persist_activity_event`` so that a database failure never
    propagates to the file-tool caller.
    """
    try:
        persist_activity_event(session, run_id, subtype, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ activity event persist failed (subtype=%s run_id=%s): %s",
            subtype,
            run_id,
            exc,
        )

# Maximum bytes read from a single file before truncation.
_MAX_READ_BYTES = 131_072  # 128 KiB

# Matches class/def header lines — used by insert_after_in_file to prevent
# inserting content after a class or function signature (which would break
# the structure of the class/function body).
_CLASS_DEF_RE: _re.Pattern[str] = _re.compile(r"^\s*(class|def)\s+\w+")


def read_file(path: str | Path) -> dict[str, JsonValue]:
    """Return the text content of *path*.

    Args:
        path: File to read.  Pre-resolved to the worktree root by the dispatcher.

    Returns:
        ``{"ok": True, "content": str, "truncated": bool}`` on success, or
        ``{"ok": False, "error": str}`` when the file cannot be read.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        logger.warning("⚠️ read_file — not found: %s", p)
        return {"ok": False, "error": f"File not found: {p}"}
    except PermissionError:
        logger.warning("⚠️ read_file — permission denied: %s", p)
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        logger.warning("⚠️ read_file — OS error: %s", exc)
        return {"ok": False, "error": str(exc)}

    truncated = len(raw) > _MAX_READ_BYTES
    if truncated:
        raw = raw[:_MAX_READ_BYTES]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "error": f"Decode error: {exc}"}

    return {"ok": True, "content": text, "truncated": truncated}


def write_file(
    path: str | Path,
    content: str,
    *,
    run_id: str | None = None,
    session: Union[Session, AsyncSession] | None = None,
) -> dict[str, JsonValue]:
    """Write *content* to *path*, creating parent directories as needed.

    Args:
        path: Destination path.
        content: Text to write (UTF-8).
        run_id: Agent run ID used to shorten the path in the activity event.
            When ``None``, no activity event is persisted.
        session: Open SQLAlchemy session for persisting the activity event.
            When ``None``, no activity event is persisted.

    Returns:
        ``{"ok": True, "bytes_written": int}`` on success, or
        ``{"ok": False, "error": str}`` on failure.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except PermissionError:
        logger.warning("⚠️ write_file — permission denied: %s", p)
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        logger.warning("⚠️ write_file — OS error: %s", exc)
        return {"ok": False, "error": str(exc)}

    bytes_written = len(content.encode("utf-8"))
    logger.info("✅ write_file — %s (%d bytes)", p, bytes_written)
    # activity event — see docs/reference/activity-events.md
    if run_id is not None and session is not None:
        _emit_activity(session, run_id, "file_written", {
            "path": _shorten_path(p, run_id),
            "byte_count": bytes_written,
        })
    return {"ok": True, "bytes_written": bytes_written}


def list_directory(path: str | Path) -> dict[str, JsonValue]:
    """Return a sorted list of entries in *path*.

    Args:
        path: Directory to list.

    Returns:
        ``{"ok": True, "entries": list[str]}`` — each entry is a relative
        name, with a trailing ``/`` for directories.  Returns an error dict
        when the path is not a directory or cannot be accessed.
    """
    p = Path(path)
    try:
        if not p.is_dir():
            return {"ok": False, "error": f"Not a directory: {p}"}
        entries = sorted(
            (f"{child.name}/" if child.is_dir() else child.name)
            for child in p.iterdir()
        )
    except PermissionError:
        logger.warning("⚠️ list_directory — permission denied: %s", p)
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        logger.warning("⚠️ list_directory — OS error: %s", exc)
        return {"ok": False, "error": str(exc)}

    entries_jv: list[JsonValue] = []
    entries_jv.extend(entries)
    return {"ok": True, "entries": entries_jv}


def replace_in_file(
    path: str | Path,
    old_string: str,
    new_string: str,
    *,
    allow_multiple: bool = False,
    run_id: str | None = None,
    session: Union[Session, AsyncSession] | None = None,
) -> dict[str, JsonValue]:
    """Replace an exact string in *path* with *new_string*.

    Safer than ``write_file`` for targeted edits because only the matched
    region changes; the rest of the file is untouched.  If the anchor text
    appears more than once and ``allow_multiple`` is ``False``, the call
    fails rather than making an ambiguous edit.

    Args:
        path: File to edit.
        old_string: Exact text to find (must be unique unless allow_multiple).
        new_string: Replacement text.
        allow_multiple: When ``True``, replace every occurrence.  When
            ``False`` (default), fail if the anchor matches more than once.

    Returns:
        ``{"ok": True, "replacements": int}`` on success, or
        ``{"ok": False, "error": str}`` on any failure.
    """
    p = Path(path)
    try:
        original = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("⚠️ replace_in_file — not found: %s", p)
        return {"ok": False, "error": f"File not found: {p}"}
    except PermissionError:
        logger.warning("⚠️ replace_in_file — permission denied: %s", p)
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        logger.warning("⚠️ replace_in_file — OS error: %s", exc)
        return {"ok": False, "error": str(exc)}

    count = original.count(old_string)
    if count == 0:
        return {"ok": False, "error": "replace_in_file: old_string not found in file"}
    if count > 1 and not allow_multiple:
        return {
            "ok": False,
            "error": (
                f"replace_in_file: old_string matches {count} times — "
                "use a longer anchor to make it unique, "
                "or pass allow_multiple=true to replace all occurrences"
            ),
        }

    updated = original.replace(old_string, new_string)
    try:
        p.write_text(updated, encoding="utf-8")
    except PermissionError:
        logger.warning("⚠️ replace_in_file — permission denied writing: %s", p)
        return {"ok": False, "error": f"Permission denied writing: {p}"}
    except OSError as exc:
        logger.warning("⚠️ replace_in_file — OS write error: %s", exc)
        return {"ok": False, "error": str(exc)}

    replacements = count if allow_multiple else 1
    logger.info("✅ replace_in_file — %s (%d replacement(s))", p, replacements)
    # activity event — see docs/reference/activity-events.md
    if run_id is not None and session is not None:
        _emit_activity(session, run_id, "file_replaced", {
            "path": _shorten_path(p, run_id),
            "replacement_count": replacements,
        })
    return {"ok": True, "replacements": replacements}


def read_file_lines(
    path: str | Path,
    start_line: int,
    end_line: int,
    *,
    run_id: str | None = None,
    session: Union[Session, AsyncSession] | None = None,
) -> dict[str, JsonValue]:
    """Return lines *start_line* through *end_line* (1-indexed, inclusive) from *path*.

    Cheaper than ``read_file`` for large files when only a specific region is
    needed.  Out-of-range bounds are clamped to the actual file length rather
    than returning an error.

    Args:
        path: File to read.  Relative paths are resolved from the caller's cwd.
        start_line: First line to return (1-indexed).
        end_line: Last line to return (1-indexed, inclusive).

    Returns:
        ``{"ok": True, "content": str, "start_line": int, "end_line": int,
        "total_lines": int}`` on success, or ``{"ok": False, "error": str}``
        on any failure.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("⚠️ read_file_lines — not found: %s", p)
        return {"ok": False, "error": f"File not found: {p}"}
    except PermissionError:
        logger.warning("⚠️ read_file_lines — permission denied: %s", p)
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        logger.warning("⚠️ read_file_lines — OS error: %s", exc)
        return {"ok": False, "error": str(exc)}

    lines = text.splitlines(keepends=True)
    total = len(lines)

    clamped_start = max(1, start_line)
    clamped_end = min(total, end_line)

    if clamped_start > clamped_end:
        return {
            "ok": False,
            "error": (
                f"read_file_lines: start_line {start_line} is beyond end_line "
                f"{end_line} after clamping to file length {total}"
            ),
        }

    content = "".join(lines[clamped_start - 1 : clamped_end])
    logger.info(
        "✅ read_file_lines — %s lines %d-%d/%d", p, clamped_start, clamped_end, total
    )
    # activity event — see docs/reference/activity-events.md
    if run_id is not None and session is not None:
        # Build a short content excerpt for the inspector detail panel.
        # Cap at 10 lines and 400 chars so the payload stays lightweight.
        _PREVIEW_MAX_LINES = 10
        _PREVIEW_MAX_CHARS = 400
        preview_lines = lines[clamped_start - 1 : clamped_start - 1 + _PREVIEW_MAX_LINES]
        raw_preview = "".join(preview_lines)
        if len(raw_preview) > _PREVIEW_MAX_CHARS:
            raw_preview = raw_preview[:_PREVIEW_MAX_CHARS] + "…"
        _emit_activity(session, run_id, "file_read", {
            "path": _shorten_path(p, run_id),
            "start_line": clamped_start,
            "end_line": clamped_end,
            "total_lines": total,
            "content_preview": raw_preview,
        })
    return {
        "ok": True,
        "content": content,
        "start_line": clamped_start,
        "end_line": clamped_end,
        "total_lines": total,
    }


def insert_after_in_file(
    path: str | Path,
    anchor: str,
    new_content: str,
    *,
    run_id: str | None = None,
    session: Union[Session, AsyncSession] | None = None,
) -> dict[str, JsonValue]:
    """Insert *new_content* immediately after the first occurrence of *anchor* in *path*.

    Complements ``replace_in_file`` for pure-insertion tasks where the anchor
    text should be preserved.  Fails if the anchor is not found or appears more
    than once, so the caller must use a unique anchor.

    Args:
        path: File to edit.
        anchor: Exact text that marks the insertion point.  Must appear exactly
            once in the file.
        new_content: Text to insert immediately after *anchor*.

    Returns:
        ``{"ok": True, "inserted_at": int}`` — byte offset of the insertion
        point — or ``{"ok": False, "error": str}`` on any failure.
    """
    p = Path(path)
    try:
        original = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("⚠️ insert_after_in_file — not found: %s", p)
        return {"ok": False, "error": f"File not found: {p}"}
    except PermissionError:
        logger.warning("⚠️ insert_after_in_file — permission denied: %s", p)
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        logger.warning("⚠️ insert_after_in_file — OS error: %s", exc)
        return {"ok": False, "error": str(exc)}

    count = original.count(anchor)
    if count == 0:
        return {"ok": False, "error": "insert_after_in_file: anchor not found in file"}
    if count > 1:
        return {
            "ok": False,
            "error": (
                f"insert_after_in_file: anchor matches {count} times — "
                "use a longer anchor to make it unique"
            ),
        }

    insert_pos = original.index(anchor) + len(anchor)

    # Guard: refuse to insert immediately after a bare class/function definition
    # header.  A header line ends with ":" and the following non-blank line is
    # indented — inserting between them detaches the body from its header and
    # produces a SyntaxError.  The caller must use an anchor that ends inside
    # the body (e.g. the last method's closing line) rather than on the header.
    anchor_end_line = original[:insert_pos].rpartition("\n")[2]
    stripped_header = anchor_end_line.rstrip()
    if stripped_header.endswith(":") and _CLASS_DEF_RE.match(stripped_header):
        for _line in original[insert_pos:].splitlines():
            if not _line.strip():
                continue  # skip blank lines
            if _line[0] in (" ", "\t"):
                return {
                    "ok": False,
                    "error": (
                        "insert_after_in_file: anchor ends on a class/function "
                        "definition header whose body immediately follows. "
                        "Inserting here would detach the body from its header "
                        "and produce a SyntaxError. "
                        "Use an anchor that ends inside the body — for example, "
                        "the last line of the final method — so the new content "
                        "is appended after the complete class/function."
                    ),
                }
            break  # next non-blank line is not indented — safe to insert

    updated = original[:insert_pos] + new_content + original[insert_pos:]

    try:
        p.write_text(updated, encoding="utf-8")
    except PermissionError:
        logger.warning("⚠️ insert_after_in_file — permission denied writing: %s", p)
        return {"ok": False, "error": f"Permission denied writing: {p}"}
    except OSError as exc:
        logger.warning("⚠️ insert_after_in_file — OS write error: %s", exc)
        return {"ok": False, "error": str(exc)}

    logger.info("✅ insert_after_in_file — %s (inserted at byte %d)", p, insert_pos)
    # activity event — see docs/reference/activity-events.md
    if run_id is not None and session is not None:
        _emit_activity(session, run_id, "file_inserted", {
            "path": _shorten_path(p, run_id),
        })
    return {"ok": True, "inserted_at": insert_pos}


def _truncate_rg_output(output: str, n_results: int) -> str:
    """Truncate ripgrep ``--heading`` output to at most *n_results* match lines.

    In ``--heading`` mode rg emits a file-path header line, then numbered
    match lines (``<lineno>:<content>``), then a blank separator between
    file groups.  This function counts only the numbered match lines and
    stops (dropping trailing headers/blanks) once the limit is reached so
    the total returned is bounded regardless of how many files matched.
    """
    kept: list[str] = []
    match_count = 0
    for line in output.split("\n"):
        # Numbered match lines in --heading mode start with digits followed
        # by ":" (e.g. "42:def foo():").  File headers and blank separators
        # do not match this pattern.
        if line and line[0].isdigit() and ":" in line:
            if match_count >= n_results:
                break
            match_count += 1
        kept.append(line)
    return "\n".join(kept).rstrip()


async def search_text(
    pattern: str,
    directory: str | Path,
    *,
    n_results: int = 30,
) -> dict[str, JsonValue]:
    """Search *directory* for lines matching *pattern* using ripgrep.

    Uses ``rg`` (ripgrep) for fast, .gitignore-aware searching.  Falls back
    to an error result when ``rg`` is not on ``PATH``.

    Args:
        pattern: Regex or literal pattern forwarded verbatim to ``rg``.
        directory: Root directory to search.
        n_results: Maximum total matching lines to return across all files.

    Returns:
        ``{"ok": True, "matches": str}`` — rg output (at most *n_results*
        total match lines) — or ``{"ok": False, "error": str}`` on failure.
    """
    import asyncio

    d = Path(directory)
    if not d.exists():
        return {"ok": False, "error": f"Directory does not exist: {d}"}

    try:
        proc = await asyncio.create_subprocess_exec(
            "rg",
            "--heading",
            "--line-number",
            pattern,
            str(d),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except FileNotFoundError:
        return {"ok": False, "error": "rg (ripgrep) not found on PATH"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "search_text timed out after 30s"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    output = stdout.decode("utf-8", errors="replace")
    # rg exits 1 when no matches found — that is not an error for us.
    if proc.returncode not in (0, 1):
        err_text = stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": f"rg failed (exit {proc.returncode}): {err_text}"}

    truncated = _truncate_rg_output(output, n_results)
    return {"ok": True, "matches": truncated or "(no matches)"}


# ---------------------------------------------------------------------------
# Symbol-aware navigation tools
# ---------------------------------------------------------------------------


def _find_symbol_lines_py(text: str, symbol_name: str) -> tuple[int, int] | None:
    """Return (start_line, end_line) for *symbol_name* using the Python AST.

    Walks every function/class definition in the parsed tree and returns the
    first match.  Includes decorator lines in the start.  Returns ``None`` on
    a parse error or when the symbol is not found.
    """
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return None

    for node in _ast.walk(tree):
        if not isinstance(
            node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)
        ):
            continue
        if node.name != symbol_name:
            continue
        end_line: int | None = getattr(node, "end_lineno", None)
        if end_line is None:
            continue
        start_line = node.lineno
        if node.decorator_list:
            start_line = min(d.lineno for d in node.decorator_list)
        return start_line, end_line

    return None


def read_symbol(path: str | Path, symbol_name: str) -> dict[str, JsonValue]:
    """Return the complete body of a function or class by name.

    For ``.py`` files, uses the Python AST to find exact symbol boundaries
    (including decorators).  Returns the full definition so the model can act
    on it without a follow-up ``read_file_lines`` call.

    Args:
        path: Source file to search.
        symbol_name: Exact function or class name (e.g. ``_truncate_tool_results``).

    Returns:
        ``{"ok": True, "content": str, "start_line": int, "end_line": int,
        "total_lines": int}`` — the symbol body with its line range.  Or
        ``{"ok": False, "error": str}`` when the symbol is not found or the
        file cannot be read.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"ok": False, "error": f"File not found: {p}"}
    except PermissionError:
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    lines = text.splitlines(keepends=True)
    total = len(lines)

    if p.suffix == ".py":
        bounds = _find_symbol_lines_py(text, symbol_name)
        if bounds is not None:
            start, end = bounds
            content = "".join(lines[start - 1 : end])
            logger.info("✅ read_symbol — %s::%s lines %d-%d", p, symbol_name, start, end)
            return {
                "ok": True,
                "content": content,
                "start_line": start,
                "end_line": end,
                "total_lines": total,
            }
        # AST found nothing — fall through to string scan below.

    # Non-Python or AST miss: scan for `def name` / `class name`.
    # LIMITATION: this heuristic uses indentation-based end-detection, which
    # works for Python-style files but NOT for brace-delimited languages such
    # as TypeScript or JavaScript.  For .ts/.js files the heuristic returns
    # only the opening line (the `{` body is not indented relative to the
    # header).  If a TypeScript agent role is added, replace this path with
    # a tree-sitter or AST-based extractor for those file types.
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith(f"def {symbol_name}(") or stripped.startswith(
            f"class {symbol_name}("
        ) or stripped.startswith(f"class {symbol_name}:"):
            # Heuristic end: next non-indented non-empty line at same or
            # lower indent level.  Good enough for most source files.
            base_indent = len(line) - len(stripped)
            end = i
            for j in range(i, total):
                following = lines[j]
                if following.strip() == "":
                    end = j + 1
                    continue
                curr_indent = len(following) - len(following.lstrip())
                if curr_indent <= base_indent and j > i:
                    break
                end = j + 1
            content = "".join(lines[i - 1 : end])
            logger.info("✅ read_symbol — %s::%s lines %d-%d (heuristic)", p, symbol_name, i, end)
            return {
                "ok": True,
                "content": content,
                "start_line": i,
                "end_line": end,
                "total_lines": total,
            }

    return {"ok": False, "error": f"Symbol '{symbol_name}' not found in {p}"}


def read_window(
    path: str | Path,
    center_line: int,
    *,
    before: int = 80,
    after: int = 120,
) -> dict[str, JsonValue]:
    """Read a window of lines centered on *center_line*.

    More ergonomic than ``read_file_lines`` for exploration: plug in a line
    number from search results and receive enough surrounding context to act
    without a follow-up read.  The default window (80 before, 120 after)
    captures most complete function definitions.

    Args:
        path: File to read.
        center_line: 1-indexed line to center the window on.
        before: Lines to include before *center_line* (default 80).
        after: Lines to include after *center_line* (default 120).

    Returns:
        ``{"ok": True, "content": str, "start_line": int, "end_line": int,
        "center_line": int, "total_lines": int}`` or ``{"ok": False, "error": str}``.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"ok": False, "error": f"File not found: {p}"}
    except PermissionError:
        return {"ok": False, "error": f"Permission denied: {p}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    lines = text.splitlines(keepends=True)
    total = len(lines)

    start = max(1, center_line - before)
    end = min(total, center_line + after)
    content = "".join(lines[start - 1 : end])

    logger.info("✅ read_window — %s center=%d (%d-%d/%d)", p, center_line, start, end, total)
    return {
        "ok": True,
        "content": content,
        "start_line": start,
        "end_line": end,
        "center_line": center_line,
        "total_lines": total,
    }


async def find_call_sites(
    symbol_name: str,
    directory: str | Path,
    *,
    n_results: int = 30,
) -> dict[str, JsonValue]:
    """Find all call sites of *symbol_name* using ripgrep.

    Searches for ``symbol_name(`` to catch function calls, plus ``symbol_name``
    in import lines.  Returns file paths, line numbers, and the matching line
    so the model can see usage patterns before editing.

    Args:
        symbol_name: Function or class name to search for.
        directory: Root directory to search (defaults to worktree root).
        n_results: Maximum total matching lines to return across all files.

    Returns:
        ``{"ok": True, "matches": str}`` — ripgrep-formatted output — or
        ``{"ok": False, "error": str}`` on failure.
    """
    import asyncio

    d = Path(directory)
    if not d.exists():
        return {"ok": False, "error": f"Directory does not exist: {d}"}

    # Four patterns cover the main usage forms:
    # 1. Call sites:   symbol_name( or symbol_name  (followed by whitespace)
    # 2. Bare import:  import symbol_name
    # 3. From-import:  from x import symbol_name  (including multi-symbol lines)
    # 4. Type context: symbol_name: / symbol_name[ / symbol_name, / symbol_name)
    #    — covers annotations, generic parameters, and tuple positions
    pattern = (
        rf"\b{symbol_name}[\(\s]"
        rf"|import\s+{symbol_name}\b"
        rf"|from\s+\S+\s+import\b[^#\n]*\b{symbol_name}\b"
        rf"|\b{symbol_name}[:\[,\)]"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "rg",
            "--heading",
            "--line-number",
            "-e",
            pattern,
            str(d),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except FileNotFoundError:
        return {"ok": False, "error": "rg (ripgrep) not found on PATH"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "find_call_sites timed out after 30s"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode not in (0, 1):
        err_text = stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": f"rg failed (exit {proc.returncode}): {err_text}"}

    truncated = _truncate_rg_output(output, n_results)
    logger.info("✅ find_call_sites — %s in %s", symbol_name, d)
    return {"ok": True, "matches": truncated or "(no call sites found)"}
