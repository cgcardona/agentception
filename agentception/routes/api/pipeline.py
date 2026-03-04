"""API routes: pipeline state and agent queries."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from agentception.models import AgentNode, PipelineState
from agentception.poller import get_state
from agentception.readers.transcripts import read_transcript_messages
from agentception.routes.ui._shared import _find_agent

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/pipeline")
async def pipeline_api() -> PipelineState:
    """Return the current PipelineState snapshot as JSON.

    Returns an empty state (zero counts, empty agents list) before the first
    polling tick completes — callers should treat ``agents == []`` as loading,
    not as "no agents exist".
    """
    return get_state() or PipelineState.empty()


@router.get("/agents")
async def agents_api() -> list[AgentNode]:
    """Return the flat list of root-level AgentNodes from the current pipeline state.

    Children are embedded inside each AgentNode's ``children`` field.
    Returns an empty list before the first polling tick completes.
    """
    state = get_state() or PipelineState.empty()
    return state.agents


@router.get("/agents/{agent_id}")
async def agent_api(agent_id: str) -> AgentNode:
    """Return a single AgentNode by ID from the current pipeline state.

    Searches root agents and their children (one level deep). Raises HTTP 404
    when the agent ID is not found in the current state.
    """
    state = get_state()
    node = _find_agent(state, agent_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return node


@router.get("/agents/{agent_id}/transcript")
async def transcript_api(agent_id: str) -> list[dict[str, str]]:
    """Return the parsed transcript messages for a given agent.

    Each element is ``{"role": "user"|"assistant", "text": "..."}``.
    Returns an empty list when the agent has no transcript file.
    Raises HTTP 404 when the agent ID is not found in the current state.
    """
    state = get_state()
    node = _find_agent(state, agent_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    if not node.transcript_path:
        return []
    return await read_transcript_messages(Path(node.transcript_path))
