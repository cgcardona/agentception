"""Ad-hoc agent run endpoint.

Provides a single endpoint that creates a fully self-contained agent run
without requiring a GitHub issue or a wave.  The caller supplies a role, an optional cognitive figure, and a plain-
language task description.  The endpoint:

1. Delegates to :func:`~agentception.services.run_factory.create_and_launch_run`.
2. Returns ``202 Accepted`` immediately.

This is the entry point for the Cursor-replacement loop: no Cursor session,
no file paste, no manual worktree setup — just a POST and an agent running.

Endpoint
--------
POST /api/runs/adhoc
    Body: ``AdhocRunRequest`` JSON.
    Returns: ``{ "ok": true, "run_id": "...", "worktree_path": "...", "cognitive_arch": "..." }``
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from agentception.services.run_factory import RunCreationError, create_and_launch_run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["agent-run"])

_TASK_DESCRIPTION_MAX_LEN = 4_000


class AdhocRunRequest(BaseModel):
    """Request body for POST /api/runs/adhoc."""

    role: str
    """Role slug — must exist in ``.agentception/roles/``.

    Examples: ``"developer"``, ``"engineering-coordinator"``.
    """

    task_description: str
    """Plain-language description of what the agent should do.

    Injected verbatim as the agent's first briefing message.  Be specific:
    include target files, expected output, and any constraints.
    """

    figure: str | None = None
    """Cognitive figure slug override (e.g. ``"guido_van_rossum"``).

    When omitted, the default figure for the role is used.
    """

    base_branch: str = "origin/dev"
    """Git ref to branch the worktree from.  Defaults to ``origin/dev``."""

    @field_validator("task_description")
    @classmethod
    def task_description_max_length(cls, v: str) -> str:
        if len(v) > _TASK_DESCRIPTION_MAX_LEN:
            raise ValueError(
                f"task_description exceeds {_TASK_DESCRIPTION_MAX_LEN} characters"
            )
        return v

    @field_validator("role")
    @classmethod
    def role_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("role must be a non-empty string")
        return v.strip()

    @field_validator("figure")
    @classmethod
    def figure_must_be_non_empty_when_provided(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("figure must be non-empty when provided")
        return v.strip() if v else None


class AdhocRunResponse(BaseModel):
    """Successful response from POST /api/runs/adhoc."""

    ok: bool
    run_id: str
    worktree_path: str
    cognitive_arch: str


@router.post("/adhoc", status_code=202, response_model=AdhocRunResponse)
async def create_adhoc_run(req: AdhocRunRequest) -> JSONResponse:
    """Create a self-contained agent run from an inline task description.

    The run bypasses the GitHub-issue dispatch pipeline entirely.  The agent
    loop receives the task description directly in its first message.

    Returns 202 immediately.  Monitor progress via the build dashboard or
    ``GET /api/runs/{run_id}``.
    """
    try:
        result = await create_and_launch_run(
            role=req.role,
            task_description=req.task_description,
            figure=req.figure,
            base_branch=req.base_branch,
        )
    except RunCreationError as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    logger.info(
        "✅ adhoc run dispatched — run_id=%s role=%s arch=%s",
        result["run_id"],
        req.role,
        result["cognitive_arch"],
    )
    return JSONResponse(status_code=202, content={"ok": True, **result})
