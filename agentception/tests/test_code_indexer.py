"""Tests for agentception.services.code_indexer.

All Qdrant and FastEmbed I/O is mocked so tests run without external services
or model downloads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agentception.services.code_indexer import (
    _INDEX_VERSION,
    IndexStats,
    SearchMatch,
    _index_version_is_current,
    _chunk_file,
    _compute_file_hash,
    _delete_chunks_by_file,
    _ensure_collection,
    _fetch_indexed_hashes,
    _reset_bm25_model,
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
    """Return deterministic 768-dim zero vectors — no model download."""
    return [[0.0] * 768 for _ in _texts]


async def _fake_bm25(texts: list[str]) -> list[dict[int, float]]:
    """Return trivial sparse vectors — no BM25 model download."""
    return [{42: 1.0} for _ in texts]


@pytest.fixture(autouse=True)
def _mock_bm25_vectors() -> Generator[None, None, None]:
    """Auto-patch BM25 helpers for every test in this file.

    - ``_compute_bm25_vectors``: prevented from loading the Qdrant/bm25 ONNX
      model — returns trivial sparse vectors instead.
    - ``_needs_index_rebuild``: always returns False so incremental tests are
      not perturbed by missing ``_index_version`` fields in mock payloads.

    Tests that need to exercise the rebuild path should override these patches
    locally with their own ``patch`` context managers.
    """
    _reset_bm25_model()
    with (
        patch(
            "agentception.services.code_indexer._compute_bm25_vectors",
            side_effect=_fake_bm25,
        ),
        patch(
            "agentception.services.code_indexer._needs_index_rebuild",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        yield
    _reset_bm25_model()


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


def test_walk_files_skips_alembic_versions(tmp_path: Path) -> None:
    """Alembic migration files are excluded — they are auto-generated DDL noise."""
    (tmp_path / "alembic").mkdir()
    (tmp_path / "alembic" / "env.py").write_text("# alembic env")
    (tmp_path / "alembic" / "versions").mkdir()
    (tmp_path / "alembic" / "versions" / "0001_schema.py").write_text("def upgrade(): pass")
    (tmp_path / "app.py").write_text("pass")

    files = _walk_files(tmp_path)
    names = {f.name for f in files}

    assert "app.py" in names
    assert "env.py" in names          # alembic/env.py IS indexed (hand-authored)
    assert "0001_schema.py" not in names  # alembic/versions/ is skipped


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


def test_chunk_file_ast_extracts_class_with_methods_as_separate_chunks(
    tmp_path: Path,
) -> None:
    """Classes with methods are split: one header chunk + one chunk per method."""
    f = tmp_path / "classes.py"
    f.write_text(
        "class Alpha:\n"
        "    '''Class docstring.'''\n"
        "    x: int = 0\n"
        "    def method(self):\n"
        "        pass\n"
        "\n"
        "class Beta:\n"
        "    pass\n"
    )
    chunks = _chunk_file(f, tmp_path)
    # Alpha → header + 1 method; Beta → whole class (no methods).
    assert len(chunks) == 3

    symbols = [c["symbol"] for c in chunks]
    assert "class Alpha" in symbols
    assert "class Alpha > def method" in symbols
    assert "class Beta" in symbols

    header = next(c for c in chunks if c["symbol"] == "class Alpha")
    assert "class Alpha:" in header["text"]
    assert "Class docstring" in header["text"]
    assert "def method" not in header["text"]  # method is in its own chunk

    method_chunk = next(c for c in chunks if c["symbol"] == "class Alpha > def method")
    assert "def method" in method_chunk["text"]


def test_chunk_file_ast_class_without_methods_emits_whole_class(tmp_path: Path) -> None:
    """A class with no methods is emitted as a single chunk."""
    f = tmp_path / "data_class.py"
    f.write_text(
        "class Point:\n"
        "    x: float\n"
        "    y: float\n"
    )
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) == 1
    assert "class Point" in chunks[0]["symbol"]
    assert "class Point:" in chunks[0]["text"]


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
    assert stats["files_skipped"] == 0
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
    # Native hybrid search: single query_points call returns fused results.
    mock_client.query_points.return_value = SimpleNamespace(points=[expected_point])

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
        # Only 1 valid candidate — reranking is skipped (requires len > 1).
    ):
        matches: list[SearchMatch] = await search_codebase("qdrant url config", n_results=3)

    assert len(matches) == 1
    m = matches[0]
    assert m["file"] == "agentception/config.py"
    assert "qdrant_url" in m["chunk"]
    assert isinstance(m["score"], float)


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


@pytest.mark.anyio
async def test_hybrid_search_uses_native_qdrant_prefetch() -> None:
    """search_codebase issues a single prefetch+RRF query and cross-encoder reranks.

    The new pipeline issues one ``query_points`` call with ``prefetch`` for
    both dense and sparse sub-queries, delegating RRF fusion to Qdrant
    server-side.  A cross-encoder then re-orders by relevance score.
    """
    from qdrant_client.models import FusionQuery, Prefetch

    p_high = SimpleNamespace(
        id=1, version=0, score=0.03,
        payload={
            "file": "models/run.py",
            "chunk": "class AgentRun:\n    state: RunState\n",
            "start_line": 20, "end_line": 22,
        },
        vector=None,
    )
    p_mid = SimpleNamespace(
        id=2, version=0, score=0.02,
        payload={
            "file": "services/workflow.py",
            "chunk": "def transition_to_implementing(run_id: str) -> None:\n    pass\n",
            "start_line": 50, "end_line": 52,
        },
        vector=None,
    )
    p_low = SimpleNamespace(
        id=3, version=0, score=0.01,
        payload={
            "file": "models/state.py",
            "chunk": "class RunState(str, enum.Enum):\n    implementing = 'implementing'\n",
            "start_line": 10, "end_line": 12,
        },
        vector=None,
    )

    mock_client = AsyncMock()
    mock_client.query_points.return_value = SimpleNamespace(points=[p_high, p_mid, p_low])

    # Reranker reverses the order: state.py → workflow.py → run.py.
    fake_rerank_scores = [0.1, 0.5, 0.9]

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch(
            "agentception.services.code_indexer._rerank",
            new_callable=AsyncMock,
            return_value=fake_rerank_scores,
        ),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        matches = await search_codebase("RunState", n_results=3)

    # Single native Qdrant call with prefetch structure.
    assert mock_client.query_points.call_count == 1
    call_kwargs = mock_client.query_points.call_args.kwargs
    assert "prefetch" in call_kwargs
    prefetch = call_kwargs["prefetch"]
    assert len(prefetch) == 2
    assert all(isinstance(p, Prefetch) for p in prefetch)
    assert isinstance(call_kwargs["query"], FusionQuery)

    # Reranker re-ordered results.
    assert len(matches) == 3
    assert matches[0]["file"] == "models/state.py"
    assert matches[1]["file"] == "services/workflow.py"
    assert matches[2]["file"] == "models/run.py"
    assert matches[0]["score"] == pytest.approx(0.9)


@pytest.mark.anyio
async def test_search_codebase_skips_reranking_for_single_result() -> None:
    """Reranking is skipped when only one valid candidate is returned."""
    point = _make_scored_point("agentception/config.py", "x = 1", score=0.5)
    mock_client = AsyncMock()
    mock_client.query_points.return_value = SimpleNamespace(points=[point])

    rerank_called = False

    async def _fake_rerank(q: str, docs: list[str]) -> list[float]:
        nonlocal rerank_called
        rerank_called = True
        return [1.0] * len(docs)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("agentception.services.code_indexer._rerank", side_effect=_fake_rerank),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        matches = await search_codebase("x", n_results=1)

    assert not rerank_called
    assert len(matches) == 1


# ── payload index and symbol field tests ─────────────────────────────────────


def test_chunk_file_ast_symbol_field_populated(tmp_path: Path) -> None:
    """AST chunks carry a populated symbol field describing each symbol."""
    f = tmp_path / "syms.py"
    f.write_text(
        # MyModel has no methods → emitted as one whole-class chunk.
        "class MyModel:\n"
        "    pass\n"
        "\n"
        "async def my_handler():\n"
        "    pass\n"
    )
    chunks = _chunk_file(f, tmp_path)
    symbols = {c["symbol"] for c in chunks}
    assert "class MyModel" in symbols
    assert "async def my_handler" in symbols


def test_chunk_file_char_symbol_field_empty(tmp_path: Path) -> None:
    """Character-level chunks always have an empty symbol field."""
    f = tmp_path / "prose.md"
    f.write_text("# Hello\n\nSome text.\n")
    chunks = _chunk_file(f, tmp_path)
    assert len(chunks) >= 1
    assert all(c["symbol"] == "" for c in chunks)


@pytest.mark.anyio
async def test_index_codebase_embeds_enriched_text(tmp_path: Path) -> None:
    """index_codebase prepends file path and symbol to each chunk before embedding.

    The embedded text must start with ``# <file>`` so the dense vector is
    anchored to the chunk's location, not just the raw source code.
    """
    py_file = tmp_path / "utils.py"
    py_file.write_text("def helper():\n    return 42\n")

    embedded_texts: list[str] = []

    def _capture_embed(texts: list[str]) -> list[list[float]]:
        embedded_texts.extend(texts)
        return [[0.0] * 768 for _ in texts]

    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(collections=[])
    mock_client.scroll.return_value = ([], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_capture_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        await index_codebase(repo_path=tmp_path)

    assert embedded_texts, "At least one text must have been embedded"
    for text in embedded_texts:
        assert text.startswith("# "), (
            f"Embedded text must start with file path header, got: {text[:60]!r}"
        )
        assert "utils.py" in text


@pytest.mark.anyio
async def test_index_codebase_creates_payload_index(tmp_path: Path) -> None:
    """index_codebase calls create_payload_index to enable filtered search."""
    py_file = tmp_path / "mod.py"
    py_file.write_text("def f(): pass\n")

    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(collections=[])
    mock_client.scroll.return_value = ([], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        await index_codebase(repo_path=tmp_path)

    mock_client.create_payload_index.assert_called_once()
    call_kwargs = mock_client.create_payload_index.call_args.kwargs
    assert call_kwargs["field_name"] == "file"


@pytest.mark.anyio
async def test_ensure_collection_no_payload_indexes_config_kwarg(tmp_path: Path) -> None:
    """Regression: _ensure_collection must not pass payload_indexes_config.

    qdrant-client 1.17 does not accept this kwarg and raises
    ``Unknown arguments: ['payload_indexes_config']``, crashing every index run.
    """
    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(collections=[])

    with patch("agentception.services.code_indexer.settings") as mock_settings:
        mock_settings.embed_model_dim = 384
        await _ensure_collection(mock_client, "code")

    mock_client.create_collection.assert_called_once()
    call_kwargs = mock_client.create_collection.call_args.kwargs
    assert "payload_indexes_config" not in call_kwargs, (
        "payload_indexes_config is not supported by the installed qdrant-client "
        "and must not be passed to create_collection"
    )


@pytest.mark.anyio
async def test_ensure_collection_migrates_legacy_single_vector_schema() -> None:
    """Regression: _ensure_collection must delete+recreate a legacy collection.

    Before hybrid search, the code collection used a single unnamed VectorParams
    (size=384, COSINE).  _ensure_collection previously skipped existing collections
    entirely, leaving live searches on the old schema with no sparse/BM25 support.
    """
    from qdrant_client.models import Distance, VectorParams

    legacy_col = SimpleNamespace(
        name="code",
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors=VectorParams(size=384, distance=Distance.COSINE),
            )
        ),
    )
    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(collections=[legacy_col])
    mock_client.get_collection.return_value = legacy_col

    with patch("agentception.services.code_indexer.settings") as mock_settings:
        mock_settings.embed_model_dim = 384
        await _ensure_collection(mock_client, "code")

    mock_client.delete_collection.assert_called_once_with("code")
    mock_client.create_collection.assert_called_once()
    call_kwargs = mock_client.create_collection.call_args.kwargs
    assert "dense" in call_kwargs["vectors_config"], "Expected named 'dense' vector"
    assert "sparse" in call_kwargs["sparse_vectors_config"], "Expected 'sparse' vector"


@pytest.mark.anyio
async def test_index_codebase_writes_symbol_to_payload(tmp_path: Path) -> None:
    """index_codebase includes 'symbol' in each upserted Qdrant point payload."""
    (tmp_path / "mod.py").write_text("def compute():\n    return 42\n")

    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(collections=[])

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        await index_codebase(repo_path=tmp_path)

    mock_client.upsert.assert_called()
    points = mock_client.upsert.call_args.kwargs["points"]
    assert len(points) == 1
    assert points[0].payload["symbol"] == "def compute"
    assert "file_hash" in points[0].payload
    assert len(points[0].payload["file_hash"]) == 64  # SHA-256 hex digest


# ── _compute_file_hash tests ──────────────────────────────────────────────────


def test_compute_file_hash_returns_sha256_hex(tmp_path: Path) -> None:
    """_compute_file_hash returns a 64-character hex SHA-256 digest."""
    f = tmp_path / "sample.py"
    f.write_text("def foo(): pass\n")
    digest = _compute_file_hash(f)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_compute_file_hash_is_deterministic(tmp_path: Path) -> None:
    """Same file content always produces the same hash."""
    f = tmp_path / "stable.py"
    f.write_text("x = 1\n")
    assert _compute_file_hash(f) == _compute_file_hash(f)


def test_compute_file_hash_differs_on_content_change(tmp_path: Path) -> None:
    """Different file contents produce different hashes."""
    f = tmp_path / "changing.py"
    f.write_text("x = 1\n")
    hash_before = _compute_file_hash(f)
    f.write_text("x = 2\n")
    hash_after = _compute_file_hash(f)
    assert hash_before != hash_after


def test_compute_file_hash_missing_file_returns_empty(tmp_path: Path) -> None:
    """_compute_file_hash returns '' for a file that does not exist."""
    f = tmp_path / "nonexistent.py"
    assert _compute_file_hash(f) == ""


# ── _fetch_indexed_hashes tests ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_fetch_indexed_hashes_returns_file_hash_map() -> None:
    """_fetch_indexed_hashes extracts file→hash pairs from Qdrant payloads."""
    from types import SimpleNamespace

    point = SimpleNamespace(
        id=1,
        payload={"file": "agentception/config.py", "file_hash": "abc123"},
    )
    mock_client = AsyncMock()
    # scroll returns (points, next_offset); None offset signals end of scroll.
    mock_client.scroll.return_value = ([point], None)

    result = await _fetch_indexed_hashes(mock_client, "code")

    assert result == {"agentception/config.py": "abc123"}


@pytest.mark.anyio
async def test_fetch_indexed_hashes_ignores_points_without_hash() -> None:
    """Points that lack a file_hash field (legacy) are silently ignored."""
    from types import SimpleNamespace

    point_legacy = SimpleNamespace(id=1, payload={"file": "old.py"})
    point_new = SimpleNamespace(
        id=2, payload={"file": "new.py", "file_hash": "deadbeef" * 8}
    )
    mock_client = AsyncMock()
    mock_client.scroll.return_value = ([point_legacy, point_new], None)

    result = await _fetch_indexed_hashes(mock_client, "code")

    assert "old.py" not in result
    assert result["new.py"] == "deadbeef" * 8


@pytest.mark.anyio
async def test_fetch_indexed_hashes_returns_empty_on_error() -> None:
    """_fetch_indexed_hashes returns {} when Qdrant raises an exception."""
    mock_client = AsyncMock()
    mock_client.scroll.side_effect = ConnectionRefusedError("qdrant down")

    result = await _fetch_indexed_hashes(mock_client, "code")

    assert result == {}


# ── incremental indexing tests ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_index_codebase_skips_unchanged_files(tmp_path: Path) -> None:
    """Files whose hash matches the stored hash are skipped (files_skipped > 0)."""
    import hashlib

    py_file = tmp_path / "mod.py"
    py_file.write_text("def foo(): pass\n")
    md_file = tmp_path / "readme.md"
    md_file.write_text("# Hello\n")

    # Pre-compute the hash for mod.py so it appears already indexed.
    py_hash = hashlib.sha256(py_file.read_bytes()).hexdigest()
    py_rel = "mod.py"

    # Simulate Qdrant already having mod.py indexed with its current hash.
    existing_point = SimpleNamespace(
        id=1,
        payload={"file": py_rel, "file_hash": py_hash},
    )

    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name="code")]
    )
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors={"dense": object(), "sparse": object()})
        )
    )
    mock_client.scroll.return_value = ([existing_point], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    assert stats["files_skipped"] == 1  # mod.py was skipped
    assert stats["files_indexed"] == 1  # readme.md was indexed


@pytest.mark.anyio
async def test_index_codebase_rehashes_changed_files(tmp_path: Path) -> None:
    """A file whose content changed since last index is re-indexed."""
    py_file = tmp_path / "mod.py"
    py_file.write_text("def foo(): pass\n")

    # Store a *stale* hash so the file appears changed.
    stale_point = SimpleNamespace(
        id=1,
        payload={"file": "mod.py", "file_hash": "stale_hash_value"},
    )

    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name="code")]
    )
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors={"dense": object(), "sparse": object()})
        )
    )
    mock_client.scroll.return_value = ([stale_point], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    assert stats["files_skipped"] == 0  # stale hash → file was re-indexed
    assert stats["files_indexed"] == 1

    # Verify the upserted point carries the new (correct) hash.
    mock_client.upsert.assert_called()
    points = mock_client.upsert.call_args.kwargs["points"]
    stored_hash = points[0].payload["file_hash"]
    assert len(stored_hash) == 64  # valid SHA-256 hex
    assert stored_hash != "stale_hash_value"


# ── Incremental indexing tests ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_incremental_first_index_upserts_all_files(tmp_path: Path) -> None:
    """First index with no prior state upserts all file chunks, no deletions."""
    (tmp_path / "app.py").write_text("def hello(): pass\n")
    (tmp_path / "readme.md").write_text("# Hello\n")

    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(collections=[])
    mock_client.scroll.return_value = ([], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    assert stats["files_skipped"] == 0
    assert stats["files_indexed"] == 2
    mock_client.upsert.assert_called()
    mock_client.delete.assert_not_called()


@pytest.mark.anyio
async def test_incremental_unchanged_files_skipped(tmp_path: Path) -> None:
    """Files whose hash matches Qdrant are skipped — zero upsert or delete calls."""
    py_file = tmp_path / "app.py"
    py_file.write_text("def hello(): pass\n")
    current_hash = _compute_file_hash(py_file)

    existing_point = SimpleNamespace(
        id=1,
        payload={"file": "app.py", "file_hash": current_hash},
    )
    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name="code")]
    )
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors={"dense": object(), "sparse": object()})
        )
    )
    mock_client.scroll.return_value = ([existing_point], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    assert stats["files_skipped"] == 1
    assert stats["files_indexed"] == 0
    mock_client.upsert.assert_not_called()
    mock_client.delete.assert_not_called()


@pytest.mark.anyio
async def test_incremental_changed_file_replaces_chunks(tmp_path: Path) -> None:
    """A changed file has its old chunks deleted before new chunks are upserted."""
    py_file = tmp_path / "app.py"
    py_file.write_text("def hello(): pass\n")

    stale_point = SimpleNamespace(
        id=1,
        payload={"file": "app.py", "file_hash": "old_stale_hash"},
    )
    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name="code")]
    )
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors={"dense": object(), "sparse": object()})
        )
    )
    mock_client.scroll.return_value = ([stale_point], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    assert stats["files_skipped"] == 0
    assert stats["files_indexed"] == 1
    # Old chunks deleted before new ones upserted.
    mock_client.delete.assert_called_once()
    mock_client.upsert.assert_called()


@pytest.mark.anyio
async def test_incremental_deleted_file_removes_chunks(tmp_path: Path) -> None:
    """A file removed from disk has all its Qdrant chunks deleted."""
    remaining = tmp_path / "remaining.py"
    remaining.write_text("x = 1\n")
    remaining_hash = _compute_file_hash(remaining)

    remaining_point = SimpleNamespace(
        id=1,
        payload={"file": "remaining.py", "file_hash": remaining_hash},
    )
    deleted_point = SimpleNamespace(
        id=2,
        payload={"file": "deleted.py", "file_hash": "some_old_hash"},
    )
    mock_client = AsyncMock()
    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name="code")]
    )
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors={"dense": object(), "sparse": object()})
        )
    )
    mock_client.scroll.return_value = ([remaining_point, deleted_point], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    assert stats["files_skipped"] == 1  # remaining.py unchanged
    # Exactly one delete call — for the removed file.
    mock_client.delete.assert_called_once()
    delete_kwargs = mock_client.delete.call_args.kwargs
    must = delete_kwargs["points_selector"].filter.must
    assert len(must) == 1
    assert must[0].key == "file"
    assert must[0].match.value == "deleted.py"


@pytest.mark.anyio
async def test_incremental_force_full_rebuilds_collection(tmp_path: Path) -> None:
    """force_full=True drops the collection and indexes all files regardless of hashes."""
    py_file = tmp_path / "app.py"
    py_file.write_text("def hello(): pass\n")
    current_hash = _compute_file_hash(py_file)

    # Simulate the file already indexed with its current hash — would be skipped
    # in incremental mode, but must be indexed in force_full mode.
    existing_point = SimpleNamespace(
        id=1,
        payload={"file": "app.py", "file_hash": current_hash},
    )
    mock_client = AsyncMock()
    # First call: collection exists (triggers force_full deletion).
    # Second call: collection gone (triggers _ensure_collection to recreate it).
    mock_client.get_collections.side_effect = [
        SimpleNamespace(collections=[SimpleNamespace(name="code")]),
        SimpleNamespace(collections=[]),
    ]
    mock_client.scroll.return_value = ([existing_point], None)

    with (
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path, force_full=True)

    assert stats["ok"] is True
    # force_full skips nothing — all files are indexed.
    assert stats["files_skipped"] == 0
    assert stats["files_indexed"] == 1
    # Collection dropped and recreated.
    mock_client.delete_collection.assert_called_once()
    mock_client.create_collection.assert_called_once()
    mock_client.upsert.assert_called()


def test_index_version_is_current_true_when_version_matches() -> None:
    """_index_version_is_current returns True when payload contains the current version."""
    assert _index_version_is_current({"_index_version": _INDEX_VERSION}) is True


def test_index_version_is_current_false_when_version_absent() -> None:
    """_index_version_is_current returns False when _index_version field is missing."""
    assert _index_version_is_current({"file": "mod.py"}) is False


def test_index_version_is_current_false_when_version_stale() -> None:
    """_index_version_is_current returns False when payload holds an older version."""
    assert _index_version_is_current({"_index_version": "v1"}) is False


@pytest.mark.anyio
async def test_index_version_mismatch_triggers_full_rebuild(tmp_path: Path) -> None:
    """index_codebase drops and rebuilds the collection when _needs_index_rebuild returns True.

    Regression: any change to the index pipeline (embedding model, chunking
    strategy, BM25 implementation, chunk text enrichment) changes vector
    semantics.  The automatic rebuild replaces stale points so search returns
    correct results from the first run after an upgrade.
    """
    py_file = tmp_path / "mod.py"
    py_file.write_text("def greet(): return 'hello'\n")

    mock_client = AsyncMock()
    # First _ensure_collection call: collection exists with current schema.
    # Second _ensure_collection call (after delete): collection gone → create.
    mock_client.get_collections.side_effect = [
        SimpleNamespace(collections=[SimpleNamespace(name="code")]),
        SimpleNamespace(collections=[]),
    ]
    mock_client.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors={"dense": object(), "sparse": object()})
        )
    )
    mock_client.scroll.return_value = ([], None)

    # Override autouse mock: _needs_index_rebuild returns True to trigger rebuild.
    with (
        patch(
            "agentception.services.code_indexer._needs_index_rebuild",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agentception.services.code_indexer._embed", side_effect=_fake_embed),
        patch("qdrant_client.AsyncQdrantClient", return_value=mock_client),
    ):
        stats: IndexStats = await index_codebase(repo_path=tmp_path)

    assert stats["ok"] is True
    # Version mismatch forced a full rebuild — nothing skipped.
    assert stats["files_skipped"] == 0
    assert stats["files_indexed"] == 1
    # Collection was dropped and recreated.
    mock_client.delete_collection.assert_called()
    mock_client.create_collection.assert_called()
    # All upserted points must carry the current index version.
    upsert_call = mock_client.upsert.call_args
    upserted_points = upsert_call.kwargs["points"]
    for point in upserted_points:
        assert point.payload.get("_index_version") == _INDEX_VERSION, (
            "Every indexed chunk must carry the current _index_version"
        )
