"""Qdrant-backed semantic code search for the Cursor-free agent loop.

Replaces Cursor's ``@Codebase`` feature with a self-hosted vector store.
Files are chunked, embedded with a local ONNX model (FastEmbed), and stored
in Qdrant.  The agent then searches with natural language instead of regex.

Chunking Strategy
-----------------
Python files are chunked by top-level symbols (classes, functions, async
functions) using AST parsing. Each symbol becomes a single chunk, including
its decorators, docstring, and full body. This produces semantically coherent
chunks that preserve complete definitions.

Non-Python files use overlapping character-level chunks for compatibility.

Hybrid Search
-------------
Search combines dense semantic vectors (FastEmbed) with sparse BM25 keyword
vectors. Results from both retrieval methods are fused using Reciprocal Rank
Fusion (RRF) with k=60, a standard parameter that balances the contribution
of both ranking methods. This ensures exact keyword matches (e.g., class names)
rank highly while preserving semantic similarity for natural language queries.

Public API
----------
``index_codebase(repo_path)``
    Walk every readable source file in *repo_path*, chunk appropriately
    (AST for Python, character-level for others), embed with
    :data:`~agentception.config.settings.embed_model` (default
    ``BAAI/bge-small-en-v1.5``), compute BM25 sparse vectors, and upsert
    both to the Qdrant collection configured in
    :data:`~agentception.config.settings.qdrant_collection`.

``search_codebase(query, n_results)``
    Run hybrid search combining dense semantic and sparse BM25 retrieval,
    fuse results with RRF, and return the top *n_results* code chunks.

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

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)

# ── Chunk sizing ───────────────────────────────────────────────────────────────

_CHUNK_SIZE = 1_500  # characters per chunk (≈ 50 lines of Python)
_CHUNK_OVERLAP = 200  # overlap between consecutive chunks for context continuity
_MAX_FILE_BYTES = 200_000  # skip files larger than ~200 KB

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

# ── Public result types ───────────────────────────────────────────────────────


class IndexStats(TypedDict):
    """Result returned by :func:`index_codebase`."""

    ok: bool
    files_indexed: int
    chunks_indexed: int
    error: str | None


class SearchMatch(TypedDict):
    """A single result from :func:`search_codebase`."""

    file: str
    chunk: str
    score: float
    start_line: int
    end_line: int


# ── Module-level embedding model cache ───────────────────────────────────────
# Lazily initialised on first use so tests can monkey-patch before loading.

_cached_model: object = None  # TextEmbedding instance after first load


def _get_model() -> object:
    """Return the cached TextEmbedding model, initialising it on first call."""
    global _cached_model
    if _cached_model is None:
        from fastembed import TextEmbedding  # noqa: PLC0415

        logger.info("✅ code_indexer — loading embed model: %s", settings.embed_model)
        _cached_model = TextEmbedding(model_name=settings.embed_model)
    return _cached_model


def _reset_model() -> None:
    """Clear the cached model — used by tests to inject a mock."""
    global _cached_model
    _cached_model = None


def _embed_sync(texts: list[str]) -> list[list[float]]:
    """Embed *texts* synchronously (runs in a thread pool via asyncio.to_thread)."""
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


def _compute_bm25_vector(text: str, vocab_size: int = 10000) -> dict[int, float]:
    """Compute a BM25-style sparse vector for *text*.

    Returns a dictionary mapping token indices to BM25 scores. This is a
    simplified BM25 implementation suitable for single-document scoring
    without a corpus-wide IDF calculation.

    Args:
        text: The text to vectorize.
        vocab_size: Maximum vocabulary size (hash space for tokens).

    Returns:
        Sparse vector as {index: score} dict. Qdrant accepts this format
        for sparse vectors.
    """
    import re

    # Tokenize: lowercase, split on non-alphanumeric, filter short tokens.
    tokens = [t for t in re.findall(r"\w+", text.lower()) if len(t) > 2]
    if not tokens:
        return {}

    # Compute term frequencies.
    tf: dict[str, int] = {}
    for token in tokens:
        tf[token] = tf.get(token, 0) + 1

    # BM25 parameters (standard values).
    k1 = 1.5
    b = 0.75
    avgdl = 100.0  # Assume average document length of 100 tokens.
    doc_len = len(tokens)

    # Compute BM25 score for each unique token.
    sparse_vec: dict[int, float] = {}
    for token, freq in tf.items():
        # Hash token to an index in [0, vocab_size).
        token_idx = hash(token) % vocab_size
        # BM25 formula (without IDF, which requires corpus statistics).
        # We use a simplified version: score = (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * doc_len / avgdl))
        score = (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * doc_len / avgdl))
        sparse_vec[token_idx] = score

    return sparse_vec


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
        # Prune whole subtrees early.
        if any(part in _SKIP_DIRS for part in child.parts):
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


def _chunk_file_ast(path: Path, repo_root: Path) -> list[_ChunkSpec]:
    """Split a Python file into chunks by top-level symbols (classes, functions).

    Each top-level class or function becomes a single chunk, including its
    decorators, docstring, and full body. This produces semantically coherent
    chunks that preserve complete definitions.

    Falls back to character-based chunking if AST parsing fails.
    """
    import ast

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = str(path.relative_to(repo_root))

    # Try to parse as Python AST.
    try:
        tree = ast.parse(raw, filename=str(path))
    except SyntaxError:
        # Fall back to character chunking for malformed Python.
        return _chunk_file_char(path, repo_root, raw, rel)

    chunks: list[_ChunkSpec] = []
    lines = raw.splitlines(keepends=True)

    for idx, node in enumerate(tree.body):
        # Only chunk top-level classes and functions.
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Extract the full source for this node, including decorators.
        start_line = node.lineno
        end_line = node.end_lineno or start_line

        # Include decorators if present.
        if node.decorator_list:
            first_decorator = node.decorator_list[0]
            start_line = first_decorator.lineno

        # Extract text from the original source (1-indexed lines).
        text = "".join(lines[start_line - 1 : end_line])

        # Generate deterministic chunk ID.
        raw_hash = hashlib.md5(f"{rel}:{node.name}".encode()).hexdigest()
        chunk_id = int(raw_hash, 16) % (2**62)

        kind = "class" if isinstance(node, ast.ClassDef) else "def"
        chunks.append(
            _ChunkSpec(
                chunk_id=chunk_id,
                file=rel,
                text=text,
                start_line=start_line,
                end_line=end_line,
                symbol=f"{kind} {node.name}",
            )
        )

    # If no top-level symbols found, fall back to character chunking.
    if not chunks:
        return _chunk_file_char(path, repo_root, raw, rel)

    return chunks


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


# ── Qdrant helpers ────────────────────────────────────────────────────────────

_UPSERT_BATCH = 64  # points per upsert call


async def _ensure_collection(client: "AsyncQdrantClient", collection: str) -> None:
    """Create the Qdrant collection if it does not exist yet.

    The collection uses named vectors: 'dense' for semantic embeddings and
    'sparse' for BM25 keyword vectors.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        Distance,
        KeywordIndexParams,
        KeywordIndexType,
        SparseVectorParams,
        VectorParams,
    )

    collections_response = await client.get_collections()
    existing = {c.name for c in collections_response.collections}
    if collection not in existing:
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
                "sparse": SparseVectorParams(),
            },
            payload_indexes_config={
                "file": KeywordIndexParams(type=KeywordIndexType.KEYWORD),
                "symbol": KeywordIndexParams(type=KeywordIndexType.KEYWORD),
            },
        )
    else:
        logger.info("✅ code_indexer — collection '%s' already exists", collection)


# ── Public API ────────────────────────────────────────────────────────────────


async def index_codebase(
    repo_path: Path | None = None,
    *,
    qdrant_url: str | None = None,
    collection: str | None = None,
) -> IndexStats:
    """Index every source file in *repo_path* into Qdrant.

    This is a long-running operation (seconds to minutes for large repos).
    Call it from a :class:`fastapi.BackgroundTasks` task so it does not block
    the HTTP response.

    Args:
        repo_path: Root of the repository to index.  Defaults to
            ``settings.repo_dir``.
        qdrant_url: Override the Qdrant URL (useful in tests).
        collection: Override the collection name (useful in tests).

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

        all_chunks: list[_ChunkSpec] = []
        for f in files:
            all_chunks.extend(_chunk_file(f, root))

        logger.info("✅ code_indexer — %d chunks from %d files", len(all_chunks), len(files))

        client = AsyncQdrantClient(url=url)
        try:
            await _ensure_collection(client, coll)

            # Embed and upsert in batches.
            for batch_start in range(0, len(all_chunks), _UPSERT_BATCH):
                batch = all_chunks[batch_start : batch_start + _UPSERT_BATCH]
                texts = [c["text"] for c in batch]
                dense_vectors = await _embed(texts)
                sparse_vectors = [_compute_bm25_vector(text) for text in texts]

                from qdrant_client.models import SparseVector  # noqa: PLC0415

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
                        },
                    )
                    for chunk, dense_vec, sparse_vec in zip(batch, dense_vectors, sparse_vectors)
                ]
                await client.upsert(collection_name=coll, points=points)
                logger.debug(
                    "  upserted batch %d–%d",
                    batch_start,
                    batch_start + len(batch),
                )
        finally:
            await client.close()

    except Exception as exc:
        logger.exception("❌ code_indexer — indexing failed: %s", exc)
        return IndexStats(ok=False, files_indexed=0, chunks_indexed=0, error=str(exc))

    logger.info(
        "✅ code_indexer — done: %d files, %d chunks",
        len(files),
        len(all_chunks),
    )
    return IndexStats(
        ok=True,
        files_indexed=len(files),
        chunks_indexed=len(all_chunks),
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

    Performs hybrid search combining dense (FastEmbed) and sparse (BM25)
    vectors, then fuses results using Reciprocal Rank Fusion (RRF) with
    k=60 (standard parameter).

    Args:
        query: Natural-language description of what to find.
        n_results: Maximum results to return.
        qdrant_url: Override the Qdrant URL (useful in tests).
        collection: Override the collection name (useful in tests).

    Returns:
        List of :class:`SearchMatch` dicts ordered by descending RRF score.
        Returns an empty list when the collection has not been indexed yet
        or when Qdrant is unavailable.
    """
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415
    from qdrant_client.models import ScoredPoint, SparseVector  # noqa: PLC0415

    url = qdrant_url or settings.qdrant_url
    coll = collection or settings.qdrant_collection

    try:
        # Compute both dense and sparse query vectors.
        dense_vectors = await _embed([query])
        dense_query = dense_vectors[0]
        sparse_dict = _compute_bm25_vector(query)
        sparse_query = SparseVector(
            indices=list(sparse_dict.keys()),
            values=list(sparse_dict.values()),
        )

        client = AsyncQdrantClient(url=url)
        try:
            # Run dense vector search.
            dense_response = await client.query_points(
                collection_name=coll,
                query=dense_query,
                using="dense",
                limit=n_results * 2,  # Fetch more for fusion.
            )
            dense_results = dense_response.points

            # Run sparse vector search.
            sparse_response = await client.query_points(
                collection_name=coll,
                query=sparse_query,
                using="sparse",
                limit=n_results * 2,  # Fetch more for fusion.
            )
            sparse_results = sparse_response.points
        finally:
            await client.close()

        # Reciprocal Rank Fusion (RRF) with k=60.
        rrf_k = 60
        rrf_scores: dict[str, float] = {}

        # Add dense results to RRF scores.
        for rank, point in enumerate(dense_results, start=1):
            point_id = str(point.id)
            rrf_scores[point_id] = rrf_scores.get(point_id, 0.0) + 1.0 / (rrf_k + rank)

        # Add sparse results to RRF scores.
        for rank, point in enumerate(sparse_results, start=1):
            point_id = str(point.id)
            rrf_scores[point_id] = rrf_scores.get(point_id, 0.0) + 1.0 / (rrf_k + rank)

        # Sort by RRF score descending and take top n_results.
        sorted_ids = sorted(rrf_scores.keys(), key=lambda pid: rrf_scores[pid], reverse=True)[:n_results]

        # Build a map of point_id -> point for payload extraction.
        all_points: dict[str, ScoredPoint] = {str(p.id): p for p in dense_results}
        all_points.update({str(p.id): p for p in sparse_results})

        # Build final results.
        matches: list[SearchMatch] = []
        for point_id in sorted_ids:
            scored_point: ScoredPoint | None = all_points.get(point_id)
            if scored_point is None:
                continue
            payload = scored_point.payload or {}
            file_val = payload.get("file")
            chunk_val = payload.get("chunk")
            start_val = payload.get("start_line")
            end_val = payload.get("end_line")
            if not isinstance(file_val, str) or not isinstance(chunk_val, str):
                continue
            matches.append(
                SearchMatch(
                    file=file_val,
                    chunk=chunk_val,
                    score=rrf_scores[point_id],
                    start_line=int(start_val) if isinstance(start_val, int) else 0,
                    end_line=int(end_val) if isinstance(end_val, int) else 0,
                )
            )

    except Exception as exc:
        logger.warning("⚠️ code_indexer — search failed: %s", exc)
        return []

    return matches
