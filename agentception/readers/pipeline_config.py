from __future__ import annotations

"""Pipeline configuration reader/writer for AgentCeption.

The canonical source of truth for pipeline allocation parameters is
``.agentception/pipeline-config.json`` in the repository root.  The CTO and
Engineering VP role files read this file at the start of every loop/seed
cycle instead of relying on hardcoded constants.

The dashboard exposes GET/PUT ``/api/config`` routes (see
``agentception/routes/api.py``) so operators can adjust allocation without
restarting the service.
"""
from __future__ import annotations

import json
from pathlib import Path

from agentception.config import settings
from agentception.models import PipelineConfig

# Default values mirror the spec exactly — used when the config file is absent.
# Kept as a typed dict so tests can inspect individual keys without constructing
# a full PipelineConfig.
_DEFAULTS: dict[str, int | list[str]] = {
    "max_eng_vps": 1,
    "max_qa_vps": 1,
    "pool_size_per_vp": 4,
    "active_labels_order": [
        "ac-ui/0-critical-bugs",
        "ac-ui/1-design-tokens",
        "ac-ui/2-data-model",
        "ac-ui/3-core-pages",
        "ac-ui/4-controls-intelligence",
        "ac-ui/5-polish",
    ],
}

_CONFIG_PATH: Path = settings.ac_dir / "pipeline-config.json"


def _config_path() -> Path:
    """Return the resolved path to ``pipeline-config.json``.

    Factored out so tests can patch ``_config_path`` without touching the
    module-level constant (which is evaluated at import time and therefore
    hard to redirect in unit tests).
    """
    return _CONFIG_PATH


async def read_pipeline_config() -> PipelineConfig:
    """Read and return the pipeline configuration from disk, validated as a PipelineConfig.

    Falls back to :data:`_DEFAULTS` when the config file does not exist yet.
    Uses ``model_validate`` to give callers a schema-checked value in all
    environments, including when the on-disk file was hand-edited.

    Returns
    -------
    PipelineConfig
        The current allocation settings, guaranteed to satisfy the Pydantic schema.

    Raises
    ------
    pydantic.ValidationError
        When the file exists but its contents do not conform to the PipelineConfig schema.
    """
    path = _config_path()
    if not path.exists():
        return PipelineConfig.model_validate(_DEFAULTS)
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    return PipelineConfig.model_validate(raw)


async def switch_project(project_name: str) -> PipelineConfig:
    """Set ``active_project`` in ``pipeline-config.json`` and return the updated config.

    Validates that *project_name* matches an existing entry in
    ``PipelineConfig.projects`` before writing.  A name that does not exist
    raises :class:`ValueError` so callers (the API route) can surface HTTP 404
    rather than silently persisting an invalid value.

    Parameters
    ----------
    project_name:
        The ``name`` field of the project to activate.

    Returns
    -------
    PipelineConfig
        The updated config with ``active_project`` set to *project_name*.

    Raises
    ------
    ValueError
        When *project_name* does not match any entry in ``projects``.
    """
    config = await read_pipeline_config()
    known_names = [p.name for p in config.projects]
    if project_name not in known_names:
        raise ValueError(
            f"Unknown project {project_name!r}. "
            f"Available: {known_names if known_names else '(no projects configured)'}"
        )
    updated = config.model_copy(update={"active_project": project_name})
    return await write_pipeline_config(updated)


async def write_pipeline_config(config: PipelineConfig) -> PipelineConfig:
    """Persist *config* to disk and return the saved value.

    The file is written with 2-space indentation so it remains human-readable.
    The parent directory is created automatically when it does not exist.

    Parameters
    ----------
    config:
        Validated PipelineConfig to persist.  The ``PUT /api/config`` route
        validates the incoming body against the Pydantic schema before calling
        this function, so corrupt values never reach the filesystem.

    Returns
    -------
    PipelineConfig
        The config that was written, identical to *config*.
    """
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    return config
