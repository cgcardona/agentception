"""Tests for agentception.services.code_indexer.

All Qdrant and FastEmbed I/O is mocked so tests run without external services
or model downloads.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agentception.services.code_indexer import (
    IndexStats,
    SearchMatch,
    _chunk_file,
    _reset_model,
    _should_index,
    _walk_files,
    index_codebase,
    search_codebase,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_scored_point(
    file: str,
    chunk: str,
    score: float = 0.9,
    start_line: int = 1,
    end_line: int = 10,
) -> object:
    """Build a minimal ScoredPoint-like object for mocking search results."""
    from qdrant_client.models import ScoredPoint

    return ScoredPoint(
        id=1,
        version=0,
        score=score,
        payload={
            "file": file,
            "chunk": chunk,
            "start_line": start_line,
            "end_line": end_line,
        },
        vector=None,
    )


def _fake_embed(_texts: list[str]) -> list[list[float]]:
    """Return deterministic 384-dim zero vectors — no model download."""
    return [[0.0] * 384 for _ in _texts]


# ── File walking tests ────────────────────────────────────────────────────────


def test_should_index_accepts_python_files(tmp_path: Path) -> None:
    f = tmp_path / "main.py"
    f.write_text("x = 1")
    assert _should_index(f) is True


def test_should_index_rejects_unknown_extension(tmp_path: Path) -> None:
    f = tmp_path / "binary.exe"
    f.write_bytes(b"\x00" * 100)
    assert _should_index(f) is False


def test_should_index_rejects_large_file(tmp_path: Path) -> None:
    f = tmp_path / "huge.py"
    f.write_bytes(b"x" * 300_001)
    assert _should_index(f) is False


def test_walk_files_skips_git_and_pycache(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git config")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.pyc").write_bytes(b"\x00")
    (tmp_path / "main.py").write_text("pass")
    files = _walk_files(tmp_path)
    paths = [f.name for f in files]
    assert "main.py" in paths
    assert "config" not in paths
    assert "mod.pyc" not in paths


def test_walk_files_includes_multiple_extensions(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("pass")
    (tmp_path / "readme.md").write_text("# Hello")
    (tmp_path / "config.toml").write_text("[tool]")
    files = _walk_files(tmp_path)
    names = {f.name for f in files}
    assert names == {"code.py", "readme.md", "config.toml"}


# ── Chunking tests ────────────────────────────────────────────────────────────


def test_chunk_file_produces_at_least_one_chunk(tmp_path: Path) -> None:
    f = tmp_path / "short.py"
    f.write_text("def foo():\n    pass\n")
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) >= 1


def test_chunk_file_relative_path(tmp_path: Path) -> None:
    sub = tmp_path / "pkg"
    sub.mkdir()
    f = sub / "mod.py"
    f.write_text("x = 1")
    chunks = _chunk_file(f, tmp_path)
    assert chunks[0]["file"] == "pkg/mod.py"


def test_chunk_file_large_file_produces_multiple_chunks(tmp_path: Path) -> None:
    f = tmp_path / "big.py"
    f.write_text("x = 1\n" * 500)
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) > 1


def test_chunk_file_ids_are_deterministic(tmp_path: Path) -> None:
    f = tmp_path / "stable.py"
    f.write_text("print('hello')")
    ids_first = [c["chunk_id"] for c in _chunk_file(f, tmp_path)]
    ids_second = [c["chunk_id"] for c in _chunk_file(f, tmp_path)]
    assert ids_first == ids_second


def test_chunk_file_ids_are_unique_within_file(tmp_path: Path) -> None:
    f = tmp_path / "multi.py"
    f.write_text("y = 2\n" * 600)
    chunks = _chunk_file(f, tmp_path)
    ids = [c["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids))


def test_chunk_file_missing_file_returns_empty(tmp_path: Path) -> None:
    f = tmp_path / "nonexistent.py"
    assert _chunk_file(f, tmp_path) == []


def test_chunk_file_ast_extracts_functions(tmp_path: Path) -> None:
    """AST chunking extracts each top-level function as a separate chunk."""
    f = tmp_path / "funcs.py"
    f.write_text(
        "def foo():\n"
        "    '''Docstring for foo.'''\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
    )
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) == 2
    assert "def foo():" in chunks[0]["text"]
    assert "Docstring for foo" in chunks[0]["text"]
    assert "def bar():" in chunks[1]["text"]


def test_chunk_file_ast_extracts_classes(tmp_path: Path) -> None:
    """AST chunking extracts each top-level class as a separate chunk."""
    f = tmp_path / "classes.py"
    f.write_text(
        "class Alpha:\n"
        "    '''Class docstring.'''\n"
        "    def method(self):\n"
        "        pass\n"
        "\n"
        "class Beta:\n"
        "    pass\n"
    )
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) == 2
    assert "class Alpha:" in chunks[0]["text"]
    assert "Class docstring" in chunks[0]["text"]
    assert "def method" in chunks[0]["text"]
    assert "class Beta:" in chunks[1]["text"]


def test_chunk_file_ast_includes_decorators(tmp_path: Path) -> None:
    """AST chunking includes decorators in the chunk."""
    f = tmp_path / "decorated.py"
    f.write_text(
        "@decorator\n"
        "@another_decorator(arg=1)\n"
        "def decorated_func():\n"
        "    pass\n"
    )
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) == 1
    assert "@decorator" in chunks[0]["text"]
    assert "@another_decorator" in chunks[0]["text"]
    assert chunks[0]["start_line"] == 1


def test_chunk_file_ast_preserves_async_functions(tmp_path: Path) -> None:
    """AST chunking handles async functions correctly."""
    f = tmp_path / "async_code.py"
    f.write_text(
        "async def fetch_data():\n"
        "    '''Async docstring.'''\n"
        "    return await something()\n"
    )
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) == 1
    assert "async def fetch_data" in chunks[0]["text"]
    assert "Async docstring" in chunks[0]["text"]


def test_chunk_file_ast_falls_back_on_syntax_error(tmp_path: Path) -> None:
    """AST chunking falls back to character chunking for malformed Python."""
    f = tmp_path / "broken.py"
    f.write_text("def incomplete(\n")  # Syntax error
    chunks = _chunk_file(f, tmp_path)
    # Should fall back to character chunking and produce at least one chunk.
    assert len(chunks) >= 1
    assert "def incomplete" in chunks[0]["text"]


def test_chunk_file_ast_falls_back_when_no_symbols(tmp_path: Path) -> None:
    """AST chunking falls back to character chunking when no top-level symbols exist."""
    f = tmp_path / "only_imports.py"
    f.write_text("import os\nimport sys\n\nx = 1\n")
    chunks = _chunk_file(f, tmp_path)
    # No top-level functions/classes, so should fall back to character chunking.
    assert len(chunks) >= 1
    assert "import os" in chunks[0]["text"]


def test_chunk_file_ast_chunk_ids_use_symbol_names(tmp_path: Path) -> None:
    """AST chunking generates deterministic IDs based on symbol names."""
    f = tmp_path / "named.py"
    f.write_text("def stable_name():\n    pass\n")
    ids_first = [c["chunk_id"] for c in _chunk_file(f, tmp_path)]
    ids_second = [c["chunk_id"] for c in _chunk_file(f, tmp_path)]
    assert ids_first == ids_second
    assert len(ids_first) == 1


def test_chunk_file_non_python_uses_character_chunking(tmp_path: Path) -> None:
    """Non-Python files always use character-based chunking."""
    f = tmp_path / "readme.md"
    f.write_text("# Title\n\nSome content.\n")
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) >= 1
    assert "# Title" in chunks[0]["text"]



# ── index_codebase tests ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_index_codebase_returns_stats(tmp_path: Path) -> None:
    """index_codebase returns ok=True with correct file/chunk counts."""
    (tmp_path / "a.py").write_text("def foo(): pass\n")
    (tmp_path / "b.md").write_text("# Title\n")

    mock_client = AsyncMock()
    # get_collections returns CollectionsResponse-like with .collections = []
    mock_client.get_collections.return_value = SimpleNamespace(collections=[])

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    assert stats["files_indexed"] == 2
    assert stats["chunks_indexed"] >= 2
    assert stats["error"] is None


@pytest.mark.anyio
async def test_index_codebase_error_returns_ok_false(tmp_path: Path) -> None:
    """index_codebase returns ok=False when Qdrant is unreachable."""
    (tmp_path / "x.py").write_text("pass")

    mock_client = AsyncMock()
    mock_client.get_collections.side_effect = ConnectionRefusedError("qdrant down")

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is False
    assert "qdrant down" in (stats["error"] or "")


# ── search_codebase tests ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_search_codebase_returns_matches() -> None:
    """search_codebase parses ScoredPoint payloads into SearchMatch dicts."""
    expected_point = _make_scored_point(
        "agentception/config.py", "qdrant_url: str = ...", score=0.92
    )

    mock_client = AsyncMock()
    mock_client.query_points.return_value = SimpleNamespace(points=[expected_point])

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        matches: list[SearchMatch] = await search_codebase("qdrant url config", n_results=3)

    assert len(matches) == 1
    m = matches[0]
    assert m["file"] == "agentception/config.py"
    assert "qdrant_url" in m["chunk"]
    assert abs(m["score"] - 0.92) < 0.001


@pytest.mark.anyio
async def test_search_codebase_empty_when_no_results() -> None:
    mock_client = AsyncMock()
    mock_client.query_points.return_value = SimpleNamespace(points=[])

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        matches = await search_codebase("nothing matches")

    assert matches == []


@pytest.mark.anyio
async def test_search_codebase_returns_empty_on_qdrant_error() -> None:
    """search_codebase swallows errors and returns [] so the agent loop continues."""
    with patch("agentception.services.code_indexer._embed", side_effect=ConnectionRefusedError):
        matches = await search_codebase("anything")

    assert matches == []


@pytest.mark.anyio
async def test_search_codebase_skips_malformed_payloads() -> None:
    """Points with missing file/chunk fields are silently dropped."""
    from qdrant_client.models import ScoredPoint

    bad_point = ScoredPoint(id=99, version=0, score=0.5, payload={"garbage": True}, vector=None)
    mock_client = AsyncMock()
    mock_client.query_points.return_value = SimpleNamespace(points=[bad_point])

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        matches = await search_codebase("test")

    assert matches == []
