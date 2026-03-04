"""UI route: template marketplace page."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/templates", response_class=HTMLResponse)
async def templates_ui(request: Request) -> HTMLResponse:
    """Template marketplace — export and import pipeline configuration bundles.

    Renders the templates management page which lets the user:
    - Export the current pipeline config as a versioned ``.tar.gz``.
    - Import a template archive into any target repo.
    - Browse previously exported templates.
    """
    from agentception.readers.templates import list_stored_templates

    stored = list_stored_templates()
    return _TEMPLATES.TemplateResponse(
        request,
        "templates.html",
        {"stored_templates": stored},
    )
