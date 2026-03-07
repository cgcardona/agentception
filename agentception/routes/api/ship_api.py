from __future__ import annotations

"""Ship page API routes — UI actions scoped to an initiative.

Endpoint
--------
POST /api/ship/{org}/{repo}/{initiative}/advance — advance the phase gate for an initiative.
"""

import logging

from fastapi import APIRouter, Response
from pydantic import BaseModel

from agentception.mcp.plan_advance_phase import plan_advance_phase as _plan_advance_phase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ship", tags=["ship"])


# ---------------------------------------------------------------------------
# Phase gate — advance a phase by unlocking all to_phase issues
# ---------------------------------------------------------------------------


class AdvancePhaseBody(BaseModel):
    """Body for ``POST /api/ship/{org}/{repo}/{initiative}/advance``.

    ``org``, ``repo``, and ``initiative`` are encoded in the URL path.
    """

    from_phase: str
    """The phase label that must be fully closed (e.g. ``"phase-1"``)."""
    to_phase: str
    """The phase label to unlock (e.g. ``"phase-2"``)."""


class AdvancePhaseOk(BaseModel):
    """Successful phase advance — all from_phase issues were closed."""

    advanced: bool
    unlocked_count: int


class AdvancePhaseBlocked(BaseModel):
    """Blocked phase advance — one or more from_phase issues remain open."""

    advanced: bool
    error: str
    open_issues: list[int]


@router.post("/{org}/{repo}/{initiative}/advance", response_model=None)
async def advance_phase(
    org: str,
    repo: str,
    initiative: str,
    req: AdvancePhaseBody,
    response: Response,
) -> AdvancePhaseOk | AdvancePhaseBlocked:
    """Advance the phase gate by unlocking all *to_phase* issues.

    Delegates to ``plan_advance_phase()`` which validates the gate condition
    (all from_phase issues closed) and mutates GitHub labels atomically.

    On success: sets ``HX-Trigger: refreshBoard`` so the Ship board partial
    auto-refreshes in the same HTMX response cycle without a full-page reload.

    Returns:
        ``AdvancePhaseOk`` when the gate passes and labels are mutated.
        ``AdvancePhaseBlocked`` when open issues still block the transition.
    """
    result = await _plan_advance_phase(initiative, req.from_phase, req.to_phase)

    if result.get("advanced") is True:
        unlocked_raw = result.get("unlocked_count")
        unlocked_count = unlocked_raw if isinstance(unlocked_raw, int) else 0
        response.headers["HX-Trigger"] = "refreshBoard"
        logger.info(
            "✅ advance_phase: initiative=%r %r → %r, %d issue(s) unlocked",
            initiative, req.from_phase, req.to_phase, unlocked_count,
        )
        return AdvancePhaseOk(advanced=True, unlocked_count=unlocked_count)

    error_raw = result.get("error")
    error_str = error_raw if isinstance(error_raw, str) else "Phase advance blocked."
    open_raw = result.get("open_issues")
    open_issues: list[int] = (
        [i for i in open_raw if isinstance(i, int)]
        if isinstance(open_raw, list)
        else []
    )
    logger.warning("⚠️ advance_phase: blocked — %s", error_str)
    return AdvancePhaseBlocked(advanced=False, error=error_str, open_issues=open_issues)
