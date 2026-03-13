"""Regression tests for the AC pre-loader size and extension guards.

These tests exercise ``_build_ac_file_sections`` in isolation — no real
Anthropic calls, no Qdrant, no database.  Each test creates a minimal
temporary directory tree so the guards run against real ``pathlib.Path``
objects without any mocking of filesystem primitives.

Guards under test (from ``agentception/routes/api/dispatch.py``):

- **Size guard**: files whose ``stat().st_size`` exceeds 51 200 bytes (50 KB)
  are skipped with a ``WARNING`` log containing ``"file too large"``.
- **Extension guard**: files with ``.js``, ``.css``, or ``.map`` suffixes, and
  files whose name ends with ``.min.js``, are skipped with a ``WARNING`` log
  containing ``"compiled artifact"``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_build_ac_file_sections(
    worktree_path: Path,
    file_paths: list[str],
) -> list[str]:
    """Import and call ``_build_ac_file_sections`` with no symbol hints."""
    from agentception.routes.api.dispatch import _build_ac_file_sections

    return _build_ac_file_sections(worktree_path, file_paths)


# ---------------------------------------------------------------------------
# test_ac_preloader_skips_large_files
# ---------------------------------------------------------------------------


def test_ac_preloader_skips_large_files(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Files exceeding 50 KB are excluded; small files are included.

    Acceptance criteria:
    - The oversized file's content is absent from the returned sections.
    - The small file's content is present in the returned sections.
    - A WARNING log containing ``"file too large"`` is emitted for the big file.
    """
    # Create a small file (1 024 bytes — well under the 51 200-byte limit).
    small_file = tmp_path / "small.py"
    small_content = "# small file\n" * 10
    small_file.write_text(small_content, encoding="utf-8")

    # Create a large file (52 000 bytes — over the 51 200-byte limit).
    large_file = tmp_path / "large.py"
    large_file.write_bytes(b"x" * 52_000)

    file_paths = ["small.py", "large.py"]

    with caplog.at_level(logging.WARNING, logger="agentception.routes.api.dispatch"):
        sections = _call_build_ac_file_sections(tmp_path, file_paths)

    # The small file must appear in the output.
    combined = "\n".join(sections)
    assert "small.py" in combined, "small.py should be present in pre-loaded sections"

    # The large file must be absent from the output.
    assert "large.py" not in combined, "large.py should be skipped (exceeds 50 KB limit)"

    # A WARNING containing "file too large" must have been emitted.
    large_warnings = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "file too large" in r.message
    ]
    assert large_warnings, (
        "Expected a WARNING log containing 'file too large' for the oversized file"
    )


# ---------------------------------------------------------------------------
# test_ac_preloader_skips_bundle_extensions
# ---------------------------------------------------------------------------


def test_ac_preloader_skips_bundle_extensions(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Files with .js, .css, and .map extensions are excluded; .py files are included.

    Acceptance criteria:
    - ``.js``, ``.css``, and ``.map`` paths are absent from the result.
    - The ``.py`` file is present in the result.
    - A WARNING log containing ``"compiled artifact"`` is emitted for each
      blocked file.
    """
    # Create the candidate files on disk so the extension guard is reached
    # (the size guard only fires after exists()/is_file() checks pass).
    js_file = tmp_path / "bundle.js"
    css_file = tmp_path / "styles.css"
    map_file = tmp_path / "bundle.js.map"
    py_file = tmp_path / "module.py"

    js_file.write_text("console.log('hi');", encoding="utf-8")
    css_file.write_text("body { margin: 0; }", encoding="utf-8")
    map_file.write_text("{}", encoding="utf-8")
    py_file.write_text("# python module\ndef hello(): pass\n", encoding="utf-8")

    file_paths = ["bundle.js", "styles.css", "bundle.js.map", "module.py"]

    with caplog.at_level(logging.WARNING, logger="agentception.routes.api.dispatch"):
        sections = _call_build_ac_file_sections(tmp_path, file_paths)

    combined = "\n".join(sections)

    # Blocked extensions must be absent.
    assert "bundle.js" not in combined, "bundle.js should be skipped (blocked extension)"
    assert "styles.css" not in combined, "styles.css should be skipped (blocked extension)"
    assert "bundle.js.map" not in combined, "bundle.js.map should be skipped (blocked extension)"

    # The Python file must be present.
    assert "module.py" in combined, "module.py should be present in pre-loaded sections"

    # A WARNING containing "compiled artifact" must have been emitted for each
    # blocked file.
    artifact_warnings = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "compiled artifact" in r.message
    ]
    assert len(artifact_warnings) >= 3, (
        f"Expected at least 3 'compiled artifact' warnings, got {len(artifact_warnings)}: "
        f"{artifact_warnings}"
    )


# ---------------------------------------------------------------------------
# test_ac_preloader_skips_min_js
# ---------------------------------------------------------------------------


def test_ac_preloader_skips_min_js(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Files named ``*.min.js`` are blocked even though ``Path.suffix`` returns ``.js``.

    This test explicitly exercises the ``.min.js`` double-suffix branch in the
    extension guard, which is separate from the single-suffix ``.js`` check.
    """
    vendor_min_js = tmp_path / "vendor.min.js"
    vendor_min_js.write_text("/* minified */", encoding="utf-8")

    file_paths = ["vendor.min.js"]

    with caplog.at_level(logging.WARNING, logger="agentception.routes.api.dispatch"):
        sections = _call_build_ac_file_sections(tmp_path, file_paths)

    # The minified JS file must be absent from the output.
    combined = "\n".join(sections)
    assert "vendor.min.js" not in combined, (
        "vendor.min.js should be skipped by the .min.js double-suffix guard"
    )

    # A WARNING containing "compiled artifact" must have been emitted.
    artifact_warnings = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "compiled artifact" in r.message
    ]
    assert artifact_warnings, (
        "Expected a WARNING log containing 'compiled artifact' for vendor.min.js"
    )
