#!/usr/bin/env python3
"""Qdrant search quality benchmark for AgentCeption.

Measures MRR@5, Hit@1, Hit@3, Hit@5, and per-query latency against a
hand-crafted evaluation set derived from this codebase.  Run this script
before and after each search-quality improvement to quantify progress.

Usage:
    docker compose exec agentception python3 /app/scripts/search_benchmark.py

Output:
    Per-query table with hit/miss indicators and rank, plus aggregate
    MRR@5, Hit@1, Hit@3, Hit@5, and mean latency.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    """A single evaluation query with its expected answer."""

    query: str
    expected_file: str          # substring that should appear in the top result's file path
    expected_symbol: str | None  # function/class name that should appear in the chunk (optional)
    description: str            # human-readable label for the result table


EVAL_SET: list[Query] = [
    Query(
        query="create a git worktree from a remote branch",
        expected_file="readers/worktrees.py",
        expected_symbol="ensure_worktree",
        description="ensure_worktree",
    ),
    Query(
        query="delete and clean up a git worktree after an agent finishes",
        expected_file="readers/worktrees.py",
        expected_symbol="teardown_worktree",
        description="teardown_worktree",
    ),
    Query(
        query="hybrid dense and sparse vector search combining results with RRF",
        expected_file="services/code_indexer.py",
        expected_symbol="search_codebase",
        description="search_codebase",
    ),
    Query(
        query="compute BM25 sparse embedding vectors for a batch of texts",
        expected_file="services/code_indexer.py",
        expected_symbol="_compute_bm25_vectors",
        description="_compute_bm25_vectors",
    ),
    Query(
        query="assemble context briefing for a developer agent before dispatch",
        expected_file="services/context_assembler.py",
        expected_symbol="assemble_executor_context",
        description="assemble_executor_context",
    ),
    Query(
        query="find the innermost AST function or class enclosing a given line number",
        expected_file="services/context_assembler.py",
        expected_symbol="_ast_enclosing_scope",
        description="_ast_enclosing_scope",
    ),
    Query(
        query="stream server-sent events to the browser during an agent run",
        expected_file="routes/api/dispatch.py",
        expected_symbol=None,
        description="SSE streaming in dispatch",
    ),
    Query(
        query="persist an agent event record to the database",
        expected_file="db/persist.py",
        expected_symbol=None,
        description="persist agent event",
    ),
    Query(
        query="Pydantic Settings class for Qdrant URL and collection configuration",
        expected_file="agentception/config.py",
        expected_symbol="Settings",
        description="Qdrant config in Settings",
    ),
    Query(
        query="create a GitHub issue with labels and milestone via API",
        expected_file="readers/issue_creator.py",
        expected_symbol=None,
        description="issue_creator",
    ),
    Query(
        query="call Anthropic Claude API with streaming enabled",
        expected_file="services/llm.py",
        expected_symbol=None,
        description="Anthropic LLM call",
    ),
    Query(
        query="SQLAlchemy async session factory for database connections",
        expected_file="db/base.py",
        expected_symbol=None,
        description="async DB session",
    ),
]

TOP_K = 5  # Evaluate MRR and hits up to this rank.


def _hit_rank(results: list[dict[str, object]], q: Query) -> int | None:
    """Return the 1-based rank of the first result matching *q*, or None."""
    for rank, r in enumerate(results[:TOP_K], start=1):
        file_val = str(r.get("file", ""))
        chunk_val = str(r.get("chunk", ""))
        if q.expected_file in file_val:
            if q.expected_symbol is None or q.expected_symbol in chunk_val:
                return rank
    return None


async def run_benchmark() -> None:
    """Execute all evaluation queries and print a formatted results table."""
    # Late import so the script can be run before the module is on sys.path.
    import sys
    sys.path.insert(0, "/app")

    from agentception.services.code_indexer import search_codebase

    print("\n" + "=" * 72)
    print("  AgentCeption  —  Qdrant Search Quality Benchmark")
    print("=" * 72)
    print(f"{'Query':<42} {'Rank':>4}  {'File hit':>8}  {'Sym hit':>7}  {'ms':>6}")
    print("-" * 72)

    reciprocal_ranks: list[float] = []
    hit_at: dict[int, int] = {1: 0, 3: 0, 5: 0}
    latencies: list[float] = []

    for q in EVAL_SET:
        t0 = time.perf_counter()
        results = await search_codebase(q.query, n_results=TOP_K)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed_ms)

        rank = _hit_rank(results, q)
        rr = 1.0 / rank if rank else 0.0
        reciprocal_ranks.append(rr)

        for k in (1, 3, 5):
            if rank is not None and rank <= k:
                hit_at[k] += 1

        # Per-query output
        rank_str = str(rank) if rank else "—"
        file_hit = "✅" if rank is not None else "❌"

        # Check symbol independently for visibility
        sym_hit = "n/a"
        if q.expected_symbol is not None:
            found_sym = any(
                q.expected_symbol in str(r.get("chunk", ""))
                for r in results[:TOP_K]
            )
            sym_hit = "✅" if found_sym else "❌"

        label = q.description[:41]
        print(f"{label:<42} {rank_str:>4}  {file_hit:>8}  {sym_hit:>7}  {elapsed_ms:>5.0f}")

    n = len(EVAL_SET)
    mrr = sum(reciprocal_ranks) / n
    mean_ms = sum(latencies) / n

    print("-" * 72)
    print(f"\n  MRR@{TOP_K}   : {mrr:.3f}")
    print(f"  Hit@1   : {hit_at[1]}/{n}  ({100 * hit_at[1] / n:.0f}%)")
    print(f"  Hit@3   : {hit_at[3]}/{n}  ({100 * hit_at[3] / n:.0f}%)")
    print(f"  Hit@5   : {hit_at[5]}/{n}  ({100 * hit_at[5] / n:.0f}%)")
    print(f"  Latency : {mean_ms:.0f} ms/query (mean)\n")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    asyncio.run(run_benchmark())
