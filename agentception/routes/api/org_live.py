from __future__ import annotations

"""Org Live API — real-time agent-tree endpoints for the Org Designer overlay.

Endpoints
---------
GET /api/org/batches/{repo}/{initiative}
    Return summary rows for every dispatch batch that has touched issues in
    this initiative.  Ordered newest-first.  Used to populate the Sessions
    tab in the overlay.

GET /api/org/live/{repo}/{initiative}
    SSE stream that emits the live agent-run tree every 5 s.  Clients send
    an optional ``?batch_id=`` query parameter to pin a specific batch;
    without it the stream follows the latest active batch.

SSE event shapes
----------------
  {"t": "tree",  "nodes": [RunTreeNodeRow, ...], "batch_id": "..."}
  {"t": "idle"}   — no active batch found
  {"t": "ping"}   — keepalive (every ~20 s)
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from starlette.requests import Request

from agentception.config import settings
from agentception.types import JsonValue
from agentception.db.queries import (
    BatchSummaryRow,
    RunTreeNodeRow,
    get_batch_summaries_for_initiative,
    get_issues_grouped_by_phase,
    get_latest_active_batch_id,
    get_run_tree_by_batch_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 5.0  # seconds between DB polls
_PING_EVERY = 4         # emit ping every N polls (~20 s)


def _sse(payload: JsonValue) -> str:
    """Serialise a JSON payload as an SSE data frame."""
    return f"data: {json.dumps(payload)}\n\n"


async def _open_issue_numbers(repo: str, initiative: str) -> list[int]:
    """Return all open GitHub issue numbers for *initiative* in *repo*."""
    groups = await get_issues_grouped_by_phase(repo, initiative=initiative)
    return [
        i["number"]
        for g in groups
        for i in g["issues"]
        if i.get("state") != "closed"
    ]


# ── Batch summary endpoint ─────────────────────────────────────────────────────


@router.get("/org/batches/{initiative}", response_model=list[BatchSummaryRow])
async def get_org_batches(initiative: str) -> list[BatchSummaryRow]:
    """Return summary rows for all dispatch batches in the active repo's *initiative*.

    Ordered newest-first by spawn time.  Each row contains the batch_id,
    spawned_at timestamp, total run count, and live run count.  The frontend
    uses this to populate the Sessions tab and to decide whether to show
    Live mode when the overlay opens.
    """
    gh_repo = settings.gh_repo
    issue_numbers = await _open_issue_numbers(gh_repo, initiative)
    return await get_batch_summaries_for_initiative(gh_repo, issue_numbers)


# ── Live SSE endpoint ──────────────────────────────────────────────────────────


@router.get("/org/live/{initiative}")
async def org_live_stream(
    request: Request,
    initiative: str,
    batch_id: str | None = Query(default=None),
) -> StreamingResponse:
    """SSE stream of the live agent-run tree for the active repo's *initiative*.

    Emits a ``tree`` event every 5 s with the flat ``RunTreeNodeRow`` list
    for the current batch.  Clients assemble the tree via ``parent_run_id``.

    When ``?batch_id=`` is omitted the stream follows the latest active batch
    (the one with at least one live run).  When no active batch exists it
    emits an ``idle`` event.
    """
    return StreamingResponse(
        _live_generator(request, initiative, batch_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _live_generator(
    request: Request,
    initiative: str,
    pinned_batch_id: str | None,
) -> AsyncGenerator[str, None]:
    """Async generator for the live tree SSE stream.

    Polls the DB every ``_POLL_INTERVAL_S`` seconds and emits ``tree``,
    ``idle``, or ``ping`` events.  The stream terminates when the client
    disconnects.
    """
    gh_repo = settings.gh_repo
    tick = 0
    last_json: str | None = None

    while True:
        if await request.is_disconnected():
            break

        tick += 1

        # ── Ping (keepalive) ─────────────────────────────────────────────
        if tick % _PING_EVERY == 0:
            yield _sse({"t": "ping"})
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        # ── Resolve batch_id ─────────────────────────────────────────────
        active_batch: str | None = pinned_batch_id
        if active_batch is None:
            issue_numbers = await _open_issue_numbers(gh_repo, initiative)
            active_batch = await get_latest_active_batch_id(
                issue_numbers=issue_numbers or None
            )

        if not active_batch:
            yield _sse({"t": "idle"})
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        # ── Fetch tree ───────────────────────────────────────────────────
        nodes: list[RunTreeNodeRow] = await get_run_tree_by_batch_id(active_batch)
        payload = {"t": "tree", "nodes": nodes, "batch_id": active_batch}
        payload_json = json.dumps(payload)

        # Suppress unchanged frames (every tick would be identical when idle).
        if payload_json != last_json:
            last_json = payload_json
            yield f"data: {payload_json}\n\n"

        await asyncio.sleep(_POLL_INTERVAL_S)
