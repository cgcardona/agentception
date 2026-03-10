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

Incremental Indexing
--------------------
``index_codebase`` computes a SHA-256 content hash per file and stores it as
``file_hash`` in each chunk's Qdrant payload.  On re-index, only files whose
hash differs from the stored value (new, changed, or deleted) incur Qdrant
writes.  Unchanged files are skipped entirely — zero Qdrant calls.  Pass
``force_full=True`` to drop and rebuild the collection from scratch.

Public API
----------
``index_codebase(repo_path, force_full=False)``
    Walk every readable source file in *repo_path*, chunk appropriately
    (AST for Python, character-level for others), embed with
    :data:`~agentception.config.settings.embed_model` (default
    ``BAAI/bge-small-en-v1.5``), compute BM25 sparse vectors, and upsert
    both to the Qdrant collection configured in
    :data:`~agentception.config.settings.qdrant_collection`.
    Incremental by default — only changed/new/deleted files are processed.

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
    files_skipped: int
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
    file_hash: str  # SHA-256 hex digest of the whole file; same value for every chunk of the same file


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
                file_hash="",  # stamped by index_codebase after hashing the file
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

_UPSERT_BATCH = 64  # points per upsert call


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
            "sparse": SparseVectorParams(),
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
                            "file_hash": chunk["file_hash"],
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
