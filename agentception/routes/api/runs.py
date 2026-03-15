"""Run lifecycle API routes — UI-facing only.

Agents interact with AgentCeption exclusively through the ``user-agentception``
MCP server.  This HTTP surface is reserved for browser / UI interactions only.

UI surfaces retained:
  POST /api/runs/{run_id}/message  — operator sends a message to an agent
  POST /api/runs/{run_id}/cancel   — UI cancels a pending_launch before dispatch
  POST /api/runs/{run_id}/stop     — UI marks a run stopped from the inspector

Agent-facing routes have been removed.  Use the MCP equivalents:
  ac://runs/pending resource, build_claim_run, build_spawn_adhoc_child,
  log_run_step, log_run_error, build_complete_run.
"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from agentception.db.engine import get_session
from agentception.db.models import ACAgentMessage, ACAgentRun
from agentception.db.queries import get_file_edit_events
from agentception.models import FileEditEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/memory — file-edit history for the inspector panel
# ---------------------------------------------------------------------------


class MemoryResponse(BaseModel):
    """Response for GET /api/runs/{run_id}/memory.

    ``files_written`` is the ordered list of file-edit events recorded by the
    agent during its run, each carrying a unified diff for inspector rendering.
    """

    files_written: list[FileEditEvent]


@router.get("/{run_id}/memory", response_model=MemoryResponse)
async def get_run_memory(run_id: str) -> MemoryResponse:
    """Return file-edit history for a run.

    Queries ``ACAgentEvent`` rows with ``event_type='file_edit'`` for the
    given run.  Returns HTTP 404 when the run does not exist in the ``runs``
    table.
    """
    try:
        async with get_session() as session:
            run_exists = await session.scalar(
                select(func.count()).select_from(ACAgentRun).where(ACAgentRun.id == run_id)
            )
        if not run_exists:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ get_run_memory run-check failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to check run existence") from exc

    events = await get_file_edit_events(run_id)
    return MemoryResponse(files_written=events)


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/message — operator sends a message to an agent
# ---------------------------------------------------------------------------


class _AgentMessageBody(BaseModel):
    content: str


@router.post("/{run_id}/message", response_model=None)
async def post_agent_message(run_id: str, body: _AgentMessageBody) -> Response:
    """Append a user message to an agent run's transcript.

    The message is stored in ``agent_messages`` with ``role='user'`` and
    immediately picked up by the inspector's SSE stream on the next 2-second
    poll.

    Returns 404 if the run_id is not found.
    """
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must be non-empty")

    try:
        async with get_session() as session:
            run_exists = await session.scalar(
                select(func.count()).select_from(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            if not run_exists:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

            seq_result = await session.scalar(
                select(func.coalesce(func.max(ACAgentMessage.sequence_index), -1)).where(
                    ACAgentMessage.agent_run_id == run_id
                )
            )
            next_seq = int(seq_result) + 1 if seq_result is not None else 0

            session.add(
                ACAgentMessage(
                    agent_run_id=run_id,
                    role="user",
                    content=content,
                    sequence_index=next_seq,
                    recorded_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ post_agent_message failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save message") from exc

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/cancel — abort a pending_launch before dispatch
# ---------------------------------------------------------------------------


@router.post("/{run_id}/cancel", response_model=None)
async def cancel_pending_run(run_id: str) -> Response:
    """Cancel a queued run before it is claimed by the Dispatcher.

    Only works on runs still in ``pending_launch`` state — once the Dispatcher
    claims a run (transitions it to ``implementing``) it cannot be cancelled
    through this endpoint.

    Returns 204 on success, 409 if already claimed/not pending.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
            if run.status != "pending_launch":
                raise HTTPException(
                    status_code=409,
                    detail=f"Run '{run_id}' is '{run.status}' — only pending_launch runs can be cancelled",
                )
            run.status = "cancelled"
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ cancel_pending_run failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to cancel run") from exc

    logger.info("✅ cancel_pending_run: %s cancelled", run_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/runs/{run_id}/stop — mark a run as done from the inspector
# ---------------------------------------------------------------------------


@router.post("/{run_id}/stop", response_model=None)
async def stop_agent(run_id: str) -> Response:
    """Mark an agent run as DONE so the board no longer shows it as active.

    Does not remove the worktree (use the control panel kill action for that).
    This is a lightweight "I'm done watching this" action that removes the
    run from the active-agent display so a fresh agent can be dispatched.

    Returns 404 if the run_id is not found.
    """
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ACAgentRun).where(ACAgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

            run.status = "done"
            await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("❌ stop_agent failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to stop agent") from exc

    return Response(status_code=204)
