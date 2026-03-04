"""UI routes: .cursor/ docs viewer."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request
from starlette.responses import Response

from agentception.config import settings as _settings
from ._shared import _TEMPLATES, _md_to_html

logger = logging.getLogger(__name__)

router = APIRouter()

_CURSOR_DIR = Path(_settings.repo_dir) / ".cursor"


def _scan_cursor_docs() -> list[dict[str, str]]:
    """Auto-discover all markdown files in .cursor/ sorted alphabetically.

    Returns a list of {slug, label, file} dicts. Label is derived from the
    filename by replacing hyphens and underscores with spaces and title-casing.
    """
    if not _CURSOR_DIR.exists():
        return []
    docs: list[dict[str, str]] = []
    for f in sorted(_CURSOR_DIR.glob("*.md")):
        slug = f.stem
        label = slug.replace("-", " ").replace("_", " ").title()
        docs.append({"slug": slug, "label": label, "file": f.name})
    return docs


def _render_doc(slug: str) -> tuple[str | None, str | None, str | None]:
    """Read and render a doc file.

    Returns (label, content_html, error). ``content_html`` is Markdown
    rendered to safe HTML; ``error`` is set on read failure.
    """
    docs = _scan_cursor_docs()
    doc_meta = next((d for d in docs if d["slug"] == slug), None)
    if doc_meta is None:
        return None, None, f"Unknown doc: {slug}"
    file_path = _CURSOR_DIR / doc_meta["file"]
    try:
        raw = file_path.read_text(encoding="utf-8")
        return doc_meta["label"], _md_to_html(raw), None
    except FileNotFoundError:
        return doc_meta["label"], None, f"File not found: {file_path}"
    except OSError as exc:
        return doc_meta["label"], None, str(exc)


@router.get("/docs", response_class=HTMLResponse)
async def docs_index(request: Request) -> Response:
    """Redirect to the first available doc."""
    docs = _scan_cursor_docs()
    if docs:
        return RedirectResponse(url=f"/docs/{docs[0]['slug']}", status_code=302)
    raise HTTPException(status_code=404, detail="No .cursor/ docs found")


@router.get("/docs/{slug}", response_class=HTMLResponse)
async def docs_viewer(request: Request, slug: str) -> HTMLResponse:
    """Full page: sidebar + rendered Markdown content."""
    label, content_html, error = _render_doc(slug)
    if label is None:
        raise HTTPException(status_code=404, detail=f"Unknown doc slug: {slug}")
    return _TEMPLATES.TemplateResponse(
        request,
        "docs.html",
        {
            "slug": slug,
            "label": label,
            "content_html": content_html,
            "error": error,
            "available_docs": [
                {"slug": d["slug"], "label": d["label"]}
                for d in _scan_cursor_docs()
            ],
        },
    )


@router.get("/docs/{slug}/content", response_class=HTMLResponse)
async def docs_content_partial(request: Request, slug: str) -> HTMLResponse:
    """HTMX partial: just the main content panel (no sidebar, no chrome)."""
    label, content_html, error = _render_doc(slug)
    if label is None:
        raise HTTPException(status_code=404, detail=f"Unknown doc slug: {slug}")
    return _TEMPLATES.TemplateResponse(
        request,
        "_doc_content.html",
        {
            "slug": slug,
            "label": label,
            "content_html": content_html,
            "error": error,
        },
    )
