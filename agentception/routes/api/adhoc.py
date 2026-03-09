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
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from agentception.config import settings
from agentception.services.run_factory import RunCreationError, create_and_launch_run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["agent-run"])

_TASK_DESCRIPTION_MAX_LEN = 16_000
_CONTEXT_FILES_MAX = 20
_CONTEXT_FILE_MAX_BYTES = 100_000  # 100 KB per file — skip larger files


def _build_context_prefix(context_files: list[str]) -> str:
    """Read *context_files* from the repo and return a formatted prefix string.

    Each file is rendered as a fenced code block with its repo-relative path as
    the header.  Files that cannot be read (missing, too large, binary) are
    skipped with a warning.  The prefix is empty when *context_files* is empty.
    """
    if not context_files:
        return ""

    repo_dir = settings.repo_dir
    blocks: list[str] = [
        "# Pre-loaded context\n"
        "The following files have been injected into your briefing so you can\n"
        "start implementing immediately — no discovery turns needed.\n"
    ]

    for rel in context_files:
        abs_path: Path = repo_dir / rel
        try:
            size = abs_path.stat().st_size
            if size > _CONTEXT_FILE_MAX_BYTES:
                logger.warning(
                    "⚠️ adhoc context_files: skipping %s — too large (%d bytes)", rel, size
                )
                continue
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("⚠️ adhoc context_files: cannot read %s — %s", rel, exc)
            continue

        ext = abs_path.suffix.lstrip(".")
        blocks.append(f"### `{rel}`\n```{ext}\n{content}\n```")

    if len(blocks) == 1:
        # Only the header was added — no files loaded.
        return ""

    return "\n\n".join(blocks) + "\n\n---\n\n"


class AdhocRunRequest(BaseModel):
    """Request body for POST /api/runs/adhoc."""

    role: str
    """Role slug — must exist in ``.agentception/roles/``.

    Examples: ``"developer"``, ``"engineering-coordinator"``.
    """

    task_description: str
    """Plain-language description of what the agent should do.

    Injected as the agent's first briefing message, preceded by any
    ``context_files`` content.  Be specific: include target files, expected
    output, and any constraints.
    """

    figure: str | None = None
    """Cognitive figure slug override (e.g. ``"guido_van_rossum"``).

    When omitted, the default figure for the role is used.
    """

    base_branch: str = "origin/dev"
    """Git ref to branch the worktree from.  Defaults to ``origin/dev``."""

    context_files: list[str] | None = None
    """Repo-relative paths whose full contents are injected before the task
    description.

    The agent receives these files verbatim in its first message so it can
    start writing code immediately — zero discovery turns required.

    Example::

        "context_files": [
            "agentception/workflow/status.py",
            "agentception/db/persist.py"
        ]

    Files that are missing, binary, or larger than 100 KB are silently skipped.
    Maximum ``{_CONTEXT_FILES_MAX}`` entries.
    """

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

    @field_validator("context_files")
    @classmethod
    def context_files_limit(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) > _CONTEXT_FILES_MAX:
            raise ValueError(f"context_files exceeds maximum of {_CONTEXT_FILES_MAX} entries")
        return v


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
    loop receives the task description directly in its first message, optionally
    preceded by the full contents of any ``context_files`` so the agent can
    begin implementing without any file-discovery iterations.

    Returns 202 immediately.  Monitor progress via the build dashboard or
    ``GET /api/runs/{run_id}``.
    """
    context_prefix = _build_context_prefix(req.context_files or [])
    full_task = f"{context_prefix}{req.task_description}"

    try:
        result = await create_and_launch_run(
            role=req.role,
            task_description=full_task,
            figure=req.figure,
            base_branch=req.base_branch,
        )
    except RunCreationError as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    logger.info(
        "✅ adhoc run dispatched — run_id=%s role=%s arch=%s context_files=%d",
        result["run_id"],
        req.role,
        result["cognitive_arch"],
        len(req.context_files or []),
    )
    return JSONResponse(status_code=202, content={"ok": True, **result})
