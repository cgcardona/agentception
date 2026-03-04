"""API route: wizard stepper state endpoint.

Implements GET /api/wizard/state which returns the three-step guided-flow
state as JSON (or as an HTMX HTML partial when HX-Request header is present).

Step 1 — Brain Dump:  complete when open GitHub issues carry an ac-workflow/* label.
Step 2 — Org Chart:   complete when pipeline-config.json has a non-null active_org.
Step 3 — Launch Wave: active when ac_waves has a wave started in the last 24 h
                      that has not yet completed (completed_at IS NULL).
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agentception.config import settings
from agentception.db.engine import get_session
from agentception.db.models import ACWave
from agentception.readers.github import get_open_issues
from agentception.routes.ui._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

_UTC = datetime.timezone.utc
_WORKFLOW_LABEL_PREFIX = "ac-workflow/"

# Path to pipeline-config.json — same resolution used by the org-chart route.
_PIPELINE_CONFIG_PATH: Path = settings.ac_dir / "pipeline-config.json"


# ---------------------------------------------------------------------------
# Pydantic models for the JSON response
# ---------------------------------------------------------------------------


class WizardStep1(BaseModel):
    """Brain Dump step — complete when workflow issues exist."""

    complete: bool
    summary: str


class WizardStep2(BaseModel):
    """Org Chart step — complete when an active org is configured."""

    complete: bool
    summary: str


class WizardStep3(BaseModel):
    """Launch Wave step — active when a wave is currently running."""

    active: bool
    summary: str


class WizardState(BaseModel):
    """Full wizard stepper state returned by GET /api/wizard/state."""

    step1: WizardStep1
    step2: WizardStep2
    step3: WizardStep3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_workflow_label(issue: dict[str, object]) -> bool:
    """Return True when an issue carries at least one ac-workflow/* label."""
    raw = issue.get("labels")
    if not isinstance(raw, list):
        return False
    for lbl in raw:
        if isinstance(lbl, str) and lbl.startswith(_WORKFLOW_LABEL_PREFIX):
            return True
        if isinstance(lbl, dict):
            name = lbl.get("name")
            if isinstance(name, str) and name.startswith(_WORKFLOW_LABEL_PREFIX):
                return True
    return False


def _read_active_org() -> str | None:
    """Read active_org from pipeline-config.json; returns None on any error."""
    path = _PIPELINE_CONFIG_PATH
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        value = raw.get("active_org")
        return value if isinstance(value, str) and value else None
    except Exception as exc:
        logger.warning("⚠️  wizard: could not read pipeline-config.json: %s", exc)
        return None


async def _compute_wizard_state() -> WizardState:
    """Compute the live wizard state from GitHub, filesystem, and DB."""

    # ── Step 1: Brain Dump ────────────────────────────────────────────────
    step1_complete = False
    step1_summary = "No workflow issues yet"
    try:
        issues = await get_open_issues()
        wf_issues = [i for i in issues if _has_workflow_label(i)]
        if wf_issues:
            step1_complete = True
            n = len(wf_issues)
            step1_summary = f"{n} issue{'s' if n != 1 else ''} created"
    except Exception as exc:
        logger.warning("⚠️  wizard step1 check failed: %s", exc)

    # ── Step 2: Org Chart ─────────────────────────────────────────────────
    step2_complete = False
    step2_summary = "No active org selected"
    try:
        active_org = _read_active_org()
        if active_org:
            step2_complete = True
            step2_summary = f"Org: {active_org}"
    except Exception as exc:
        logger.warning("⚠️  wizard step2 check failed: %s", exc)

    # ── Step 3: Launch Wave ───────────────────────────────────────────────
    # A wave is "running" when it was started in the last 24 h and has no
    # completed_at timestamp (i.e. spawn is still in progress or ongoing).
    step3_active = False
    step3_summary = "No active wave"
    try:
        cutoff = datetime.datetime.now(_UTC) - datetime.timedelta(hours=24)
        async with get_session() as session:
            result = await session.execute(
                select(ACWave)
                .where(ACWave.started_at >= cutoff, ACWave.completed_at.is_(None))
                .order_by(ACWave.started_at.desc())
                .limit(1)
            )
            wave = result.scalar_one_or_none()
            if wave is not None:
                step3_active = True
                step3_summary = f"Wave {wave.id} running"
    except Exception as exc:
        logger.warning("⚠️  wizard step3 check failed: %s", exc)

    return WizardState(
        step1=WizardStep1(complete=step1_complete, summary=step1_summary),
        step2=WizardStep2(complete=step2_complete, summary=step2_summary),
        step3=WizardStep3(active=step3_active, summary=step3_summary),
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/wizard/state")
async def wizard_state(request: Request) -> Response:
    """Return the current wizard stepper state.

    Returns JSON by default.  When called via HTMX (``HX-Request: true``
    header present) returns the ``_wizard_stepper.html`` HTML partial so the
    stepper can be swapped in-place without a full page reload.  Both paths
    use the same ``_compute_wizard_state()`` logic, so there is no drift
    between the JSON API and the rendered UI.
    """
    state = await _compute_wizard_state()

    if request.headers.get("HX-Request"):
        return _TEMPLATES.TemplateResponse(
            request,
            "_wizard_stepper.html",
            {"state": state},
        )

    return JSONResponse(content=json.loads(state.model_dump_json()))
