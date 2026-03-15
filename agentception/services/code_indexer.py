"""Qdrant-backed semantic code search for the Cursor-free agent loop.

Replaces Cursor's ``@Codebase`` feature with a self-hosted vector store.
Files are chunked, embedded with a local ONNX model (FastEmbed), and stored
in Qdrant.  The agent then searches with natural language instead of regex.

Chunking Strategy
-----------------
Python files are chunked at the symbol level using AST parsing:

- **Top-level functions** become individual chunks.
- **Top-level classes** are split into a *class header* chunk (class
  definition, docstring, class-level attributes) and one *method chunk* per
  method/async-method in the class body.  This ensures precise retrieval of
  individual methods even in large classes — searching for
  ``teardown_worktree`` returns exactly that method, not the full
  ``WorktreeManager`` class.

TypeScript / JavaScript files are chunked by function and class boundaries
via tree-sitter AST parsing.  All other files use overlapping character-level
chunks for compatibility.

Chunk Text Enrichment
---------------------
Before embedding, every chunk's raw source text is prefixed with its file
path and symbol name::

    # agentception/readers/worktrees.py
    # def ensure_worktree
    <raw source>

This anchors the dense embedding to the file's location and symbol identity
so that queries like "create a git worktree" score highest for
``readers/worktrees.py :: ensure_worktree`` rather than for callers of that
function in other files.

Hybrid Search
-------------
Search combines dense semantic vectors (``jinaai/jina-embeddings-v2-base-code``
— a code-specific 768-dimension model with 8 192-token context window) with
sparse BM25 vectors produced by FastEmbed's ``Qdrant/bm25`` model.  The
sparse model computes proper corpus-aware IDF instead of a hash-based toy.

Results are fused server-side by Qdrant using Reciprocal Rank Fusion (RRF)
via the native ``prefetch + Fusion.RRF`` API — a single round-trip rather
than two sequential queries plus manual Python fusion.

After fusion, a cross-encoder reranker (``BAAI/bge-reranker-base``) scores
each candidate jointly against the query and re-orders the list for precision.

Incremental Indexing
--------------------
``index_codebase`` computes a SHA-256 content hash per file and stores it as
``file_hash`` in each chunk's Qdrant payload.  On re-index, only files whose
hash differs from the stored value (new, changed, or deleted) incur Qdrant
writes.  Unchanged files are skipped entirely — zero Qdrant calls.  Pass
``force_full=True`` to drop and rebuild the collection from scratch.

An ``_index_version`` field is stored in every chunk payload.  When the
indexing pipeline changes (chunking strategy, embedding model, BM25
implementation), the stored version is compared with ``_INDEX_VERSION`` at
the start of ``index_codebase``.  A mismatch triggers an automatic forced
full rebuild.

Public API
----------
``index_codebase(repo_path, force_full=False)``
    Walk every readable source file in *repo_path*, chunk appropriately,
    enrich chunk text with file path + symbol prefix, embed with the
    code-specific FastEmbed model, compute BM25 sparse vectors, and upsert
    to the Qdrant collection configured in
    :data:`~agentception.config.settings.qdrant_collection`.
    Incremental by default — only changed/new/deleted files are processed.

``search_codebase(query, n_results)``
    Run hybrid search (dense + sparse via Qdrant native RRF), then rerank
    with a cross-encoder.  Returns the top *n_results* code chunks.

Both functions accept optional overrides for ``qdrant_url`` and
``collection`` so tests can inject isolated instances without touching the
real vector store.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from agentception.config import settings
from agentception.types import JsonValue

if TYPE_CHECKING:
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    from fastembed.sparse import SparseTextEmbedding
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)

# ── Chunk sizing ───────────────────────────────────────────────────────────────

_CHUNK_SIZE = 1_500  # characters per chunk (≈ 50 lines of Python)
_CHUNK_OVERLAP = 200  # overlap between consecutive chunks for context continuity
_MAX_FILE_BYTES = 200_000  # skip files larger than ~200 KB

# Hard cap on the character length of any single chunk fed to the embedding
# model.  ONNX transformer attention is O(n²) in sequence length; chunks
# produced by the AST chunker have no natural size bound (a single large
# function or class with no methods can be thousands of lines).  Any AST chunk
# that exceeds this limit is char-split into _CHUNK_SIZE-character sub-chunks
# so that the embedder never receives an oversized input.
# 8 192 chars ≈ 2 048 tokens, comfortably inside jina-v2's 8 192-token window.
_MAX_CHUNK_CHARS = 8_192

# ── File extensions to index ───────────────────────────────────────────────────

_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".md", ".j2", ".toml", ".yml", ".yaml",
        ".js", ".ts", ".scss", ".css", ".html", ".json",
        ".txt", ".sh", ".env.example",
    }
)

# ── Directories to skip completely ────────────────────────────────────────────

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
    }
)

# Path-segment pairs that together identify auto-generated subtrees with no
# search value.  A file is skipped when ALL segments in any pair appear (in
# order) as consecutive components of its path.
#
# ``("alembic", "versions")`` — Alembic migration files are DDL generated
# from SQLAlchemy models.  The models in ``agentception/db/models.py`` are the
# source of truth; indexing both creates duplicate, lower-quality signal.
# Migrations also contain large monolithic ``upgrade()`` functions (10k+ chars)
# that cause O(n²) ONNX attention stalls even with the _MAX_EMBED_CHARS cap.
_SKIP_PATH_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("alembic", "versions"),
    }
)

# ── Public result types ───────────────────────────────────────────────────────


class IndexStats(TypedDict):
    """Result returned by :func:`index_codebase`."""

    ok: bool
    files_indexed: int
    chunks_indexed: int
    files_skipped: int
    error: str | None


class SearchMatch(TypedDict):
    """A single result from :func:`search_codebase`."""

    file: str
    chunk: str
    score: float
    start_line: int
    end_line: int


# ── Index pipeline version sentinel ───────────────────────────────────────────
# Stored in every chunk payload under the key ``_index_version``.  Bump this
# string whenever the indexing pipeline changes in a way that invalidates
# existing vectors (new chunking strategy, new embedding model, new BM25
# implementation, chunk text enrichment format, etc.).  A mismatch between
# the stored value and _INDEX_VERSION triggers an automatic forced full rebuild.

_INDEX_VERSION = "v6"


# ── Module-level embedding model caches ───────────────────────────────────────
# Lazily initialised on first use so tests can monkey-patch before loading.

_cached_model: TextEmbedding | None = None
_bm25_model: SparseTextEmbedding | None = None
_rerank_model: TextCrossEncoder | None = None


def _get_model() -> TextEmbedding | None:
    """Return the cached dense TextEmbedding model, initialising it on first call."""
    global _cached_model
    if _cached_model is None:
        import os
        import psutil as _psutil
        _p = _psutil.Process(os.getpid())
        _rss_before = _p.memory_info().rss // 1024 // 1024
        logger.warning("📊 _get_model: LOADING embed model RSS_before=%dMB", _rss_before)
        from fastembed import TextEmbedding  # noqa: PLC0415

        logger.info("✅ code_indexer — loading embed model: %s", settings.embed_model)
        _cached_model = TextEmbedding(model_name=settings.embed_model)
        _rss_after = _p.memory_info().rss // 1024 // 1024
        logger.warning("📊 _get_model: LOADED embed model RSS_after=%dMB (+%dMB)", _rss_after, _rss_after - _rss_before)
    return _cached_model


def _reset_model() -> None:
    """Clear the cached dense model — used by tests to inject a mock."""
    global _cached_model
    _cached_model = None


def _get_bm25_model() -> SparseTextEmbedding | None:
    """Return the cached SparseTextEmbedding BM25 model, initialising it on first call."""
    global _bm25_model
    if _bm25_model is None:
        import os
        import psutil as _psutil
        _p = _psutil.Process(os.getpid())
        _rss_before = _p.memory_info().rss // 1024 // 1024
        logger.warning("📊 _get_bm25_model: LOADING BM25 RSS_before=%dMB", _rss_before)
        from fastembed.sparse import SparseTextEmbedding  # noqa: PLC0415

        logger.info("✅ code_indexer — loading BM25 sparse model: Qdrant/bm25")
        _bm25_model = SparseTextEmbedding("Qdrant/bm25")
        _rss_after = _p.memory_info().rss // 1024 // 1024
        logger.warning("📊 _get_bm25_model: LOADED BM25 RSS_after=%dMB (+%dMB)", _rss_after, _rss_after - _rss_before)
    return _bm25_model


def _reset_bm25_model() -> None:
    """Clear the cached BM25 model — used by tests to inject a mock."""
    global _bm25_model
    _bm25_model = None


def _get_rerank_model() -> TextCrossEncoder | None:
    """Return the cached TextCrossEncoder reranker, initialising it on first call."""
    global _rerank_model
    if _rerank_model is None:
        import os
        import psutil as _psutil
        _p = _psutil.Process(os.getpid())
        _rss_before = _p.memory_info().rss // 1024 // 1024
        logger.warning("📊 _get_rerank_model: LOADING reranker RSS_before=%dMB", _rss_before)
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415

        logger.info(
            "✅ code_indexer — loading reranker model: %s", settings.rerank_model
        )
        _rerank_model = TextCrossEncoder(settings.rerank_model)
        _rss_after = _p.memory_info().rss // 1024 // 1024
        logger.warning("📊 _get_rerank_model: LOADED reranker RSS_after=%dMB (+%dMB)", _rss_after, _rss_after - _rss_before)
    return _rerank_model


def _reset_rerank_model() -> None:
    """Clear the cached reranker — used by tests to inject a mock."""
    global _rerank_model
    _rerank_model = None


def _rerank_sync(query: str, documents: list[str]) -> list[float]:
    """Return cross-encoder relevance scores for *documents* given *query*.

    Runs synchronously — call via :func:`asyncio.to_thread` from async code.
    Scores are in the same order as *documents*.
    """
    from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415

    model = _get_rerank_model()
    if not isinstance(model, TextCrossEncoder):
        raise TypeError(f"Expected TextCrossEncoder, got {type(model)}")
    return [float(s) for s in model.rerank(query, documents)]


async def _rerank(query: str, documents: list[str]) -> list[float]:
    """Async wrapper around :func:`_rerank_sync` using the thread pool."""
    return await asyncio.to_thread(_rerank_sync, query, documents)


def _embed_sync(texts: list[str]) -> list[list[float]]:
    """Embed *texts* synchronously (runs in a thread pool via asyncio.to_thread).

    Uses ``jinaai/jina-embeddings-v2-base-code`` (768 dims, 8 192-token context)
    which is trained on code and substantially outperforms general English models
    for code retrieval tasks.
    """
    from fastembed import TextEmbedding  # noqa: PLC0415

    model = _get_model()
    if not isinstance(model, TextEmbedding):
        raise TypeError(f"Expected TextEmbedding, got {type(model)}")
    embeddings = list(model.embed(texts))
    # Convert numpy arrays to plain Python lists of floats.
    return [[float(v) for v in e.tolist()] for e in embeddings]


async def _embed(texts: list[str]) -> list[list[float]]:
    """Async wrapper around :func:`_embed_sync` using the thread pool."""
    return await asyncio.to_thread(_embed_sync, texts)


# ── BM25 sparse vector computation ───────────────────────────────────────────


def _compute_bm25_vectors_sync(texts: list[str]) -> list[dict[int, float]]:
    """Compute BM25 sparse vectors for a batch of *texts*.

    Uses FastEmbed's ``Qdrant/bm25`` model which applies corpus-aware IDF
    weights pre-trained on a large text corpus.  This produces far better
    discriminative power than a hash-based toy implementation because:

    - Rare, information-dense tokens (``ensure_worktree``, ``persist_agent_event``)
      receive high IDF scores.
    - Common tokens (``def``, ``self``, ``return``) receive low IDF scores.
    - No hash collisions — the model uses a proper vocabulary.

    Args:
        texts: Batch of strings to vectorise.

    Returns:
        One ``{index: score}`` dict per input text, in the same order.
        Qdrant accepts this format for sparse vectors.
    """
    from fastembed.sparse import SparseTextEmbedding  # noqa: PLC0415

    model = _get_bm25_model()
    if not isinstance(model, SparseTextEmbedding):
        raise TypeError(f"Expected SparseTextEmbedding, got {type(model)}")
    return [
        {int(idx): float(val) for idx, val in zip(emb.indices, emb.values)}
        for emb in model.embed(texts)
    ]


async def _compute_bm25_vectors(texts: list[str]) -> list[dict[int, float]]:
    """Async wrapper around :func:`_compute_bm25_vectors_sync` using the thread pool."""
    return await asyncio.to_thread(_compute_bm25_vectors_sync, texts)


# ── File walking and chunking ─────────────────────────────────────────────────


def _should_index(path: Path) -> bool:
    """Return True when *path* is a text file we want to embed."""
    if path.suffix not in _TEXT_EXTENSIONS:
        return False
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def _walk_files(repo_path: Path) -> list[Path]:
    """Return all indexable source files under *repo_path*, sorted."""
    results: list[Path] = []
    for child in sorted(repo_path.rglob("*")):
        if child.is_dir():
            continue
        parts = child.parts
        # Prune whole subtrees matching single-directory skip list.
        if any(part in _SKIP_DIRS for part in parts):
            continue
        # Prune subtrees matching consecutive path-segment pairs (e.g. alembic/versions).
        if any(
            any(
                parts[i] == a and i + 1 < len(parts) and parts[i + 1] == b
                for i in range(len(parts) - 1)
            )
            for a, b in _SKIP_PATH_PAIRS
        ):
            continue
        if _should_index(child):
            results.append(child)
    return results


class _ChunkSpec(TypedDict):
    """Internal: a raw chunk before embedding."""

    chunk_id: int
    file: str
    text: str
    start_line: int
    end_line: int
    symbol: str  # symbol name for AST chunks (e.g. "class Foo"); empty for char chunks
    file_hash: str  # SHA-256 hex digest of the whole file; same value for every chunk of the same file


def _chunk_file_ast(path: Path, repo_root: Path) -> list[_ChunkSpec]:
    """Split a Python file into symbol-level chunks using AST parsing.

    **Top-level functions** each become a single chunk.

    **Top-level classes** are split into:
    - A *class header* chunk: the class definition, docstring, and any
      class-level attributes up to the first method body.  This preserves
      class-level context (``__slots__``, class variables, type annotations)
      without duplicating all method bodies.
    - One *method chunk* per ``FunctionDef`` / ``AsyncFunctionDef`` in the
      class body, labelled ``"class Foo > def bar"``.  This enables precise
      retrieval of individual methods even in large classes.

    Falls back to character-based chunking if AST parsing fails.
    """
    import ast

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = str(path.relative_to(repo_root))

    try:
        tree = ast.parse(raw, filename=str(path))
    except SyntaxError:
        return _chunk_file_char(path, repo_root, raw, rel)

    chunks: list[_ChunkSpec] = []
    lines = raw.splitlines(keepends=True)

    def _node_start(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
        """Return the first line of *node* including any decorators (1-indexed)."""
        if node.decorator_list:
            return node.decorator_list[0].lineno
        return node.lineno

    def _node_end(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
        """Return the last line of *node* (1-indexed)."""
        return node.end_lineno if node.end_lineno is not None else node.lineno

    def _make_chunk(key: str, symbol: str, start: int, end: int) -> _ChunkSpec:
        text = "".join(lines[start - 1 : end])
        raw_hash = hashlib.md5(f"{rel}:{key}".encode()).hexdigest()
        chunk_id = int(raw_hash, 16) % (2**62)
        return _ChunkSpec(
            chunk_id=chunk_id,
            file=rel,
            text=text,
            start_line=start,
            end_line=end,
            symbol=symbol,
            file_hash="",
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "def" if isinstance(node, ast.FunctionDef) else "async def"
            chunks.append(_make_chunk(
                key=node.name,
                symbol=f"{kind} {node.name}",
                start=_node_start(node),
                end=_node_end(node),
            ))

        elif isinstance(node, ast.ClassDef):
            class_start = _node_start(node)
            class_end = _node_end(node)

            methods = [
                child for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]

            if methods:
                # Class header: from class decorator/definition up to just
                # before the first method body (exclusive).
                first_m_start = _node_start(methods[0])
                header_end = first_m_start - 1
                header_text = "".join(lines[class_start - 1 : header_end]).rstrip()
                if header_text:
                    chunks.append(_make_chunk(
                        key=f"{node.name}:header",
                        symbol=f"class {node.name}",
                        start=class_start,
                        end=header_end,
                    ))

                for method in methods:
                    m_kind = "def" if isinstance(method, ast.FunctionDef) else "async def"
                    chunks.append(_make_chunk(
                        key=f"{node.name}.{method.name}",
                        symbol=f"class {node.name} > {m_kind} {method.name}",
                        start=_node_start(method),
                        end=_node_end(method),
                    ))
            else:
                # No methods (e.g. NamedTuple, pure-data class): emit whole class.
                chunks.append(_make_chunk(
                    key=node.name,
                    symbol=f"class {node.name}",
                    start=class_start,
                    end=class_end,
                ))

    if not chunks:
        return _chunk_file_char(path, repo_root, raw, rel)

    # Hard-cap: split any AST chunk that exceeds _MAX_CHUNK_CHARS into
    # _CHUNK_SIZE-character sub-chunks so the embedder never receives an
    # oversized input.  A large function or data-only class can easily exceed
    # this limit; the sub-chunks inherit the parent's file/symbol metadata.
    capped: list[_ChunkSpec] = []
    for spec in chunks:
        if len(spec["text"]) <= _MAX_CHUNK_CHARS:
            capped.append(spec)
            continue
        # Char-split the oversized chunk, keeping the parent symbol in context.
        text = spec["text"]
        file_path = spec["file"]
        symbol = spec["symbol"]
        start_line = spec["start_line"]
        end_line = spec["end_line"]
        file_hash = spec["file_hash"]
        sub_start = 0
        sub_idx = 0
        while sub_start < len(text):
            sub_end = min(sub_start + _CHUNK_SIZE, len(text))
            sub_text = text[sub_start:sub_end]
            raw_hash = hashlib.md5(
                f"{file_path}:{symbol}:sub{sub_idx}".encode()
            ).hexdigest()
            capped.append(_ChunkSpec(
                chunk_id=int(raw_hash, 16) % (2**62),
                file=file_path,
                text=sub_text,
                start_line=start_line,
                end_line=end_line,
                symbol=f"{symbol} [part {sub_idx + 1}]",
                file_hash=file_hash,
            ))
            sub_start += _CHUNK_SIZE - _CHUNK_OVERLAP
            sub_idx += 1

    return capped


def _chunk_file_char(
    path: Path, repo_root: Path, raw: str | None = None, rel: str | None = None
) -> list[_ChunkSpec]:
    """Split a file into overlapping character-level chunks.

    This is the fallback strategy for non-Python files or Python files
    that cannot be parsed with AST.
    """
    if raw is None:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

    if rel is None:
        rel = str(path.relative_to(repo_root))

    chunks: list[_ChunkSpec] = []
    start = 0
    chunk_idx = 0

    while start < len(raw):
        end = min(start + _CHUNK_SIZE, len(raw))
        text = raw[start:end]
        # Derive 1-based line numbers from the full file text up to this chunk.
        start_line = raw[:start].count("\n") + 1
        end_line = start_line + text.count("\n")
        # Deterministic int ID from file + chunk index so re-indexing is idempotent.
        raw_hash = hashlib.md5(f"{rel}:{chunk_idx}".encode()).hexdigest()
        chunk_id = int(raw_hash, 16) % (2**62)
        chunks.append(
            _ChunkSpec(
                chunk_id=chunk_id,
                file=rel,
                text=text,
                start_line=start_line,
                end_line=end_line,
                symbol="",
                file_hash="",  # stamped by index_codebase after hashing the file
            )
        )
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
        chunk_idx += 1

    return chunks


def _chunk_file(path: Path, repo_root: Path) -> list[_ChunkSpec]:
    """Split *path* into chunks using the appropriate strategy.

    Python files are chunked by top-level symbols (classes, functions) via AST.
    All other files use overlapping character-level chunks.
    """
    if path.suffix == ".py":
        return _chunk_file_ast(path, repo_root)
    return _chunk_file_char(path, repo_root)


# ── File hashing ─────────────────────────────────────────────────────────────


def _compute_file_hash(path: Path) -> str:
    """Return the SHA-256 hex digest of *path*'s contents.

    Used to detect whether a file has changed since it was last indexed.
    Returns an empty string when the file cannot be read (e.g. a race
    condition between walking and hashing).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _index_version_is_current(payload: dict[str, JsonValue]) -> bool:
    """Return True when *payload* contains the current :data:`_INDEX_VERSION`.

    Extracted from :func:`_needs_index_rebuild` so the version check logic
    can be tested without a live Qdrant client or async context.
    """
    return payload.get("_index_version") == _INDEX_VERSION


async def _needs_index_rebuild(client: "AsyncQdrantClient", collection: str) -> bool:
    """Return True when the stored index version does not match :data:`_INDEX_VERSION`.

    Fetches a single point from *collection* and checks its ``_index_version``
    payload field via :func:`_index_version_is_current`.  If the field is absent
    (pre-upgrade points) or holds an older version string, a full forced rebuild
    is required to replace the stale vectors.  Returns ``False`` when the
    collection is empty or when Qdrant is unreachable — neither case requires a
    rebuild.
    """
    try:
        result = await client.scroll(
            collection_name=collection,
            scroll_filter=None,
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        points, _ = result
        if not points:
            return False
        return not _index_version_is_current(points[0].payload or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ code_indexer — could not check BM25 version (skipping rebuild check): %s",
            exc,
        )
        return False


async def _fetch_indexed_hashes(
    client: "AsyncQdrantClient", collection: str
) -> dict[str, str]:
    """Return a mapping of ``{relative_file_path: file_hash}`` from Qdrant.

    Scrolls through all points in *collection* and collects the ``file`` and
    ``file_hash`` payload fields.  Points without a ``file_hash`` field are
    ignored — they were indexed before this feature was introduced and will be
    re-indexed on the next run.

    Returns an empty dict when the collection does not exist or when Qdrant
    is unreachable (the caller falls back to full re-indexing).
    """
    from qdrant_client.http.models import ExtendedPointId  # noqa: PLC0415

    hashes: dict[str, str] = {}
    offset: ExtendedPointId | None = None

    try:
        while True:
            result = await client.scroll(
                collection_name=collection,
                scroll_filter=None,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, next_offset = result
            for point in points:
                payload = point.payload or {}
                file_val = payload.get("file")
                hash_val = payload.get("file_hash")
                if isinstance(file_val, str) and isinstance(hash_val, str):
                    # Last writer wins — all chunks for a file share the same hash.
                    hashes[file_val] = hash_val
            if next_offset is None:
                break
            offset = next_offset
    except Exception as exc:
        logger.warning(
            "⚠️ code_indexer — could not fetch indexed hashes (will re-index all): %s",
            exc,
        )
        return {}

    return hashes


# ── Qdrant helpers ────────────────────────────────────────────────────────────

# Dynamic batching — padded-cost model.
#
# FastEmbed passes the entire batch to ONNX as a single tensor padded to the
# LONGEST sequence.  The attention compute and peak memory scale as:
#
#   cost = n_chunks × max_seq_len²   (O(n²) attention per chunk, n chunks)
#
# Capping Σ(lengths) (our previous approach) is wrong: a batch of 27 small
# chunks plus one 6 268-char chunk pads ALL 27 to ~1 567 tokens, making the
# effective cost 27 × 1 567² ≈ 66 M — far above the budget — while the sum
# of lengths (say 18 000 chars) looks safe.
#
# Correct constraint: n_chunks × max_len ≤ _MAX_PADDED_CHARS where
# _MAX_PADDED_CHARS = 16 × 1 500 = 24 000 (16 typical chunks at 1 500 chars).
# This means the padded tensor fed to ONNX is never wider than it would be
# for a homogeneous batch of 16 average-sized chunks.
#
# Examples:
#   16 × 1 500-char chunks → 16 × 1 500 = 24 000 → batch of 16  (typical)
#    3 × 6 268-char chunks → 3  × 6 268 = 18 804 → batch of  3  (large fns)
#    4 × 6 268-char chunks → 4  × 6 268 = 25 072 > 24 000 → flush at 3
#    1 × 14 000-char chunk → 1  × 14 000 = 14 000 → solo batch  (outlier)
_MAX_PADDED_CHARS = 24_000


async def _ensure_collection(client: "AsyncQdrantClient", collection: str) -> None:
    """Create or migrate the Qdrant collection for hybrid dense+sparse search.

    If the collection does not exist it is created with named vectors: ``dense``
    (FastEmbed cosine) and ``sparse`` (BM25).

    If the collection exists but uses the legacy single-unnamed-vector schema
    (introduced before hybrid search), it is deleted and recreated so that
    hybrid search works correctly.  The caller's indexing loop re-populates it
    immediately after this function returns.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        Distance,
        Modifier,
        SparseVectorParams,
        VectorParams,
    )

    collections_response = await client.get_collections()
    existing = {c.name for c in collections_response.collections}

    if collection in existing:
        col_info = await client.get_collection(collection)
        vectors = col_info.config.params.vectors
        if isinstance(vectors, VectorParams):
            # Legacy schema: single unnamed vector.  Recreate for hybrid search.
            logger.warning(
                "⚠️ code_indexer — collection '%s' has legacy single-vector schema; "
                "recreating for hybrid dense+sparse search (data will be re-indexed)",
                collection,
            )
            await client.delete_collection(collection)
        else:
            logger.info("✅ code_indexer — collection '%s' schema is current", collection)
            return

    logger.info("✅ code_indexer — creating collection '%s'", collection)
    await client.create_collection(
        collection_name=collection,
        vectors_config={
            "dense": VectorParams(
                size=settings.embed_model_dim,
                distance=Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            # Modifier.IDF enables proper BM25 term-frequency weighting in
            # Qdrant's sparse retrieval.  Without it, Qdrant treats sparse
            # vectors as plain dot-product and ignores IDF — common tokens
            # like "def" or "self" are not down-weighted, degrading retrieval
            # quality for code search.  The FastEmbed Qdrant/bm25 model
            # pre-computes IDF-scaled token weights; this modifier tells
            # Qdrant to apply corpus-level IDF on top during query scoring.
            "sparse": SparseVectorParams(modifier=Modifier.IDF),
        },
    )


async def _delete_chunks_by_file(
    client: "AsyncQdrantClient", collection: str, file_path: str
) -> None:
    """Delete all Qdrant points whose ``file`` payload matches *file_path*.

    Used to remove stale chunks when a file changes or is deleted from disk.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        FilterSelector,
        MatchValue,
    )

    await client.delete(
        collection_name=collection,
        points_selector=FilterSelector(
            filter=Filter(
                must=[FieldCondition(key="file", match=MatchValue(value=file_path))]
            )
        ),
    )


async def _ensure_payload_index(client: "AsyncQdrantClient", collection: str) -> None:
    """Create a keyword payload index on the ``file`` field if not present.

    This index lets callers filter search results by file path server-side
    (e.g. restrict to ``agentception/services/``) without pulling irrelevant
    chunks into Python.  Qdrant silently ignores the call when the index
    already exists, so this is safe to call on every index run.
    """
    from qdrant_client.models import PayloadSchemaType  # noqa: PLC0415

    await client.create_payload_index(
        collection_name=collection,
        field_name="file",
        field_schema=PayloadSchemaType.KEYWORD,
    )


# ── Public API ────────────────────────────────────────────────────────────────


async def index_codebase(
    repo_path: Path | None = None,
    *,
    qdrant_url: str | None = None,
    collection: str | None = None,
    force_full: bool = False,
) -> IndexStats:
    """Index every source file in *repo_path* into Qdrant using hash-based incremental mode.

    Incremental mode (default, ``force_full=False``): computes a SHA-256 content
    hash per file and compares it with the stored ``file_hash`` payload field in
    Qdrant.  Only files that are new, changed, or deleted incur Qdrant writes.
    Unchanged files are skipped entirely — zero Qdrant calls.  The ``file_hash``
    is stored in every chunk payload so subsequent runs can detect changes.
    Changed files have their old chunks deleted before new chunks are upserted.
    Files deleted from disk have all their chunks removed from Qdrant.

    Force-full mode (``force_full=True``): drops the collection and rebuilds it
    from scratch, regardless of stored hashes.  Use for explicit clean rebuilds
    only — schema migration is handled by :func:`_ensure_collection` and is not
    duplicated here.

    This is a long-running operation (seconds to minutes for large repos).
    Call it from a :class:`fastapi.BackgroundTasks` task so it does not block
    the HTTP response.

    Args:
        repo_path: Root of the repository to index.  Defaults to
            ``settings.repo_dir``.
        qdrant_url: Override the Qdrant URL (useful in tests).
        collection: Override the collection name (useful in tests).
        force_full: Drop and rebuild the collection from scratch when ``True``.
            Defaults to ``False`` (incremental mode).

    Returns:
        :class:`IndexStats` with the number of files and chunks indexed,
        or an error description when indexing fails.
    """
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415
    from qdrant_client.models import PointStruct  # noqa: PLC0415

    root = repo_path or settings.repo_dir
    url = qdrant_url or settings.qdrant_url
    coll = collection or settings.qdrant_collection

    logger.info("✅ code_indexer — start indexing %s → %s/%s", root, url, coll)

    try:
        files = _walk_files(root)
        logger.info("✅ code_indexer — found %d indexable files", len(files))

        client = AsyncQdrantClient(url=url)
        try:
            if force_full:
                # Drop the collection so _ensure_collection recreates it fresh.
                collections_response = await client.get_collections()
                existing = {c.name for c in collections_response.collections}
                if coll in existing:
                    logger.info(
                        "✅ code_indexer — force_full: dropping collection '%s'", coll
                    )
                    await client.delete_collection(coll)

            await _ensure_collection(client, coll)

            # Detect index pipeline changes — auto-trigger full rebuild.
            # Any change to chunking strategy, embedding model, BM25
            # implementation, or chunk text enrichment format requires a
            # clean rebuild so all vectors are consistent.
            if not force_full and await _needs_index_rebuild(client, coll):
                logger.info(
                    "✅ code_indexer — index version mismatch (want %s); "
                    "triggering full rebuild",
                    _INDEX_VERSION,
                )
                await client.delete_collection(coll)
                await _ensure_collection(client, coll)
                force_full = True

            # Ensure file-path keyword index for filtered search.
            await _ensure_payload_index(client, coll)

            # Incremental: build file→hash map from Qdrant.
            # Empty when force_full since the collection was just dropped.
            indexed_hashes = (
                {} if force_full else await _fetch_indexed_hashes(client, coll)
            )

            all_chunks: list[_ChunkSpec] = []
            current_files: set[str] = set()
            files_skipped = 0

            for f in files:
                rel = str(f.relative_to(root))
                current_files.add(rel)
                current_hash = _compute_file_hash(f)

                if current_hash and indexed_hashes.get(rel) == current_hash:
                    files_skipped += 1
                    logger.debug("  skipping unchanged file: %s", rel)
                    continue

                # Changed file: delete stale chunks before upserting new ones.
                if rel in indexed_hashes:
                    await _delete_chunks_by_file(client, coll, rel)
                    logger.debug("  deleted stale chunks for changed file: %s", rel)

                chunks = _chunk_file(f, root)
                # Stamp the file hash onto every chunk so it is stored in Qdrant.
                for c in chunks:
                    c["file_hash"] = current_hash
                all_chunks.extend(chunks)

            # Deleted files: present in Qdrant but no longer on disk.
            for deleted_rel in set(indexed_hashes) - current_files:
                await _delete_chunks_by_file(client, coll, deleted_rel)
                logger.info(
                    "✅ code_indexer — deleted chunks for removed file: %s", deleted_rel
                )

            files_indexed = len(files) - files_skipped
            logger.info(
                "✅ code_indexer — %d chunks from %d files (%d skipped as unchanged)",
                len(all_chunks),
                files_indexed,
                files_skipped,
            )

            # Embed and upsert in batches.
            import resource as _resource  # noqa: PLC0415
            import time as _time  # noqa: PLC0415

            from qdrant_client.models import SparseVector  # noqa: PLC0415

            def _rss_mb() -> float:
                """Return current process RSS in MiB (Linux: ru_maxrss is in KiB)."""
                return _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss / 1024

            # Pre-compute enriched texts for all chunks so we can measure each
            # chunk's length before deciding batch boundaries.  Enrichment
            # prepends the file path and symbol name to anchor the dense vector.
            all_enriched: list[str] = [
                f"# {c['file']}\n# {c['symbol']}\n{c['text']}"
                if c["symbol"]
                else f"# {c['file']}\n{c['text']}"
                for c in all_chunks
            ]

            # Build dynamic batches using the padded-cost model.
            # Adding a new chunk raises the effective cost to
            # (n + 1) × max(cur_max_len, new_len).  Flush the current batch
            # before that cost would exceed _MAX_PADDED_CHARS.
            dyn_batches: list[tuple[list[_ChunkSpec], list[str]]] = []
            cur_chunks: list[_ChunkSpec] = []
            cur_texts: list[str] = []
            cur_max_len = 0
            for chunk, text in zip(all_chunks, all_enriched):
                text_len = len(text)
                projected_max = max(cur_max_len, text_len)
                projected_cost = (len(cur_chunks) + 1) * projected_max
                if cur_chunks and projected_cost > _MAX_PADDED_CHARS:
                    dyn_batches.append((cur_chunks, cur_texts))
                    cur_chunks = [chunk]
                    cur_texts = [text]
                    cur_max_len = text_len
                else:
                    cur_chunks.append(chunk)
                    cur_texts.append(text)
                    cur_max_len = projected_max
            if cur_chunks:
                dyn_batches.append((cur_chunks, cur_texts))

            n_batches = len(dyn_batches)
            batch_errors = 0
            chunk_offset = 0

            for batch_num, (batch, embed_texts) in enumerate(dyn_batches, start=1):
                batch_files = sorted({c["file"] for c in batch})
                t_batch = _time.monotonic()
                rss_before = _rss_mb()

                padded_cost = len(batch) * max(len(t) for t in embed_texts)
                logger.info(
                    "✅ code_indexer — batch %d/%d [chunks %d–%d] "
                    "n=%d max=%d cost=%d rss=%.0fMiB files: %s",
                    batch_num,
                    n_batches,
                    chunk_offset,
                    chunk_offset + len(batch) - 1,
                    len(batch),
                    max(len(t) for t in embed_texts),
                    padded_cost,
                    rss_before,
                    batch_files[:3],
                )
                chunk_offset += len(batch)

                try:
                    t0 = _time.monotonic()
                    dense_vectors = await _embed(embed_texts)
                    rss_after = _rss_mb()
                    logger.info(
                        "✅ code_indexer —   embed: %.1fs (%d chunks, max %d chars) rss=%.0fMiB Δ%+.0fMiB",
                        _time.monotonic() - t0,
                        len(batch),
                        max(len(t) for t in embed_texts),
                        rss_after,
                        rss_after - rss_before,
                    )
                except Exception as embed_exc:
                    logger.exception(
                        "❌ code_indexer — embed FAILED on batch %d/%d: %s",
                        batch_num, n_batches, embed_exc,
                    )
                    batch_errors += 1
                    continue

                try:
                    t0 = _time.monotonic()
                    sparse_vectors = await _compute_bm25_vectors(embed_texts)
                    logger.info(
                        "✅ code_indexer —   bm25:  %.1fs", _time.monotonic() - t0
                    )
                except Exception as bm25_exc:
                    logger.exception(
                        "❌ code_indexer — BM25 FAILED on batch %d/%d: %s",
                        batch_num, n_batches, bm25_exc,
                    )
                    batch_errors += 1
                    continue

                try:
                    points = [
                        PointStruct(
                            id=chunk["chunk_id"],
                            vector={
                                "dense": dense_vec,
                                "sparse": SparseVector(
                                    indices=list(sparse_vec.keys()),
                                    values=list(sparse_vec.values()),
                                ),
                            },
                            payload={
                                "file": chunk["file"],
                                "chunk": chunk["text"],
                                "start_line": chunk["start_line"],
                                "end_line": chunk["end_line"],
                                "symbol": chunk["symbol"],
                                "file_hash": chunk["file_hash"],
                                "_index_version": _INDEX_VERSION,
                            },
                        )
                        for chunk, dense_vec, sparse_vec in zip(batch, dense_vectors, sparse_vectors)
                    ]
                    t0 = _time.monotonic()
                    await client.upsert(collection_name=coll, points=points)
                    logger.info(
                        "✅ code_indexer —   upsert: %.1fs | batch total: %.1fs",
                        _time.monotonic() - t0,
                        _time.monotonic() - t_batch,
                    )
                except Exception as upsert_exc:
                    logger.exception(
                        "❌ code_indexer — upsert FAILED on batch %d/%d: %s",
                        batch_num, n_batches, upsert_exc,
                    )
                    batch_errors += 1
                    continue

            if batch_errors:
                logger.warning(
                    "⚠️ code_indexer — %d batch(es) failed and were skipped", batch_errors
                )
        finally:
            await client.close()

    except Exception as exc:
        logger.exception("❌ code_indexer — indexing failed: %s", exc)
        return IndexStats(
            ok=False,
            files_indexed=0,
            chunks_indexed=0,
            files_skipped=0,
            error=str(exc),
        )

    logger.info(
        "✅ code_indexer — done: %d files indexed, %d skipped, %d chunks",
        files_indexed,
        files_skipped,
        len(all_chunks),
    )
    return IndexStats(
        ok=True,
        files_indexed=files_indexed,
        chunks_indexed=len(all_chunks),
        files_skipped=files_skipped,
        error=None,
    )


async def search_codebase(
    query: str,
    n_results: int = 5,
    *,
    qdrant_url: str | None = None,
    collection: str | None = None,
) -> list[SearchMatch]:
    """Search the indexed codebase for chunks relevant to *query*.

    Pipeline:
    1. **Embed** the query with the same code-specific dense model used at
       index time (``jinaai/jina-embeddings-v2-base-code``).
    2. **Hybrid retrieval** — Qdrant fetches ``n_results * 4`` candidates by
       running dense and sparse (BM25) searches in parallel server-side and
       fusing them with Reciprocal Rank Fusion (RRF) in a single round-trip.
    3. **Rerank** — a cross-encoder (``BAAI/bge-reranker-base``) scores each
       candidate jointly against the query and re-orders the list, cutting
       false positives that slipped through the retrieval phase.

    Args:
        query: Natural-language or code-level description of what to find.
        n_results: Maximum results to return after reranking.
        qdrant_url: Override the Qdrant URL (useful in tests).
        collection: Override the collection name (useful in tests).

    Returns:
        List of :class:`SearchMatch` dicts ordered by descending reranker
        score (or RRF score when reranking is disabled).  Returns an empty
        list when the collection has not been indexed yet or Qdrant is
        unavailable.
    """
    import os as _os_sc
    import psutil as _psutil_sc
    import time as _time_sc
    _p_sc = _psutil_sc.Process(_os_sc.getpid())
    _sc_rss_start = _p_sc.memory_info().rss // 1024 // 1024
    _sc_t0 = _time_sc.monotonic()
    logger.warning("📊 search_codebase START query=%r coll=%s RSS=%dMB", query[:60], collection or "(default)", _sc_rss_start)

    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415
    from qdrant_client.models import (  # noqa: PLC0415
        Fusion,
        FusionQuery,
        Prefetch,
        SparseVector,
    )

    url = qdrant_url or settings.qdrant_url
    coll = collection or settings.qdrant_collection

    # Fetch more candidates than needed so the reranker has room to reorder.
    fetch_limit = n_results * 4

    try:
        # Compute query vectors (dense + sparse) concurrently.
        dense_vecs, bm25_vecs = await asyncio.gather(
            _embed([query]),
            _compute_bm25_vectors([query]),
        )
        logger.warning("📊 search_codebase VECTORS_DONE query=%r RSS=%dMB elapsed=%.1fs", query[:60], _p_sc.memory_info().rss // 1024 // 1024, _time_sc.monotonic() - _sc_t0)
        dense_query = dense_vecs[0]
        sparse_dict = bm25_vecs[0]
        sparse_query = SparseVector(
            indices=list(sparse_dict.keys()),
            values=list(sparse_dict.values()),
        )

        client = AsyncQdrantClient(url=url)
        try:
            # Native Qdrant hybrid search: prefetch dense + sparse in parallel,
            # fuse server-side with RRF — single round-trip, no Python fusion.
            response = await client.query_points(
                collection_name=coll,
                prefetch=[
                    Prefetch(query=dense_query, using="dense", limit=fetch_limit),
                    Prefetch(query=sparse_query, using="sparse", limit=fetch_limit),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                limit=fetch_limit,
                with_payload=True,
            )
            candidates = response.points
        finally:
            await client.close()

        # Extract payload fields, dropping malformed points.
        class _Candidate:
            __slots__ = ("file", "chunk", "start_line", "end_line", "rrf_score")

            def __init__(
                self,
                file: str,
                chunk: str,
                start_line: int,
                end_line: int,
                rrf_score: float,
            ) -> None:
                self.file = file
                self.chunk = chunk
                self.start_line = start_line
                self.end_line = end_line
                self.rrf_score = rrf_score

        valid: list[_Candidate] = []
        for point in candidates:
            payload = point.payload or {}
            file_val = payload.get("file")
            chunk_val = payload.get("chunk")
            if not isinstance(file_val, str) or not isinstance(chunk_val, str):
                continue
            start_val = payload.get("start_line")
            end_val = payload.get("end_line")
            valid.append(_Candidate(
                file=file_val,
                chunk=chunk_val,
                start_line=int(start_val) if isinstance(start_val, int) else 0,
                end_line=int(end_val) if isinstance(end_val, int) else 0,
                rrf_score=float(point.score),
            ))

        if not valid:
            logger.warning("📊 search_codebase NO_RESULTS query=%r RSS=%dMB elapsed=%.1fs", query[:60], _p_sc.memory_info().rss // 1024 // 1024, _time_sc.monotonic() - _sc_t0)
            return []

        # Cross-encoder reranking: score each candidate against the query and
        # re-order by relevance.  Skip when rerank_model is empty (test override).
        if settings.rerank_model and len(valid) > 1:
            documents = [c.chunk for c in valid]
            rerank_scores = await _rerank(query, documents)
            ranked = sorted(
                zip(valid, rerank_scores),
                key=lambda pair: pair[1],
                reverse=True,
            )
            _result = [
                SearchMatch(
                    file=c.file,
                    chunk=c.chunk,
                    score=score,
                    start_line=c.start_line,
                    end_line=c.end_line,
                )
                for c, score in ranked[:n_results]
            ]
            logger.warning("📊 search_codebase DONE query=%r hits=%d RSS=%dMB elapsed=%.1fs", query[:60], len(_result), _p_sc.memory_info().rss // 1024 // 1024, _time_sc.monotonic() - _sc_t0)
            return _result

        # Reranking disabled: return top n_results by RRF score.
        _result2 = [
            SearchMatch(
                file=c.file,
                chunk=c.chunk,
                score=c.rrf_score,
                start_line=c.start_line,
                end_line=c.end_line,
            )
            for c in valid[:n_results]
        ]
        logger.warning("📊 search_codebase DONE(no-rerank) query=%r hits=%d RSS=%dMB elapsed=%.1fs", query[:60], len(_result2), _p_sc.memory_info().rss // 1024 // 1024, _time_sc.monotonic() - _sc_t0)
        return _result2

    except Exception as exc:
        logger.warning("📊 search_codebase FAILED query=%r exc=%s RSS=%dMB elapsed=%.1fs", query[:60], exc, _p_sc.memory_info().rss // 1024 // 1024, _time_sc.monotonic() - _sc_t0)
        logger.warning("⚠️ code_indexer — search failed: %s", exc)
        return []
