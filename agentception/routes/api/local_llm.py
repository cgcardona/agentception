from __future__ import annotations

"""Minimal local-LLM probe and hello-agent endpoints.

- GET /hello — one-shot completion (no agent loop).
- POST /hello-agent — dispatch a minimal agent run that says "hello world" via the local LLM.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentception.config import LLMProviderChoice, settings
from agentception.db.persist import acknowledge_agent_run, persist_agent_run_dispatch
from agentception.services.agent_loop import run_agent_loop
from agentception.services.llm import call_local_completion

router = APIRouter(prefix="/local-llm", tags=["local-llm"])


@router.get("/hello")
async def local_llm_hello() -> dict[str, str | bool]:
    """One-shot completion: ask the local LLM to reply 'hello world'.

    Requires effective provider ``local`` (e.g. ``USE_LOCAL_LLM=true`` or
    ``LLM_PROVIDER=local``). Returns the model's reply so you can confirm the
    pipeline works without running a full developer agent.
    """
    if settings.effective_llm_provider != LLMProviderChoice.local:
        raise HTTPException(
            503,
            detail="Local LLM is disabled. Set LLM_PROVIDER=local or USE_LOCAL_LLM=true and restart.",
        )
    try:
        reply = await call_local_completion(
            system="You are a helpful assistant. Reply briefly.",
            user_message="Reply with exactly: hello world",
            max_tokens=128,
        )
    except Exception as exc:
        raise HTTPException(502, detail=f"Local LLM request failed: {exc!s}") from exc
    return {"ok": True, "reply": reply}


class HelloAgentResponse(BaseModel):
    run_id: str
    status: str = "implementing"


@router.post("/hello-agent", response_model=HelloAgentResponse)
async def local_llm_hello_agent() -> HelloAgentResponse:
    """Dispatch a minimal agent run that uses the local LLM to reply "hello world".

    Creates a run with run_id ``local-hello-<uuid>``, a minimal task (no tools,
    one user message), and fires the agent loop. The agent responds with text
    only and the run completes. Requires effective provider ``local``.
    """
    if settings.effective_llm_provider != LLMProviderChoice.local:
        raise HTTPException(
            503,
            detail="Local LLM is disabled. Set LLM_PROVIDER=local or USE_LOCAL_LLM=true and restart.",
        )
    run_id = f"local-hello-{uuid.uuid4().hex[:8]}"
    worktree_path = str(Path(settings.worktrees_dir) / run_id)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / run_id)
    Path(worktree_path).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = f"local-hello-{stamp}-{uuid.uuid4().hex[:4]}"
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=0,
        role="developer",
        branch="local-hello",
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=None,
        gh_repo=settings.gh_repo,
    )
    await acknowledge_agent_run(run_id)
    asyncio.create_task(run_agent_loop(run_id), name=f"agent-loop-{run_id}")
    return HelloAgentResponse(run_id=run_id, status="implementing")
