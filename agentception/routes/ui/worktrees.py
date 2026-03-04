"""UI routes: git worktrees browser and detail partial."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.config import settings as _settings
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/worktrees", response_class=HTMLResponse)
async def worktrees_page(request: Request) -> HTMLResponse:
    """Agent Sandboxes — live view of git worktrees as isolated code environments."""
    from agentception.readers.git import list_git_worktrees

    worktrees: list[dict[str, object]] = []

    try:
        worktrees = await list_git_worktrees()
    except Exception as exc:
        logger.warning("⚠️  Worktrees page git read failed: %s", exc)

    main_wt = next((wt for wt in worktrees if wt.get("is_main")), None)
    agent_worktrees = [wt for wt in worktrees if not wt.get("is_main")]

    return _TEMPLATES.TemplateResponse(
        request,
        "worktrees.html",
        {
            "main_wt": main_wt,
            "agent_worktrees": agent_worktrees,
            "gh_repo": _settings.gh_repo,
        },
    )


@router.get("/worktrees/{slug}/detail", response_class=HTMLResponse)
async def worktree_detail_partial(request: Request, slug: str) -> HTMLResponse:
    """HTMX partial: on-demand detail panel for a single worktree.

    Returns commits ahead of origin/dev, a diff stat, and the raw
    .agent-task file content — all rendered server-side.
    """
    from agentception.readers.git import get_worktree_detail

    detail = await get_worktree_detail(slug)
    return _TEMPLATES.TemplateResponse(
        request,
        "_worktree_detail.html",
        {"slug": slug, **detail},
    )
