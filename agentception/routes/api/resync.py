from __future__ import annotations

"""Route: POST /api/control/resync-issues.

Triggers a forced full GitHub issue sync (open + closed) without a server
restart.  Intended for Mission Control and operator tooling.

When the request carries the ``HX-Request`` header (i.e. the call originates
from an HTMX element), the endpoint returns a bare HTML fragment instead of
JSON.  JSON is the default for all other callers (curl, API clients, tests).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from agentception.config import settings
from agentception.services.resync_service import resync_all_issues

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/control", tags=["control"])

_RESYNC_ERROR_HTML = '<span class="resync-error">Resync failed — try again</span>'


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


def _is_htmx(request: Request) -> bool:
    """Return True when the request originates from an HTMX element.

    HTMX sets the ``HX-Request: true`` header on every request it makes.
    Plain API clients (curl, httpx, etc.) do not set this header.
    """
    return request.headers.get("HX-Request") == "true"


@router.post("/resync-issues", response_model=None)
async def post_resync_issues(request: Request) -> HTMLResponse | JSONResponse:
    """Force a full open+closed issue sync from the configured GitHub repository.

    Always uses ``settings.gh_repo`` — no repo parameter is accepted so there
    is no risk of fetching from one repo while writing to another.

    Returns
    -------
    200
        ``ResyncOkResponse`` — counts of open, closed, and upserted issues.
        When called via HTMX (``HX-Request: true``), returns an empty HTML body.
    422
        ``ResyncErrorResponse`` — ``GH_REPO`` is not configured.
    503
        ``ResyncErrorResponse`` — GitHub API raised an error.
        When called via HTMX, returns an HTML error fragment with class
        ``resync-error``.
    """
    htmx = _is_htmx(request)

    if not settings.gh_repo:
        error_msg = "No repository configured. Set GH_REPO in the environment."
        if htmx:
            return HTMLResponse(content=_RESYNC_ERROR_HTML, status_code=503)
        body = ResyncErrorResponse(ok=False, error=error_msg)
        return JSONResponse(status_code=422, content=body.model_dump())

    try:
        counts = await resync_all_issues()
    except Exception as exc:
        logger.exception("resync_all_issues failed: %s", exc)
        if htmx:
            return HTMLResponse(content=_RESYNC_ERROR_HTML, status_code=503)
        body_err = ResyncErrorResponse(ok=False, error=str(exc))
        return JSONResponse(status_code=503, content=body_err.model_dump())

    if htmx:
        # Trigger immediate board refresh so the UI shows fresh data from GitHub.
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "refreshBoard"},
        )

    body_ok = ResyncOkResponse(ok=True, **counts)
    return JSONResponse(status_code=200, content=body_ok.model_dump())
