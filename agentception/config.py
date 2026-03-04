"""AgentCeption service configuration.

All settings are prefixed with ``AC_`` so they never collide with Maestro's
``STORI_*`` namespace. Defaults work for local development without any env vars set.

When ``pipeline-config.json`` contains a ``projects`` list and an
``active_project`` name, the model validator applies the matching project's
``gh_repo``, ``repo_dir``, and ``worktrees_dir`` values over the env-var
defaults.  This is the primary mechanism for multi-repo generalisation (AC-601).

:func:`settings.reload` re-applies the active project on demand.  The poller
calls it at the top of every tick so a project switch via the GUI takes effect
within one polling interval — no service restart required.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _resolve_project(raw: dict[str, object], target: AgentCeptionSettings) -> None:
    """Apply the active project's path overrides from *raw* onto *target* in-place.

    Extracted as a module-level helper so both the Pydantic validator and
    :meth:`AgentCeptionSettings.reload` can share identical logic without
    duplication.
    """
    active_name: object = raw.get("active_project")
    projects: object = raw.get("projects", [])
    if not isinstance(projects, list) or not active_name:
        return
    for proj in projects:
        if not isinstance(proj, dict) or proj.get("name") != active_name:
            continue
        if "gh_repo" in proj and isinstance(proj["gh_repo"], str):
            target.gh_repo = proj["gh_repo"]
        if "repo_dir" in proj and isinstance(proj["repo_dir"], str):
            target.repo_dir = Path(proj["repo_dir"])
        if "worktrees_dir" in proj and isinstance(proj["worktrees_dir"], str):
            wd = proj["worktrees_dir"]
            if wd.startswith("~/"):
                wd = str(Path.home()) + wd[1:]
            target.worktrees_dir = Path(wd)
        break


class AgentCeptionSettings(BaseSettings):
    """Runtime configuration for the AgentCeption dashboard service.

    Path settings are resolved in order:
    1. Environment variables (``AC_REPO_DIR``, ``AC_WORKTREES_DIR``, etc.)
    2. Active project from ``pipeline-config.json`` (overrides env vars when present)

    Call :meth:`reload` to pick up a changed ``active_project`` at runtime
    without restarting the service.
    """

    model_config = SettingsConfigDict(env_prefix="AC_")

    cursor_projects_dir: Path = Path.home() / ".cursor/projects"
    worktrees_dir: Path = Path.home() / ".agentception/worktrees/maestro"
    host_worktrees_dir: Path = Path.home() / ".agentception/worktrees/maestro"
    """Host-side path to the worktrees directory.

    Inside Docker, ``worktrees_dir`` is the container path (``/worktrees``).
    ``host_worktrees_dir`` is the corresponding path on the developer's machine
    (e.g. ``~/.agentception/worktrees/maestro``), used to generate paths that the
    user can open directly in Cursor and that the agent-task file embeds.
    Set via ``AC_HOST_WORKTREES_DIR`` in docker-compose.override.yml.
    """
    repo_dir: Path = Path.cwd()
    gh_repo: str = "cgcardona/maestro"

    @property
    def ac_dir(self) -> Path:
        """Canonical path to the ``.agentception/`` directory at the repo root.

        All AgentCeption-owned config files (roles, prompts, pipeline-config,
        dispatcher prompt, etc.) live here — not in ``.cursor/``, which belongs
        to the IDE.
        """
        return self.repo_dir / ".agentception"
    poll_interval_seconds: int = 5
    github_cache_seconds: int = 10
    database_url: str | None = None
    openrouter_api_key: str = ""
    """OpenRouter API key for direct LLM calls (plan phase preview, enrichment).

    Set via ``AC_OPENROUTER_API_KEY`` env var.  When absent the Phase Planner
    falls back to the keyword-based heuristic classifier — no LLM is required
    for the service to start.
    """
    """Async database URL for AgentCeption's own ac_* tables.

    Set via ``AC_DATABASE_URL`` env var (docker-compose injects this).
    Falls back to a local SQLite file when absent so the service starts
    without Postgres in pure-filesystem dev mode.
    """

    @model_validator(mode="after")
    def _apply_active_project(self) -> AgentCeptionSettings:
        """Override path settings from the active project in ``pipeline-config.json``.

        Reads the config file synchronously at initialisation so that all
        downstream code that imports ``settings`` sees the correct project
        paths immediately.  If the file is absent, malformed, or has no
        ``active_project`` key, the validator is a no-op.
        """
        config_path = self.repo_dir / ".agentception" / "pipeline-config.json"
        if not config_path.exists():
            return self
        try:
            raw: object = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover — filesystem error path
            logger.warning("⚠️ Could not read pipeline-config.json for project override: %s", exc)
            return self
        if not isinstance(raw, dict):
            return self
        _resolve_project(raw, self)
        return self

    def reload(self) -> None:
        """Re-read ``pipeline-config.json`` and apply the active project's paths in-place.

        Called by the poller at the start of each tick and by the
        ``switch-project`` API endpoint so project switches take effect
        within one polling interval — no service restart required.

        This method is synchronous: reading a small local JSON file is fast
        enough that wrapping it in an executor would add more overhead than
        it saves.
        """
        config_path = self.repo_dir / ".agentception" / "pipeline-config.json"
        if not config_path.exists():
            return
        try:
            raw: object = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("⚠️ Could not read pipeline-config.json during reload: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        _resolve_project(raw, self)
        logger.debug("✅ Settings reloaded — gh_repo=%s repo_dir=%s", self.gh_repo, self.repo_dir)


settings = AgentCeptionSettings()
