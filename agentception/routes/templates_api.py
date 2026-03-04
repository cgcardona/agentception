from __future__ import annotations

"""Template export / import API routes (AC-602).

These endpoints let users package the current pipeline configuration as a
versioned ``.tar.gz`` and later import it into any target repository.

Endpoints:

- ``POST /api/templates/export`` — build and store a template archive
- ``POST /api/templates/import`` — unpack an uploaded archive into a target repo
- ``GET /api/templates`` — list previously exported templates
- ``GET /api/templates/{filename}`` — download a stored template archive
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response

from agentception.models import (
    TemplateExportRequest,
    TemplateImportResult,
    TemplateListEntry,
)
from agentception.readers.templates import (
    TEMPLATES_STORE,
    export_template,
    import_template,
    list_stored_templates,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.post("/export", tags=["templates"])
async def export_template_endpoint(body: TemplateExportRequest) -> Response:
    """Export the current pipeline configuration as a versioned ``.tar.gz`` archive.

    Creates an archive containing all managed ``.cursor/`` files (roles,
    PARALLEL_*.md, pipeline-config.json, agent-command-policy.md) plus a
    ``template-manifest.json`` with provenance metadata.  The archive is
    persisted to ``~/.cursor/agentception-templates/`` and returned as a
    file download.

    Parameters
    ----------
    body.name:
        Human-readable template name (embedded in the manifest).
    body.version:
        Semver-style version string (e.g. ``"1.0.0"``).

    Returns
    -------
    Response
        ``application/gzip`` download with ``Content-Disposition`` set to the
        suggested filename.

    Raises
    ------
    HTTP 422
        When ``name`` or ``version`` fail Pydantic validation.
    HTTP 500
        When the archive cannot be created (e.g. filesystem error).
    """
    try:
        archive_bytes, filename = export_template(body.name, body.version)
    except Exception as exc:
        logger.exception("❌ Template export failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Template export failed: {exc}",
        ) from exc

    return Response(
        content=archive_bytes,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", tags=["templates"])
async def import_template_endpoint(
    file: UploadFile,
    target_repo: str,
) -> TemplateImportResult:
    """Import a template archive into a target repository.

    Extracts the ``.tar.gz`` into ``{target_repo}/.cursor/``, creating parent
    directories as needed.  Files that already exist are overwritten — the
    response ``conflicts`` list identifies which files were clobbered so the
    caller can warn the user.

    Parameters
    ----------
    file:
        Uploaded ``.tar.gz`` archive produced by ``POST /api/templates/export``.
    target_repo:
        Absolute filesystem path to the target repository root.  The caller
        is responsible for ensuring the path is writable and trustworthy.

    Raises
    ------
    HTTP 400
        When the archive is invalid or missing ``template-manifest.json``.
    HTTP 422
        When ``target_repo`` is missing or the upload fails validation.
    HTTP 500
        When extraction fails due to a filesystem error.
    """
    try:
        archive_bytes = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read uploaded file: {exc}",
        ) from exc

    try:
        result = import_template(archive_bytes, target_repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("❌ Template import failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Template import failed: {exc}",
        ) from exc

    return result


@router.get("", tags=["templates"])
async def list_templates_endpoint() -> list[TemplateListEntry]:
    """Return metadata for all previously exported templates.

    Reads ``template-manifest.json`` from each ``.tar.gz`` in the templates
    store directory (``~/.cursor/agentception-templates/``).  Unreadable
    archives are silently skipped.  Results are sorted most-recent-first.
    """
    return list_stored_templates()


@router.get("/{filename}", tags=["templates"])
async def download_template_endpoint(filename: str) -> Response:
    """Download a specific stored template archive by filename.

    Parameters
    ----------
    filename:
        The ``.tar.gz`` filename as returned by ``GET /api/templates``.

    Raises
    ------
    HTTP 404
        When no archive with that filename exists in the store.
    """
    archive_path = TEMPLATES_STORE / filename
    if not archive_path.is_file() or not filename.endswith(".tar.gz"):
        raise HTTPException(
            status_code=404,
            detail=f"Template archive not found: {filename!r}",
        )
    return Response(
        content=archive_path.read_bytes(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
