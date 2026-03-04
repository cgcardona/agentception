"""API routes: pipeline configuration read and write."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agentception.models import PipelineConfig, SwitchProjectRequest
from agentception.readers.pipeline_config import read_pipeline_config, switch_project, write_pipeline_config

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/config", tags=["config"])
async def get_config() -> PipelineConfig:
    """Return the current pipeline allocation configuration.

    Reads ``.cursor/pipeline-config.json`` from disk on every call so that
    manual edits to the file are reflected immediately without a service restart.
    Falls back to compiled-in defaults when the file does not exist.
    """
    return await read_pipeline_config()


@router.put("/config", tags=["config"])
async def update_config(body: PipelineConfig) -> PipelineConfig:
    """Persist updated pipeline allocation settings to disk.

    Validates the incoming body against :class:`~agentception.models.PipelineConfig`
    before writing, so callers receive a 422 on schema violations rather than
    silently corrupting the config file.

    Returns the saved config so callers can confirm what was written.
    """
    return await write_pipeline_config(body)


@router.post("/config/switch-project", tags=["config"])
async def switch_project_endpoint(body: SwitchProjectRequest) -> PipelineConfig:
    """Switch the active project in ``pipeline-config.json``.

    Sets ``active_project`` to *body.project_name*, persists the updated
    config, then immediately reloads ``settings`` so the poller targets the
    new repo on its very next tick — no service restart required.

    Parameters
    ----------
    body.project_name:
        The ``name`` of the project to activate.  Must match an entry in
        ``PipelineConfig.projects``.

    Returns
    -------
    PipelineConfig
        The updated config with ``active_project`` set.

    Raises
    ------
    HTTP 404
        When *project_name* does not match any configured project.
    """
    try:
        result = await switch_project(body.project_name)
        # Apply the new project's paths immediately — readers pick them up
        # on the very next call without waiting for a service restart.
        from agentception.config import settings
        settings.reload()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
