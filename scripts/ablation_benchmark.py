#!/usr/bin/env python3
"""Ablation benchmark: isolates which search components drive quality gains.

Tests five additive configurations on a fixed 30-file corpus so each change
is attributable to a single variable (model, chunking, IDF, reranking).

Additive chain — one variable changes at a time:
  1. bge_char_noidF  — BGE-small + char chunking + no IDF modifier
  2. bge_char_idf    — BGE-small + char chunking + IDF modifier          (+IDF)
  3. bge_ast_idf     — BGE-small + AST chunking  + IDF modifier          (+AST)
  4. jina_ast_idf    — Jina-v2   + AST chunking  + IDF modifier          (+Jina)
  5. jina_ast_rerank — Jina-v2   + AST chunking  + IDF + cross-encoder   (+rerank)

Usage (stop the agentception app first; Qdrant must remain running):
    docker stop agentception
    docker compose run --rm --no-deps agentception \\
        python3 /app/scripts/ablation_benchmark.py
    docker start agentception

Compare against the live benchmark (search_benchmark.py) for production
numbers (full 5 k-chunk index, running server).
"""

from __future__ import annotations

import ast
import gc
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastembed import SparseTextEmbedding, TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from fastembed.sparse.sparse_embedding_base import SparseEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Fusion,
    FusionQuery,
    Modifier,
    Prefetch,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

logging.basicConfig(level=logging.WARNING)

# ─── Paths ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path("/app")
QDRANT_URL = "http://agentception-qdrant:6333"
COLLECTION_PREFIX = "ablation"

# ─── Fixed evaluation corpus ─────────────────────────────────────────────────
# Includes all benchmark target files plus enough distractors to make
# retrieval non-trivial.  Filter to files that actually exist at runtime.

CORPUS_PATHS: list[str] = [
    # ── target files ──────────────────────────────────────────────────────────
    "agentception/readers/git.py",
    "agentception/services/code_indexer.py",
    "agentception/services/context_assembler.py",
    "agentception/routes/api/dispatch.py",
    "agentception/db/persist.py",
    "agentception/config.py",
    "agentception/readers/issue_creator.py",
    "agentception/services/llm.py",
    "agentception/db/engine.py",
    "agentception/services/teardown.py",
    # ── distractors ───────────────────────────────────────────────────────────
    "agentception/readers/llm_phase_planner.py",
    "agentception/readers/github.py",
    "agentception/db/queries.py",
    "agentception/db/models.py",
    "agentception/db/base.py",
    "agentception/routes/api/runs.py",
    "agentception/routes/api/system.py",
    "agentception/routes/api/health.py",
    "agentception/routes/api/plan.py",
    "agentception/routes/api/control.py",
    "agentception/app.py",
    "agentception/mcp/build_commands.py",
    "agentception/mcp/server.py",
    "agentception/services/agent_loop.py",
    "agentception/services/working_memory.py",
    "agentception/services/run_factory.py",
    "agentception/services/spawn_child.py",
    "agentception/services/worktree_reaper.py",
    "agentception/poller.py",
    "agentception/services/cognitive_arch.py",
]

# ─── Evaluation set (corrected expected_file paths) ──────────────────────────


@dataclass(frozen=True)
class EvalQuery:
    """A single evaluation query with its expected answer."""

    description: str
    query: str
    expected_file: str   # substring of the file path
    expected_symbol: str | None  # function/class name in chunk text, or None


EVAL_SET: list[EvalQuery] = [
    EvalQuery(
        description="ensure_worktree",
        query="create a git worktree from a remote branch",
        expected_file="readers/git.py",
        expected_symbol="ensure_worktree",
    ),
    EvalQuery(
        description="teardown_agent_worktree",
        query="delete and clean up a git worktree after an agent finishes",
        expected_file="services/teardown.py",
        expected_symbol="teardown_agent_worktree",
    ),
    EvalQuery(
        description="search_codebase",
        query="hybrid dense and sparse vector search combining results with RRF",
        expected_file="services/code_indexer.py",
        expected_symbol="search_codebase",
    ),
    EvalQuery(
        description="_compute_bm25_vectors",
        query="compute BM25 sparse embedding vectors for a batch of texts",
        expected_file="services/code_indexer.py",
        expected_symbol="_compute_bm25_vectors",
    ),
    EvalQuery(
        description="assemble_executor_context",
        query="assemble context briefing for a developer agent before dispatch",
        expected_file="services/context_assembler.py",
        expected_symbol="assemble_executor_context",
    ),
    EvalQuery(
        description="_ast_enclosing_scope",
        query="find the innermost AST function or class enclosing a given line number",
        expected_file="services/context_assembler.py",
        expected_symbol="_ast_enclosing_scope",
    ),
    EvalQuery(
        description="SSE streaming in dispatch",
        query="stream server-sent events to the browser during an agent run",
        expected_file="routes/api/dispatch.py",
        expected_symbol=None,
    ),
    EvalQuery(
        description="persist agent event",
        query="persist an agent event record to the database",
        expected_file="db/persist.py",
        expected_symbol=None,
    ),
    EvalQuery(
        description="Qdrant config in Settings",
        query="Pydantic Settings class for Qdrant URL and collection configuration",
        expected_file="agentception/config.py",
        expected_symbol="AgentCeptionSettings",
    ),
    EvalQuery(
        description="issue_creator",
        query="create a GitHub issue with labels and milestone via API",
        expected_file="readers/issue_creator.py",
        expected_symbol=None,
    ),
    EvalQuery(
        description="Anthropic LLM call",
        query="call Anthropic Claude API with streaming enabled",
        expected_file="services/llm.py",
        expected_symbol=None,
    ),
    EvalQuery(
        description="async DB session",
        query="SQLAlchemy async session factory for database connections",
        expected_file="db/engine.py",
        expected_symbol=None,
    ),
]

TOP_K = 5

# ─── Ablation configurations ─────────────────────────────────────────────────


@dataclass(frozen=True)
class AblationConfig:
    """One ablation configuration to test."""

    name: str            # used as Qdrant collection suffix
    embed_model: str
    embed_dim: int
    chunking: str        # "char" or "ast"
    use_bm25_idf: bool
    use_rerank: bool
    label: str           # human-readable label for the table


CONFIGS: list[AblationConfig] = [
    AblationConfig(
        name="bge_char_noidf",
        embed_model="BAAI/bge-small-en-v1.5",
        embed_dim=384,
        chunking="char",
        use_bm25_idf=False,
        use_rerank=False,
        label="BGE-small + char + no-IDF",
    ),
    AblationConfig(
        name="bge_char_idf",
        embed_model="BAAI/bge-small-en-v1.5",
        embed_dim=384,
        chunking="char",
        use_bm25_idf=True,
        use_rerank=False,
        label="BGE-small + char + IDF       (+IDF)",
    ),
    AblationConfig(
        name="bge_ast_idf",
        embed_model="BAAI/bge-small-en-v1.5",
        embed_dim=384,
        chunking="ast",
        use_bm25_idf=True,
        use_rerank=False,
        label="BGE-small + AST  + IDF       (+AST)",
    ),
    AblationConfig(
        name="jina_ast_idf",
        embed_model="jinaai/jina-embeddings-v2-base-code",
        embed_dim=768,
        chunking="ast",
        use_bm25_idf=True,
        use_rerank=False,
        label="Jina-v2  + AST  + IDF       (+Jina)",
    ),
    AblationConfig(
        name="jina_ast_rerank",
        embed_model="jinaai/jina-embeddings-v2-base-code",
        embed_dim=768,
        chunking="ast",
        use_bm25_idf=True,
        use_rerank=True,
        label="Jina-v2  + AST  + IDF+rerank (+rerank)",
    ),
]

# ─── Chunking ────────────────────────────────────────────────────────────────

_CHAR_SIZE = 1_500
_CHAR_OVERLAP = 150


@dataclass(frozen=True)
class Chunk:
    """A single indexed unit."""

    file: str    # relative path from repo root
    symbol: str  # function/class name, or "" for char chunks
    text: str    # enriched text (file + symbol prefix + body)


def _chunk_char(path: Path, rel: str) -> list[Chunk]:
    """Fixed-size character windows with overlap."""
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    chunks: list[Chunk] = []
    i = 0
    while i < len(body):
        window = body[i : i + _CHAR_SIZE]
        chunks.append(Chunk(file=rel, symbol="", text=f"# file: {rel}\n{window}"))
        i += _CHAR_SIZE - _CHAR_OVERLAP
    return chunks


def _chunk_ast(path: Path, rel: str) -> list[Chunk]:
    """AST-level chunks for Python: top-level and method-level nodes.

    Falls back to char chunking for non-Python files or parse errors.
    """
    if path.suffix != ".py":
        return _chunk_char(path, rel)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return _chunk_char(path, rel)

    lines = source.splitlines(keepends=True)
    chunks: list[Chunk] = []

    def _extract(node: ast.AST, parent_name: str) -> None:
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            return
        name = f"{parent_name}.{node.name}" if parent_name else node.name
        end = node.end_lineno or node.lineno
        body = "".join(lines[node.lineno - 1 : end])
        chunks.append(
            Chunk(
                file=rel,
                symbol=name,
                text=f"# file: {rel}\n# symbol: {name}\n{body}",
            )
        )
        if isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                _extract(child, name)

    for top in ast.iter_child_nodes(tree):
        _extract(top, "")

    return chunks if chunks else _chunk_char(path, rel)


# ─── Embedding helpers ────────────────────────────────────────────────────────


def _to_dense_vec(arr: object) -> list[float]:
    """Convert any ndarray to a plain Python list[float]."""
    import numpy as np  # local to keep top-level imports lean

    if isinstance(arr, np.ndarray):
        return [float(x) for x in arr]
    raise TypeError(f"Expected ndarray, got {type(arr)}")


def _to_sparse_vec(se: SparseEmbedding) -> SparseVector:
    """Convert a SparseEmbedding to a Qdrant SparseVector."""
    return SparseVector(
        indices=[int(x) for x in se.indices],
        values=[float(x) for x in se.values],
    )


# ─── Batching ────────────────────────────────────────────────────────────────

_MAX_PADDED_CHARS = 24_000  # n_chunks × max_len budget (see code_indexer.py)


def _make_batches(chunks: list[Chunk]) -> list[list[Chunk]]:
    """Group chunks so padded ONNX cost stays within budget."""
    batches: list[list[Chunk]] = []
    cur: list[Chunk] = []
    for chunk in chunks:
        tlen = len(chunk.text)
        if cur:
            projected_max = max(max(len(c.text) for c in cur), tlen)
            projected_cost = (len(cur) + 1) * projected_max
            if projected_cost > _MAX_PADDED_CHARS:
                batches.append(cur)
                cur = []
        cur.append(chunk)
    if cur:
        batches.append(cur)
    return batches


# ─── Qdrant helpers ───────────────────────────────────────────────────────────


def _collection_name(config: AblationConfig) -> str:
    return f"{COLLECTION_PREFIX}-{config.name}"


def _create_collection(
    client: QdrantClient, name: str, dim: int, use_idf: bool
) -> None:
    """Drop and re-create an ablation collection."""
    try:
        client.delete_collection(name)
    except Exception:  # noqa: BLE001
        pass
    sparse_params = (
        SparseVectorParams(modifier=Modifier.IDF)
        if use_idf
        else SparseVectorParams()
    )
    client.create_collection(
        collection_name=name,
        vectors_config={"dense": VectorParams(size=dim, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": sparse_params},
    )


# ─── Indexing ────────────────────────────────────────────────────────────────


def _index_corpus(
    client: QdrantClient,
    collection: str,
    corpus: list[Path],
    rel_paths: list[str],
    dense_model: TextEmbedding,
    sparse_model: SparseTextEmbedding,
    chunking: str,
) -> int:
    """Chunk, embed, and upsert all corpus files.  Returns total chunk count."""
    chunk_fn = _chunk_ast if chunking == "ast" else _chunk_char
    all_chunks: list[Chunk] = []
    for path, rel in zip(corpus, rel_paths):
        all_chunks.extend(chunk_fn(path, rel))

    batches = _make_batches(all_chunks)
    point_id = 0
    for batch in batches:
        texts = [c.text for c in batch]
        dense_vecs = [_to_dense_vec(v) for v in dense_model.embed(texts)]
        sparse_vecs = [_to_sparse_vec(se) for se in sparse_model.embed(texts)]
        points = [
            PointStruct(
                id=point_id + i,
                vector={"dense": dv, "sparse": sv},
                payload={"file": c.file, "symbol": c.symbol, "chunk": c.text},
            )
            for i, (c, dv, sv) in enumerate(zip(batch, dense_vecs, sparse_vecs))
        ]
        client.upsert(collection_name=collection, points=points)
        point_id += len(batch)

    return point_id


# ─── Search ───────────────────────────────────────────────────────────────────


@dataclass
class SearchHit:
    file: str
    chunk: str
    symbol: str


def _search(
    client: QdrantClient,
    collection: str,
    query: str,
    dense_model: TextEmbedding,
    sparse_model: SparseTextEmbedding,
    reranker: TextCrossEncoder | None,
    n: int,
) -> list[SearchHit]:
    """Hybrid RRF search with optional cross-encoder reranking."""
    dense_q = _to_dense_vec(next(iter(dense_model.embed([query]))))
    sparse_q = _to_sparse_vec(next(iter(sparse_model.embed([query]))))

    prefetch_limit = n * 4
    response = client.query_points(
        collection_name=collection,
        prefetch=[
            Prefetch(query=dense_q, using="dense", limit=prefetch_limit),
            Prefetch(query=sparse_q, using="sparse", limit=prefetch_limit),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=n * 2,
        with_payload=True,
    )

    hits: list[SearchHit] = []
    for point in response.points:
        payload = point.payload or {}
        file_val = payload.get("file")
        chunk_val = payload.get("chunk")
        sym_val = payload.get("symbol")
        if not isinstance(file_val, str) or not isinstance(chunk_val, str):
            continue
        hits.append(
            SearchHit(
                file=file_val,
                chunk=chunk_val,
                symbol=sym_val if isinstance(sym_val, str) else "",
            )
        )

    if reranker is not None and len(hits) > 1:
        docs = [h.chunk for h in hits]
        scores = list(reranker.rerank(query=query, documents=docs))
        ranked_pairs = sorted(
            zip(scores, hits), key=lambda pair: pair[0], reverse=True
        )
        hits = [h for _, h in ranked_pairs]

    return hits[:n]


# ─── Per-query result ─────────────────────────────────────────────────────────


@dataclass
class QueryResult:
    description: str
    rank: int | None
    elapsed_ms: float


def _eval_query(
    client: QdrantClient,
    collection: str,
    q: EvalQuery,
    dense_model: TextEmbedding,
    sparse_model: SparseTextEmbedding,
    reranker: TextCrossEncoder | None,
) -> QueryResult:
    t0 = time.perf_counter()
    hits = _search(client, collection, q.query, dense_model, sparse_model, reranker, TOP_K)
    elapsed_ms = (time.perf_counter() - t0) * 1_000

    rank: int | None = None
    for i, hit in enumerate(hits, start=1):
        if q.expected_file in hit.file:
            if q.expected_symbol is None or q.expected_symbol in hit.chunk:
                rank = i
                break

    return QueryResult(description=q.description, rank=rank, elapsed_ms=elapsed_ms)


# ─── Config runner ────────────────────────────────────────────────────────────


@dataclass
class ConfigResult:
    config: AblationConfig
    query_results: list[QueryResult] = field(default_factory=list)
    index_elapsed_s: float = 0.0
    chunk_count: int = 0

    @property
    def mrr(self) -> float:
        rrs = [1.0 / r.rank if r.rank else 0.0 for r in self.query_results]
        return sum(rrs) / len(rrs) if rrs else 0.0

    def hit_at(self, k: int) -> int:
        return sum(
            1 for r in self.query_results if r.rank is not None and r.rank <= k
        )


def _run_config(
    client: QdrantClient,
    config: AblationConfig,
    corpus: list[Path],
    rel_paths: list[str],
    dense_model: TextEmbedding,
    sparse_model: SparseTextEmbedding,
    reranker: TextCrossEncoder | None,
) -> ConfigResult:
    collection = _collection_name(config)
    print(f"  [{config.name}] creating collection …", flush=True)
    _create_collection(client, collection, config.embed_dim, config.use_bm25_idf)

    print(f"  [{config.name}] indexing {len(corpus)} files …", flush=True)
    t0 = time.perf_counter()
    n_chunks = _index_corpus(
        client, collection, corpus, rel_paths, dense_model, sparse_model, config.chunking
    )
    index_elapsed = time.perf_counter() - t0
    print(
        f"  [{config.name}] indexed {n_chunks} chunks in {index_elapsed:.1f}s",
        flush=True,
    )

    result = ConfigResult(
        config=config,
        index_elapsed_s=index_elapsed,
        chunk_count=n_chunks,
    )
    for q in EVAL_SET:
        result.query_results.append(
            _eval_query(client, collection, q, dense_model, sparse_model, reranker)
        )

    # Clean up test collection to keep Qdrant tidy.
    try:
        client.delete_collection(collection)
    except Exception:  # noqa: BLE001
        pass

    return result


# ─── Main ─────────────────────────────────────────────────────────────────────


def _print_table(results: list[ConfigResult]) -> None:
    n = len(EVAL_SET)
    col_w = 36
    header_parts = [f"{'Query':<28}"] + [f"{r.config.name:>{col_w}}" for r in results]
    print("\n" + "=" * (28 + col_w * len(results) + len(results)))
    print("  Ablation Benchmark — per-query ranks")
    print("=" * (28 + col_w * len(results) + len(results)))
    print("  ".join(header_parts))
    print("-" * (28 + col_w * len(results) + len(results)))

    for i, q in enumerate(EVAL_SET):
        row_parts = [f"{q.description:<28}"]
        for r in results:
            qr = r.query_results[i]
            cell = f"#{qr.rank}" if qr.rank else "—"
            row_parts.append(f"{cell:>{col_w}}")
        print("  ".join(row_parts))

    print("-" * (28 + col_w * len(results) + len(results)))

    # Summary row
    print()
    print(f"{'Config':<36}  {'MRR@5':>6}  {'H@1':>4}  {'H@3':>4}  {'H@5':>4}  {'Chunks':>6}  {'idx(s)':>6}  Label")
    print("-" * 100)
    for r in results:
        print(
            f"{r.config.name:<36}  {r.mrr:>6.3f}  {r.hit_at(1):>4}/{n}"
            f"  {r.hit_at(3):>4}/{n}  {r.hit_at(5):>4}/{n}"
            f"  {r.chunk_count:>6}  {r.index_elapsed_s:>5.0f}s  {r.config.label}"
        )
    print("=" * 100 + "\n")


def main() -> None:
    """Run all ablation configurations and print a comparison table."""
    client = QdrantClient(url=QDRANT_URL)

    # Resolve corpus to existing files only.
    corpus: list[Path] = []
    rel_paths: list[str] = []
    for rel in CORPUS_PATHS:
        p = REPO_ROOT / rel
        if p.exists():
            corpus.append(p)
            rel_paths.append(rel)
        else:
            print(f"  [warn] corpus file not found, skipping: {rel}", flush=True)

    print(f"\nAblation corpus: {len(corpus)} files, {len(EVAL_SET)} queries\n", flush=True)

    # Load BM25 once — shared across all configs.  The IDF weighting is applied
    # server-side by Qdrant (SparseVectorParams.modifier), not by the model.
    print("Loading BM25 model …", flush=True)
    sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")

    all_results: list[ConfigResult] = []

    # ── BGE-small configs (1–3) ───────────────────────────────────────────────
    bge_configs = [c for c in CONFIGS if "bge" in c.embed_model.lower()]
    if bge_configs:
        print(f"\nLoading dense model: {bge_configs[0].embed_model} …", flush=True)
        bge_model = TextEmbedding(model_name=bge_configs[0].embed_model)
        for cfg in bge_configs:
            all_results.append(
                _run_config(client, cfg, corpus, rel_paths, bge_model, sparse_model, None)
            )
        del bge_model
        gc.collect()

    # ── Jina configs (4–5) ────────────────────────────────────────────────────
    jina_configs = [c for c in CONFIGS if "jina" in c.embed_model.lower()]
    if jina_configs:
        print(f"\nLoading dense model: {jina_configs[0].embed_model} …", flush=True)
        jina_model = TextEmbedding(model_name=jina_configs[0].embed_model)

        reranker: TextCrossEncoder | None = None
        if any(c.use_rerank for c in jina_configs):
            print("Loading reranker: BAAI/bge-reranker-base …", flush=True)
            reranker = TextCrossEncoder(model_name="BAAI/bge-reranker-base")

        for cfg in jina_configs:
            all_results.append(
                _run_config(
                    client,
                    cfg,
                    corpus,
                    rel_paths,
                    jina_model,
                    sparse_model,
                    reranker if cfg.use_rerank else None,
                )
            )
        del jina_model
        if reranker is not None:
            del reranker
        gc.collect()

    _print_table(all_results)


if __name__ == "__main__":
    main()
