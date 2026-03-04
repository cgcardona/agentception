"""UI route: settings hub — runtime configuration and GitHub auth status."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request
from starlette.responses import Response

from agentception.config import settings
from agentception.routes.ui._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"])


async def _check_gh_auth() -> bool:
    """Run ``gh auth status``; return True if exit code is 0."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "status",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception:
        return False


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    """Settings hub — displays runtime config and GitHub connection status."""
    return _TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "gh_auth_ok": await _check_gh_auth(),
        },
    )


@router.get("/partials/settings/gh-check", response_class=HTMLResponse)
async def gh_check_partial(request: Request) -> Response:
    """HTMX partial — re-runs gh auth check and returns badge HTML."""
    ok = await _check_gh_auth()
    badge = (
        '<span class="badge badge--green">✅ Authenticated</span>'
        if ok else
        '<span class="badge badge--red">❌ Not authenticated</span>'
    )
    return HTMLResponse(content=badge)
