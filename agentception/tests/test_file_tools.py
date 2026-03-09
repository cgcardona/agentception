"""Unit tests for agentception.tools.file_tools.

Tests run against a real temporary filesystem (``tmp_path`` from pytest).
The ``search_text`` tests mock the ``rg`` subprocess since ripgrep is an
optional system dependency that may not be installed in every environment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.tools.file_tools import (
    insert_after_in_file,
    list_directory,
    read_file,
    read_file_lines,
    replace_in_file,
    search_text,
    write_file,
)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_reads_text_file(self, tmp_path: Path) -> None:
        p = tmp_path / "hello.txt"
        p.write_text("hello world", encoding="utf-8")
        result = read_file(p)
        assert result["ok"] is True
        assert result["content"] == "hello world"
        assert result["truncated"] is False

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        p = tmp_path / "string_path.txt"
        p.write_text("content", encoding="utf-8")
        result = read_file(str(p))
        assert result["ok"] is True
        assert result["content"] == "content"

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        result = read_file(tmp_path / "nonexistent.txt")
        assert result["ok"] is False
        assert "not found" in str(result["error"]).lower()

    def test_truncates_large_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agentception.tools.file_tools._MAX_READ_BYTES", 10)
        p = tmp_path / "big.txt"
        p.write_text("A" * 50, encoding="utf-8")
        result = read_file(p)
        assert result["ok"] is True
        assert result["truncated"] is True
        assert len(str(result["content"])) == 10

    def test_reads_nested_path(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c.txt"
        nested.parent.mkdir(parents=True)
        nested.write_text("nested", encoding="utf-8")
        result = read_file(nested)
        assert result["ok"] is True
        assert result["content"] == "nested"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def test_writes_new_file(self, tmp_path: Path) -> None:
        p = tmp_path / "out.txt"
        result = write_file(p, "written content")
        assert result["ok"] is True
        assert result["bytes_written"] == len("written content".encode())
        assert p.read_text() == "written content"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        p = tmp_path / "deep" / "nested" / "file.txt"
        result = write_file(p, "deep")
        assert result["ok"] is True
        assert p.read_text() == "deep"

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "overwrite.txt"
        p.write_text("original")
        result = write_file(p, "replaced")
        assert result["ok"] is True
        assert p.read_text() == "replaced"

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        p = tmp_path / "str.txt"
        result = write_file(str(p), "via string")
        assert result["ok"] is True
        assert p.read_text() == "via string"

    def test_writes_empty_string(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.txt"
        result = write_file(p, "")
        assert result["ok"] is True
        assert result["bytes_written"] == 0
        assert p.read_text() == ""


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------


class TestListDirectory:
    def test_lists_files_and_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("x")
        (tmp_path / "subdir").mkdir()
        result = list_directory(tmp_path)
        assert result["ok"] is True
        entries = result["entries"]
        assert isinstance(entries, list)
        assert "file.txt" in entries
        assert "subdir/" in entries

    def test_entries_are_sorted(self, tmp_path: Path) -> None:
        (tmp_path / "z.txt").write_text("z")
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "m.txt").write_text("m")
        result = list_directory(tmp_path)
        assert result["ok"] is True
        raw_entries = result["entries"]
        assert isinstance(raw_entries, list)
        entries: list[str] = [str(e) for e in raw_entries]
        assert entries == sorted(entries)

    def test_directories_suffixed_with_slash(self, tmp_path: Path) -> None:
        (tmp_path / "mydir").mkdir()
        result = list_directory(tmp_path)
        assert result["ok"] is True
        raw_entries = result["entries"]
        assert isinstance(raw_entries, list)
        assert "mydir/" in raw_entries

    def test_not_a_directory_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "file.txt"
        p.write_text("x")
        result = list_directory(p)
        assert result["ok"] is False
        assert "not a directory" in str(result["error"]).lower()

    def test_missing_directory_returns_error(self, tmp_path: Path) -> None:
        result = list_directory(tmp_path / "absent")
        assert result["ok"] is False

    def test_empty_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        result = list_directory(d)
        assert result["ok"] is True
        assert result["entries"] == []


# ---------------------------------------------------------------------------
# replace_in_file
# ---------------------------------------------------------------------------


class TestReplaceInFile:
    def test_replaces_unique_string(self, tmp_path: Path) -> None:
        p = tmp_path / "doc.md"
        p.write_text("| OLD_ROW | value |\n| NEXT_ROW | val |\n")
        result = replace_in_file(p, "| OLD_ROW | value |", "| NEW_ROW | value |")
        assert result["ok"] is True
        assert result["replacements"] == 1
        assert "NEW_ROW" in p.read_text()
        assert "OLD_ROW" not in p.read_text()

    def test_old_string_not_found_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "file.txt"
        p.write_text("hello world")
        result = replace_in_file(p, "does not exist", "replacement")
        assert result["ok"] is False
        assert "not found" in str(result["error"]).lower()

    def test_multiple_matches_without_flag_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "file.txt"
        p.write_text("foo bar foo baz foo")
        result = replace_in_file(p, "foo", "qux")
        assert result["ok"] is False
        assert "3" in str(result["error"])

    def test_allow_multiple_replaces_all(self, tmp_path: Path) -> None:
        p = tmp_path / "file.txt"
        p.write_text("foo bar foo baz foo")
        result = replace_in_file(p, "foo", "qux", allow_multiple=True)
        assert result["ok"] is True
        assert result["replacements"] == 3
        assert p.read_text() == "qux bar qux baz qux"

    def test_preserves_surrounding_content(self, tmp_path: Path) -> None:
        p = tmp_path / "setup.md"
        p.write_text("line 1\nTARGET LINE\nline 3\n")
        result = replace_in_file(p, "TARGET LINE", "REPLACED LINE")
        assert result["ok"] is True
        assert p.read_text() == "line 1\nREPLACED LINE\nline 3\n"

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        p = tmp_path / "str.txt"
        p.write_text("old content")
        result = replace_in_file(str(p), "old content", "new content")
        assert result["ok"] is True

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        result = replace_in_file(tmp_path / "ghost.txt", "old", "new")
        assert result["ok"] is False
        assert "not found" in str(result["error"]).lower()

    def test_multiline_anchor(self, tmp_path: Path) -> None:
        p = tmp_path / "multi.txt"
        p.write_text("line A\nline B\nline C\n")
        result = replace_in_file(p, "line A\nline B", "line X\nline Y")
        assert result["ok"] is True
        assert p.read_text() == "line X\nline Y\nline C\n"


# ---------------------------------------------------------------------------
# search_text (async — mocked rg subprocess since rg may not be installed)
# ---------------------------------------------------------------------------


def _make_proc(stdout: bytes, stderr: bytes, returncode: int) -> MagicMock:
    """Build a fake asyncio subprocess process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class TestSearchText:
    @pytest.mark.anyio
    async def test_finds_matching_line(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("def foo():\n    return 42\n")
        rg_output = b"code.py\n1:def foo():\n"
        proc = _make_proc(rg_output, b"", 0)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            result = await search_text("def foo", tmp_path)
        assert result["ok"] is True
        assert "foo" in str(result["matches"])

    @pytest.mark.anyio
    async def test_no_matches_returns_placeholder(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("hello world\n")
        proc = _make_proc(b"", b"", 1)  # rg exits 1 when no matches
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            result = await search_text("xyzzy_not_here", tmp_path)
        assert result["ok"] is True
        assert "(no matches)" in str(result["matches"])

    @pytest.mark.anyio
    async def test_nonexistent_directory_returns_error(self, tmp_path: Path) -> None:
        result = await search_text("pattern", tmp_path / "ghost")
        assert result["ok"] is False
        assert "does not exist" in str(result["error"]).lower()

    @pytest.mark.anyio
    async def test_finds_across_multiple_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("needle here\n")
        (tmp_path / "b.txt").write_text("nothing\n")
        (tmp_path / "c.txt").write_text("another needle\n")
        rg_output = b"a.txt\n1:needle here\n\nc.txt\n1:another needle\n"
        proc = _make_proc(rg_output, b"", 0)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            result = await search_text("needle", tmp_path)
        assert result["ok"] is True
        assert "needle" in str(result["matches"])

    @pytest.mark.anyio
    async def test_rg_not_found_returns_error(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("content")
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("rg")):
            result = await search_text("pattern", tmp_path)
        assert result["ok"] is False
        assert "not found" in str(result["error"]).lower()

    @pytest.mark.anyio
    async def test_rg_error_exit_returns_error(self, tmp_path: Path) -> None:
        (tmp_path / "file.txt").write_text("content")
        proc = _make_proc(b"", b"some error", 2)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            result = await search_text("pattern", tmp_path)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# read_file_lines
# ---------------------------------------------------------------------------


class TestReadFileLines:
    def test_reads_exact_range(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
        result = read_file_lines(p, 2, 3)
        assert result["ok"] is True
        assert result["content"] == "line2\nline3\n"
        assert result["start_line"] == 2
        assert result["end_line"] == 3
        assert result["total_lines"] == 4

    def test_clamps_end_beyond_file(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("a\nb\nc\n", encoding="utf-8")
        result = read_file_lines(p, 2, 999)
        assert result["ok"] is True
        assert result["end_line"] == 3
        assert "b\n" in str(result["content"])

    def test_clamps_start_below_one(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("a\nb\n", encoding="utf-8")
        result = read_file_lines(p, -5, 1)
        assert result["ok"] is True
        assert result["start_line"] == 1

    def test_single_line(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("only\n", encoding="utf-8")
        result = read_file_lines(p, 1, 1)
        assert result["ok"] is True
        assert result["content"] == "only\n"

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        result = read_file_lines(tmp_path / "missing.txt", 1, 5)
        assert result["ok"] is False
        assert "not found" in str(result["error"]).lower()

    def test_start_beyond_end_after_clamp_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("one\n", encoding="utf-8")
        result = read_file_lines(p, 5, 3)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# insert_after_in_file
# ---------------------------------------------------------------------------


class TestInsertAfterInFile:
    def test_inserts_after_anchor(self, tmp_path: Path) -> None:
        p = tmp_path / "f.py"
        p.write_text("import os\nimport sys\n", encoding="utf-8")
        result = insert_after_in_file(p, "import os\n", "import re\n")
        assert result["ok"] is True
        assert p.read_text(encoding="utf-8") == "import os\nimport re\nimport sys\n"

    def test_anchor_not_found_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("hello world\n", encoding="utf-8")
        result = insert_after_in_file(p, "MISSING", "new line\n")
        assert result["ok"] is False
        assert "not found" in str(result["error"])

    def test_ambiguous_anchor_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("foo\nfoo\n", encoding="utf-8")
        result = insert_after_in_file(p, "foo", "bar")
        assert result["ok"] is False
        assert "2 times" in str(result["error"])

    def test_inserts_at_end_of_file(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_text("the end", encoding="utf-8")
        result = insert_after_in_file(p, "the end", "\nextra")
        assert result["ok"] is True
        assert p.read_text(encoding="utf-8") == "the end\nextra"

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        result = insert_after_in_file(tmp_path / "nope.txt", "anchor", "content")
        assert result["ok"] is False
        assert "not found" in str(result["error"]).lower()

    def test_multiline_anchor(self, tmp_path: Path) -> None:
        p = tmp_path / "f.py"
        p.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n", encoding="utf-8")
        result = insert_after_in_file(p, "def foo():\n    pass\n", "\ndef baz():\n    pass\n")
        assert result["ok"] is True
        text = p.read_text(encoding="utf-8")
        assert "def baz" in text
        assert text.index("def baz") < text.index("def bar")
