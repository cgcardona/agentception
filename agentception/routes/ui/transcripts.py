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
    """Browse all agent transcripts indexed from the Cursor filesystem.

    Query parameters:
    - ``role``   — filter to a specific inferred role string
    - ``status`` — "done" or "unknown"
    - ``issue``  — filter to transcripts mentioning a specific issue number
    - ``q``      — free-text search against the preview text (case-insensitive)
    """
    from agentception.readers.transcripts import find_transcript_root, index_transcripts

    error: str | None = None
    transcripts: list[dict[str, object]] = []
    transcripts_dir_str: str = ""

    filter_role: str = request.query_params.get("role", "").strip()
    filter_status: str = request.query_params.get("status", "").strip()
    filter_issue_raw: str = request.query_params.get("issue", "").strip()
    filter_q: str = request.query_params.get("q", "").strip().lower()
    filter_issue: int | None = int(filter_issue_raw) if filter_issue_raw.isdigit() else None

    try:
        tr_root = await find_transcript_root()
        if tr_root is not None:
            transcripts_dir_str = str(tr_root)
            all_transcripts = await index_transcripts(tr_root)
            _all_transcripts_for_roles = all_transcripts

            # Server-side filter pass
            for t in all_transcripts:
                if filter_role and t.get("role") != filter_role:
                    continue
                if filter_status and t.get("status") != filter_status:
                    continue
                if filter_issue is not None:
                    li = t.get("linked_issues")
                    if not isinstance(li, list) or filter_issue not in li:
                        continue
                if filter_q:
                    preview = t.get("preview")
                    if not isinstance(preview, str) or filter_q not in preview.lower():
                        continue
                transcripts.append(t)
        else:
            error = "Transcript directory not found — check CURSOR_PROJECTS_DIR setting."
    except Exception as exc:
        error = str(exc)

    # Collect unique roles from the full unfiltered index for the filter UI
    all_roles: list[str] = []
    seen_roles: set[str] = set()
    for t in (_all_transcripts_for_roles if '_all_transcripts_for_roles' in dir() else transcripts):
        r = str(t.get("role") or "unknown")
        if r not in seen_roles:
            seen_roles.add(r)
            all_roles.append(r)

    return _TEMPLATES.TemplateResponse(
        request,
        "transcripts.html",
        {
            "transcripts": transcripts,
            "transcripts_dir": transcripts_dir_str,
            "error": error,
            "filter_role": filter_role,
            "filter_status": filter_status,
            "filter_issue": filter_issue,
            "filter_q": filter_q,
            "all_roles": sorted(all_roles),
            "total": len(transcripts),
        },
    )


@router.get("/transcripts/{uuid}", response_class=HTMLResponse)
async def transcript_detail(request: Request, uuid: str) -> HTMLResponse:
    """Full detail view for a single agent conversation."""
    from agentception.readers.transcripts import find_transcript_root, read_transcript_full

    error: str | None = None
    transcript: dict[str, object] | None = None

    try:
        tr_root = await find_transcript_root()
        if tr_root is not None:
            transcript = await read_transcript_full(uuid, tr_root)
            if transcript is None:
                error = f"Transcript {uuid!r} not found in {tr_root}"
        else:
            error = "Transcript directory not found — check CURSOR_PROJECTS_DIR setting."
    except Exception as exc:
        error = str(exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "transcript_detail.html",
        {
            "transcript": transcript,
            "uuid": uuid,
            "error": error,
        },
    )
