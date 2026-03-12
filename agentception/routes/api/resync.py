from __future__ import annotations

"""Route: POST /api/control/resync-issues

Thin HTTP wrapper around ``resync_all_issues()`` from the resync service.
Accepts an optional ``repo`` query parameter (defaults to the configured
``settings.gh_repo``).  Returns counts of open, closed, and upserted issues
on success, or a 503 with an error message when the GitHub API is unreachable.
"""

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agentception.config import settings
from agentception.services.resync_service import GitHubAPIError, resync_all_issues

logger = logging.getLogger(__name__)

router = APIRouter()


class ResyncOkResponse(BaseModel):
    """Successful resync response — counts of issues processed."""

    ok: bool
    open: int
    closed: int
    upserted: int


class ResyncErrorResponse(BaseModel):
    """Error response returned when the GitHub API call fails."""

    ok: bool
    error: str


@router.post("/control/resync-issues", tags=["control"])
async def resync_issues(
    repo: str = Query(
        default="",
        description=(
            "Full owner/repo string (e.g. 'cgcardona/agentception'). "
            "Defaults to the configured default repo when omitted."
        ),
    ),
) -> JSONResponse:
    """Force a full re-sync of all GitHub issues into the local DB.

    Fetches every open and closed issue from GitHub and upserts them.
    Useful when the local DB has drifted from GitHub state (e.g. after a
    DB reset or a missed poller tick).

    Returns counts so the caller can confirm how many issues were processed.
    """
    effective_repo = repo.strip() or settings.gh_repo
    if not effective_repo:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": (
                    "No repo configured. Set AC_GH_REPO in the environment "
                    "or pass ?repo=owner/repo as a query parameter."
                ),
            },
        )

    logger.info("POST /api/control/resync-issues repo=%s", effective_repo)

    try:
        result = await resync_all_issues(effective_repo)
    except GitHubAPIError as exc:
        logger.warning("resync_issues: GitHub API error: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": str(exc)},
        )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "open": result.open,
            "closed": result.closed,
            "upserted": result.upserted,
        },
    )
