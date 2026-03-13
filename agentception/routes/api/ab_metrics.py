"""A/B metrics API — per-prompt-variant aggregates for developer runs.

Read-only endpoint for comparing control vs treatment (e.g. prompt_variant)
without changing dispatch or prompts. Used when ready to analyse A/B experiments.

When the request includes the HTMX header ``HX-Request: true``, returns an HTML
partial (table) for in-place swap; otherwise returns JSON.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import Response

from agentception.db.engine import get_session
from agentception.routes.ui._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


class ABVariantMetrics(BaseModel):
    """Per-variant aggregate KPIs for completed developer runs."""

    model_config = ConfigDict(frozen=True)

    variant: str
    role: str
    runs: int
    avg_iterations: float
    avg_input_tokens: float
    total_tokens: int
    pass_rate: float
    passed: int
    failed: int


class ABMetricsResponse(BaseModel):
    """Response shape for GET /api/metrics/ab."""

    model_config = ConfigDict(frozen=True)

    variants: list[ABVariantMetrics]


_AB_QUERY = text("""
SELECT
  COALESCE(r.prompt_variant, 'control') AS variant,
  r.role,
  COUNT(DISTINCT r.id)::int AS runs,
  COALESCE(AVG(iter.cnt), 0)::float AS avg_iterations,
  COALESCE(AVG(r.total_input_tokens), 0)::float AS avg_input_tokens,
  COALESCE(SUM(r.total_input_tokens + r.total_output_tokens), 0)::int AS total_tokens,
  COALESCE(AVG(CASE WHEN e.grade IN ('A','B') THEN 1.0 ELSE 0.0 END), 0)::float AS pass_rate,
  COUNT(CASE WHEN e.grade IN ('A','B') THEN 1 END)::int AS passed,
  COUNT(CASE WHEN e.grade IN ('C','D','F') THEN 1 END)::int AS failed
FROM agent_runs r
LEFT JOIN (
  SELECT agent_run_id, COUNT(*) AS cnt
  FROM agent_events
  WHERE event_type = 'step_start'
  GROUP BY agent_run_id
) iter ON iter.agent_run_id = r.id
LEFT JOIN (
  SELECT agent_run_id,
         payload::json->>'grade' AS grade
  FROM agent_events
  WHERE event_type = 'done'
) e ON e.agent_run_id = r.id
WHERE r.role = 'developer'
  AND r.status = 'completed'
  AND r.spawned_at > NOW() - (:days::integer * INTERVAL '1 day')
GROUP BY COALESCE(r.prompt_variant, 'control'), r.role
ORDER BY 1, 2
""")


@router.get("/metrics/ab", response_model=None)
async def get_metrics_ab(
    request: Request,
    days: int = Query(default=7, ge=1, le=90, description="Lookback window in days (1–90)."),
) -> ABMetricsResponse | Response:
    """Return per-prompt-variant aggregates for completed developer runs.

    Runs with ``prompt_variant IS NULL`` are grouped as the ``control`` bucket.
    Pass rate is derived from reviewer grade (A/B = pass, C/D/F = fail) when
    available from the run's done event payload.

    When the request includes the ``HX-Request: true`` header (HTMX), returns
    HTML (partials/_ab_metrics.html) for in-place swap; otherwise returns JSON.
    """
    try:
        async with get_session() as session:
            result = await session.execute(_AB_QUERY, {"days": days})
            rows = result.mappings().all()
    except Exception as exc:
        logger.warning("⚠️ get_metrics_ab DB query failed (non-fatal): %s", exc)
        response = ABMetricsResponse(variants=[])
    else:
        variants = [
            ABVariantMetrics(
                variant=str(row["variant"]),
                role=str(row["role"]),
                runs=int(row["runs"]),
                avg_iterations=float(row["avg_iterations"]),
                avg_input_tokens=float(row["avg_input_tokens"]),
                total_tokens=int(row["total_tokens"]),
                pass_rate=float(row["pass_rate"]),
                passed=int(row["passed"]),
                failed=int(row["failed"]),
            )
            for row in rows
        ]
        response = ABMetricsResponse(variants=variants)

    if request.headers.get("HX-Request") == "true":
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/_ab_metrics.html",
            {"variants": [v.model_dump() for v in response.variants]},
        )
    return response
