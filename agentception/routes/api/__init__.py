"""JSON API routes sub-package — replaces the monolithic ``routes/api.py``.

Each domain module owns a focused set of endpoints.  This ``__init__`` assembles
the combined ``/api``-prefixed router and re-exports nothing (the only public
symbol is ``router``).

``app.py`` does ``from agentception.routes.api import router as api_router`` — that
import path continues to work unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter

from .build import router as _build
from .config import router as _config
from .control import router as _control
from .intelligence import router as _intelligence
from .issues import router as _issues
from .pipeline import router as _pipeline
from .plan import router as _plan
from .telemetry import router as _telemetry
from .wizard import router as _wizard
from .worktrees import router as _worktrees

router = APIRouter(prefix="/api", tags=["api"])
router.include_router(_build)
router.include_router(_pipeline)
router.include_router(_control)
router.include_router(_config)
router.include_router(_intelligence)
router.include_router(_telemetry)
router.include_router(_worktrees)
router.include_router(_issues)
router.include_router(_wizard)
router.include_router(_plan)
