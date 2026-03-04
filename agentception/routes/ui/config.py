"""UI route: pipeline configuration panel."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.models import PipelineConfig
from agentception.readers.pipeline_config import read_pipeline_config
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_APPROVAL_LABELS: list[str] = ["db-schema", "security", "api-contract"]


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request) -> HTMLResponse:
    """Pipeline configuration panel — sliders for VP count and pool size.

    Renders the pipeline config UI (AC-305): allocation sliders for max_eng_vps,
    max_qa_vps, pool_size_per_vp, and a drag-and-drop label order editor.
    The page loads current values from ``GET /api/config`` on mount via Alpine.js
    and persists changes via ``PUT /api/config`` on save.

    Pre-populates the ``config`` template variable from the config file so the
    initial render reflects current values even before Alpine.js hydrates.
    On any read error the page still renders with hardcoded defaults — the save
    button is always accessible.
    """
    config: PipelineConfig | None = None
    try:
        config = await read_pipeline_config()
    except Exception:  # pragma: no cover — filesystem error path
        pass
    approval_labels: list[str] = (
        config.approval_required_labels if config is not None else _DEFAULT_APPROVAL_LABELS
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "config.html",
        {"config": config, "approval_required_labels": approval_labels},
    )
