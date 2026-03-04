"""UI routes sub-package — replaces the monolithic ``routes/ui.py``.

Each domain module owns a focused set of route handlers and imports only what
it needs.  This ``__init__`` assembles the combined router and re-exports the
shared helpers that ``agentception/routes/api/`` historically imported from the
old ``ui.py`` module.

``app.py`` does ``from agentception.routes.ui import router as ui_router`` — that
import path continues to work unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter

from .ab_testing import router as _ab
from .agents import router as _agents
from .build_ui import router as _build_ui
from .cognitive_arch import router as _cognitive_arch
from .api_reference import router as _api_reference
from .plan_ui import router as _plan_ui
from .config import router as _config
from .dag import router as _dag
from .docs import router as _docs
from .github_ui import router as _github
from .org_chart import router as _org_chart
from .overview import router as _overview
from .roles_ui import router as _roles_ui
from .telemetry import router as _telemetry
from .settings import router as _settings
from .templates_ui import router as _templates_ui
from .transcripts import router as _transcripts
from .worktrees import router as _worktrees

# Re-export shared symbols so that existing imports from ``agentception.routes.ui``
# continue to resolve without changes — specifically the api/ sub-modules that
# import ``_find_agent`` and ``_TEMPLATES`` from the old flat module path.
from ._shared import _find_agent, _TEMPLATES  # noqa: F401

router = APIRouter(tags=["ui"])
router.include_router(_plan_ui)   # owns "/" — must be first
router.include_router(_build_ui)
router.include_router(_overview)
router.include_router(_agents)
router.include_router(_telemetry)
router.include_router(_dag)
router.include_router(_config)
router.include_router(_ab)
router.include_router(_roles_ui)
router.include_router(_org_chart)
router.include_router(_github)
router.include_router(_transcripts)
router.include_router(_worktrees)
router.include_router(_docs)
router.include_router(_api_reference)
router.include_router(_settings)
router.include_router(_templates_ui)
router.include_router(_cognitive_arch)
