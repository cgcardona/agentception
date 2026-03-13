"""System management API routes.

Exposes infrastructure operations that operators and agents need but that do
not belong in the domain-specific route modules.

Endpoints
---------
POST /api/system/index-codebase
    Trigger an asynchronous codebase indexing run.  Chunks every source file,
    generates embeddings with the configured FastEmbed model, and upserts them
    into Qdrant.  Returns ``202 Accepted`` immediately; progress is visible in
    the application logs.

GET /api/system/search
    Semantic code search against the Qdrant index.  Useful for testing the
    index before deploying agents and for operators who want direct search
    access from the CLI or UI.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse

from agentception.services.code_indexer import IndexStats, SearchMatch, index_codebase, search_codebase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])


@router.post("/index-codebase", status_code=202)
async def trigger_index_codebase(background_tasks: BackgroundTasks) -> JSONResponse:
    """Start an asynchronous codebase indexing run.

    The indexer walks every source file in the configured repository root,
    chunks and embeds them, and upserts the vectors into Qdrant.  For a
    typical mid-sized codebase this takes 10–60 seconds.

    Returns ``202 Accepted`` immediately.  On completion the log will show
    ``✅ code_indexer — done``.
    """
    background_tasks.add_task(_run_and_log_indexing)
    logger.info("✅ system — codebase indexing scheduled")
    return JSONResponse(
        status_code=202,
        content={"ok": True, "message": "Codebase indexing started in the background."},
    )


async def _run_and_log_indexing() -> None:
    """Background wrapper that logs the final IndexStats."""
    stats: IndexStats = await index_codebase()
    if stats["ok"]:
        logger.info(
            "✅ system — indexing complete: %d files, %d chunks",
            stats["files_indexed"],
            stats["chunks_indexed"],
        )
    else:
        logger.error("❌ system — indexing failed: %s", stats["error"])


@router.get("/search", response_model=None)
async def semantic_search(
    q: str = Query(..., description="Natural-language search query."),
    n: int = Query(5, ge=1, le=20, description="Number of results to return."),
) -> JSONResponse:
    """Search the indexed codebase with a natural-language query.

    Returns the top *n* most semantically relevant code chunks from the Qdrant
    index.  If the codebase has not been indexed yet, returns an empty list.

    Args:
        q: Free-form search query (e.g. ``"where is authentication handled?"``).
        n: Maximum number of results (1–20, default 5).
    """
    matches: list[SearchMatch] = await search_codebase(q, n)
    return JSONResponse(
        content={
            "ok": True,
            "query": q,
            "n_results": len(matches),
            "matches": [
                {
                    "file": m["file"],
                    "score": m["score"],
                    "start_line": m["start_line"],
                    "end_line": m["end_line"],
                    "chunk": m["chunk"],
                }
                for m in matches
            ],
        }
    )
