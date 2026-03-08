"""File-system tools exposed to the agent loop.

Each public function returns ``{"ok": True, ...}`` on success and
``{"ok": False, "error": "<message>"}`` on failure so the model always
receives structured feedback rather than a Python exception traceback.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum bytes read from a single file before truncation.
_MAX_READ_BYTES = 131_072  # 128 KiB


def read_file(path: str | Path) -> dict[str, object]:
    """Return the text content of *path*.

    Args:
        path: File to read.  Relative paths are resolved from the caller's cwd.

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


def write_file(path: str | Path, content: str) -> dict[str, object]:
    """Write *content* to *path*, creating parent directories as needed.

    Args:
        path: Destination path.
        content: Text to write (UTF-8).

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
    return {"ok": True, "bytes_written": bytes_written}


def list_directory(path: str | Path) -> dict[str, object]:
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

    return {"ok": True, "entries": entries}


async def search_text(
    pattern: str,
    directory: str | Path,
    *,
    n_results: int = 30,
) -> dict[str, object]:
    """Search *directory* for lines matching *pattern* using ripgrep.

    Uses ``rg`` (ripgrep) for fast, .gitignore-aware searching.  Falls back
    to an error result when ``rg`` is not on ``PATH``.

    Args:
        pattern: Regex or literal pattern forwarded verbatim to ``rg``.
        directory: Root directory to search.
        n_results: Maximum number of matching lines to return.

    Returns:
        ``{"ok": True, "matches": str}`` — rg output (at most *n_results*
        lines) — or ``{"ok": False, "error": str}`` on failure.
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
            "--max-count",
            str(n_results),
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

    return {"ok": True, "matches": output or "(no matches)"}
