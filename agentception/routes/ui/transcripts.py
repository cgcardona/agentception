"""UI routes: agent transcript browser and detail view."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/transcripts", response_class=HTMLResponse)
async def transcripts_browser(request: Request) -> HTMLResponse:
    """Browse agent transcripts stored in the database.

    Query parameters:
    - ``role``   — filter to a specific inferred role string
    - ``status`` — "done" or "unknown"
    - ``issue``  — filter to transcripts mentioning a specific issue number
    - ``q``      — free-text search against the preview text (case-insensitive)

    Transcript storage from Postgres is not yet implemented.
    The page renders with empty results until the DB-backed reader is wired up.
    """
    filter_role: str = request.query_params.get("role", "").strip()
    filter_status: str = request.query_params.get("status", "").strip()
    filter_issue_raw: str = request.query_params.get("issue", "").strip()
    filter_q: str = request.query_params.get("q", "").strip().lower()
    filter_issue: int | None = int(filter_issue_raw) if filter_issue_raw.isdigit() else None

    return _TEMPLATES.TemplateResponse(
        request,
        "transcripts.html",
        {
            "transcripts": [],
            "transcripts_dir": "",
            "error": None,
            "filter_role": filter_role,
            "filter_status": filter_status,
            "filter_issue": filter_issue,
            "filter_q": filter_q,
            "all_roles": [],
            "total": 0,
        },
    )


@router.get("/transcripts/{uuid}", response_class=HTMLResponse)
async def transcript_detail(request: Request, uuid: str) -> HTMLResponse:
    """Full detail view for a single agent conversation.

    Transcript storage from Postgres is not yet implemented.
    Returns an empty detail page until the DB-backed reader is wired up.
    """
    return _TEMPLATES.TemplateResponse(
        request,
        "transcript_detail.html",
        {
            "transcript": None,
            "uuid": uuid,
            "error": None,
        },
    )
