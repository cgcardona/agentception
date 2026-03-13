from __future__ import annotations

"""JSON API routes sub-package — replaces the monolithic ``routes/api.py``.

Each domain module owns a focused set of endpoints.  This ``__init__`` assembles
the combined ``/api``-prefixed router and re-exports nothing (the only public
symbol is ``router``).

``app.py`` does ``from agentception.routes.api import router as api_router`` — that
import path continues to work unchanged.
"""

from fastapi import APIRouter

from .agent_run import router as _agent_run
from .system import router as _system
from .dispatch import router as _dispatch
from .runs import router as _runs
from .ship_api import router as _ship_api
from .config import router as _config
from .control import router as _control
from .health import router as _health
from .version import router as _version
from .intelligence import router as _intelligence
from .issues import router as _issues
from .mcp import router as _mcp
from .pipeline import router as _pipeline
from .plan import router as _plan
from .presets import router as _presets
from .resync import router as _resync
from .telemetry import router as _telemetry
from .wizard import router as _wizard
from .ab_metrics import router as _ab_metrics
from .local_llm import router as _local_llm
from .metrics import router as _metrics
from .worktrees import router as _worktrees

router = APIRouter(prefix="/api", tags=["api"])
router.include_router(_agent_run)
router.include_router(_system)
router.include_router(_dispatch)
router.include_router(_runs)
router.include_router(_ship_api)
router.include_router(_pipeline)
router.include_router(_control)
router.include_router(_resync)
router.include_router(_config)
router.include_router(_health)
router.include_router(_version)
router.include_router(_intelligence)
router.include_router(_mcp)
router.include_router(_telemetry)
router.include_router(_worktrees)
router.include_router(_issues)
router.include_router(_wizard)
router.include_router(_plan)
router.include_router(_presets)
router.include_router(_metrics)
router.include_router(_ab_metrics)
router.include_router(_local_llm)
