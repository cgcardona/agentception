"""Qdrant-backed semantic code search for the Cursor-free agent loop.

Replaces Cursor's ``@Codebase`` feature with a self-hosted vector store.
Files are chunked, embedded with a local ONNX model (FastEmbed), and stored
in Qdrant.  The agent then searches with natural language instead of regex.

Public API
----------
``index_codebase(repo_path)``
    Walk every readable source file in *repo_path*, split into overlapping
    character-level chunks, embed with :data:`~agentception.config.settings.embed_model`
    (default ``BAAI/bge-small-en-v1.5``), and upsert to the Qdrant
    collection configured in :data:`~agentception.config.settings.qdrant_collection`.

``search_codebase(query, n_results)``
    Embed *query* with the same model and return the top *n_results* code
    chunks from Qdrant, ordered by cosine similarity.

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


def _chunk_file(path: Path, repo_root: Path) -> list[_ChunkSpec]:
    """Split *path* into overlapping character-level chunks."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

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
            )
        )
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
        chunk_idx += 1

    return chunks


# ── Qdrant helpers ────────────────────────────────────────────────────────────

_UPSERT_BATCH = 64  # points per upsert call


async def _ensure_collection(client: "AsyncQdrantClient", collection: str) -> None:
    """Create the Qdrant collection if it does not exist yet."""
    from qdrant_client.models import Distance, VectorParams  # noqa: PLC0415

    collections_response = await client.get_collections()
    existing = {c.name for c in collections_response.collections}
    if collection not in existing:
        logger.info("✅ code_indexer — creating collection '%s'", collection)
        await client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(
                size=settings.embed_model_dim,
                distance=Distance.COSINE,
            ),
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
                vectors = await _embed(texts)

                points = [
                    PointStruct(
                        id=chunk["chunk_id"],
                        vector=vec,
                        payload={
                            "file": chunk["file"],
                            "chunk": chunk["text"],
                            "start_line": chunk["start_line"],
                            "end_line": chunk["end_line"],
                        },
                    )
                    for chunk, vec in zip(batch, vectors)
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

    Args:
        query: Natural-language description of what to find.
        n_results: Maximum results to return.
        qdrant_url: Override the Qdrant URL (useful in tests).
        collection: Override the collection name (useful in tests).

    Returns:
        List of :class:`SearchMatch` dicts ordered by descending relevance.
        Returns an empty list when the collection has not been indexed yet
        or when Qdrant is unavailable.
    """
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415

    url = qdrant_url or settings.qdrant_url
    coll = collection or settings.qdrant_collection

    try:
        vectors = await _embed([query])
        query_vector = vectors[0]

        client = AsyncQdrantClient(url=url)
        try:
            response = await client.query_points(
                collection_name=coll,
                query=query_vector,
                limit=n_results,
            )
            results = response.points
        finally:
            await client.close()

    except Exception as exc:
        logger.warning("⚠️ code_indexer — search failed: %s", exc)
        return []

    matches: list[SearchMatch] = []
    for point in results:
        payload = point.payload or {}
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
                score=float(point.score),
                start_line=int(start_val) if isinstance(start_val, int) else 0,
                end_line=int(end_val) if isinstance(end_val, int) else 0,
            )
        )

    return matches
