from __future__ import annotations

"""Route: POST /api/control/resync-issues.

Triggers a forced full GitHub issue sync (open + closed) without a server
restart.  Intended for Mission Control and operator tooling.

When the request carries ``Accept: text/html`` (e.g. from an HTMX button),
the endpoint returns a bare HTML fragment rendered from
``_resync_result.html`` instead of JSON.  JSON is the default for all other
callers (curl, API clients, tests).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from agentception.config import settings
from agentception.services.resync_service import resync_all_issues

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/control", tags=["control"])

_TEMPLATES = Jinja2Templates(directory="agentception/templates")


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


def _wants_html(request: Request) -> bool:
    """Return True when the caller prefers an HTML fragment over JSON.

    HTMX sends ``Accept: text/html, */*`` by default; plain API clients
    send ``Accept: application/json`` or omit the header entirely.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept


@router.post("/resync-issues", response_model=None)
async def post_resync_issues(request: Request) -> HTMLResponse | JSONResponse:
    """Force a full open+closed issue sync from the configured GitHub repository.

    Always uses ``settings.gh_repo`` — no repo parameter is accepted so there
    is no risk of fetching from one repo while writing to another.

    Returns
    -------
    200
        ``ResyncOkResponse`` — counts of open, closed, and upserted issues.
        When ``Accept: text/html``, returns a bare HTML fragment instead.
    422
        ``ResyncErrorResponse`` — ``GH_REPO`` is not configured.
    503
        ``ResyncErrorResponse`` — GitHub API raised an error.
        When ``Accept: text/html``, returns a bare HTML fragment instead.
    """
    html_response = _wants_html(request)

    if not settings.gh_repo:
        error_msg = "No repository configured. Set GH_REPO in the environment."
        if html_response:
            return _TEMPLATES.TemplateResponse(
                "_resync_result.html",
                {"request": request, "ok": False, "error": error_msg},
                status_code=422,
            )
        body = ResyncErrorResponse(ok=False, error=error_msg)
        return JSONResponse(status_code=422, content=body.model_dump())

    try:
        counts = await resync_all_issues()
    except Exception as exc:
        logger.exception("resync_all_issues failed: %s", exc)
        error_msg = str(exc)
        if html_response:
            return _TEMPLATES.TemplateResponse(
                "_resync_result.html",
                {"request": request, "ok": False, "error": error_msg},
                status_code=503,
            )
        body_err = ResyncErrorResponse(ok=False, error=error_msg)
        return JSONResponse(status_code=503, content=body_err.model_dump())

    if html_response:
        return _TEMPLATES.TemplateResponse(
            "_resync_result.html",
            {
                "request": request,
                "ok": True,
                "open": counts["open"],
                "closed": counts["closed"],
                "upserted": counts["upserted"],
            },
        )
    body_ok = ResyncOkResponse(ok=True, **counts)
    return JSONResponse(status_code=200, content=body_ok.model_dump())
