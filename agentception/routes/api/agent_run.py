"""Agent execution trigger route.

Provides a single endpoint to launch the Cursor-free agent loop for an
existing run.  The loop runs as a FastAPI ``BackgroundTask`` and reports
progress through the MCP log tools (visible in the build dashboard).

Endpoint
--------
POST /api/runs/{run_id}/execute
    Transitions the run to ``implementing`` and fires the agent loop in the
    background.  Returns ``202 Accepted`` immediately.  The caller should poll
    the build dashboard or the run inspector to observe progress.

Error codes
-----------
404  Run not found.
409  Run is not in a dispatchable state (must be ``pending_launch`` or
     ``implementing``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select

from agentception.db.engine import get_session
from agentception.db.models import ACAgentRun
from agentception.mcp.build_commands import build_claim_run
from agentception.services.agent_loop import run_agent_loop

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["agent-run"])

# Statuses from which the agent loop may be launched.
_DISPATCHABLE_STATUSES: frozenset[str] = frozenset({"pending_launch", "implementing"})


@router.post("/{run_id}/execute", status_code=202)
async def execute_agent_run(run_id: str, background_tasks: BackgroundTasks) -> JSONResponse:
    """Launch the Cursor-free agent loop for *run_id*.

    If the run is ``pending_launch`` it is atomically claimed (transitioned to
    ``implementing``) before the background task is scheduled.  Runs already
    ``implementing`` are accepted as-is so the caller can retry a stale loop.

    Returns 202 immediately; progress is visible in the build dashboard.
    """
    async with get_session() as session:
        run: ACAgentRun | None = await session.scalar(
            select(ACAgentRun).where(ACAgentRun.id == run_id)
        )

    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    if run.status not in _DISPATCHABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run '{run_id}' is in status '{run.status}' — "
                f"only {sorted(_DISPATCHABLE_STATUSES)} may be executed."
            ),
        )

    if run.status == "pending_launch":
        claim_result = await build_claim_run(run_id)
        if not claim_result.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=f"Failed to claim run '{run_id}': {claim_result.get('error')}",
            )
        logger.info("✅ execute_agent_run — claimed run_id=%s", run_id)

    background_tasks.add_task(run_agent_loop, run_id)
    logger.info("✅ execute_agent_run — loop scheduled for run_id=%s", run_id)

    return JSONResponse(
        status_code=202,
        content={"ok": True, "run_id": run_id, "message": "Agent loop started."},
    )
