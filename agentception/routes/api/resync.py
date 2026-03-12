from __future__ import annotations

"""Route: POST /api/control/resync-issues.

Triggers a forced full GitHub issue sync (open + closed) without a server
restart.  Intended for Mission Control and operator tooling.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agentception.config import settings
from agentception.services.resync_service import resync_all_issues

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/control", tags=["control"])


class ResyncOkResponse(BaseModel):
    """Successful resync response."""

    ok: bool
    open: int
    closed: int
    upserted: int


class ResyncErrorResponse(BaseModel):
    """Error resync response."""

    ok: bool
    error: str


@router.post("/resync-issues")
async def post_resync_issues() -> JSONResponse:
    """Force a full open+closed issue sync from the configured GitHub repository.

    Always uses ``settings.gh_repo`` — no repo parameter is accepted so there
    is no risk of fetching from one repo while writing to another.

    Returns
    -------
    200
        ``ResyncOkResponse`` — counts of open, closed, and upserted issues.
    422
        ``ResyncErrorResponse`` — ``GH_REPO`` is not configured.
    503
        ``ResyncErrorResponse`` — GitHub API raised an error.
    """
    if not settings.gh_repo:
        body = ResyncErrorResponse(
            ok=False,
            error="No repository configured. Set GH_REPO in the environment.",
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    try:
        counts = await resync_all_issues()
    except Exception as exc:
        logger.exception("resync_all_issues failed: %s", exc)
        body_err = ResyncErrorResponse(ok=False, error=str(exc))
        return JSONResponse(status_code=503, content=body_err.model_dump())

    body_ok = ResyncOkResponse(ok=True, **counts)
    return JSONResponse(status_code=200, content=body_ok.model_dump())
