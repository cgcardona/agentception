"""Developer Run Health API — aggregate KPIs for completed developer runs.

Read-only endpoint that surfaces pass rate, iteration count, token usage,
cost estimates, duration, and grade distribution for developer runs.  Runs
are grouped by ``prompt_variant`` (NULL = baseline).

When the request includes the HTMX header ``HX-Request: true``, returns an
HTML partial for in-place swap; otherwise returns JSON.
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

# Anthropic pricing per million tokens (claude-sonnet-4-x tier).
_PRICE_INPUT        = 3.00   # $/M input tokens
_PRICE_OUTPUT       = 15.00  # $/M output tokens
_PRICE_CACHE_WRITE  = 3.75   # $/M cache-write tokens
_PRICE_CACHE_READ   = 0.30   # $/M cache-read tokens


class DevHealthMetrics(BaseModel):
    """Per-variant aggregate KPIs for completed developer runs."""

    model_config = ConfigDict(frozen=True)

    variant: str
    role: str
    runs: int
    avg_iterations: float
    avg_input_tokens: float
    avg_output_tokens: float
    avg_cache_read_tokens: float
    avg_cache_write_tokens: float
    total_tokens: int
    avg_duration_secs: float
    retry_count: int
    pass_rate: float
    passed: int
    failed: int
    grade_a: int
    grade_b: int
    grade_c: int
    grade_d: int
    grade_f: int
    # Computed in Python from token averages.
    estimated_cost_per_run: float
    retry_rate: float


class DevHealthResponse(BaseModel):
    """Response shape for GET /api/metrics/ab."""

    model_config = ConfigDict(frozen=True)

    variants: list[DevHealthMetrics]


_AB_QUERY = text("""
SELECT
  COALESCE(r.prompt_variant, 'control') AS variant,
  r.role,
  COUNT(DISTINCT r.id)::int AS runs,
  COALESCE(AVG(iter.cnt), 0)::float AS avg_iterations,
  COALESCE(AVG(r.total_input_tokens), 0)::float AS avg_input_tokens,
  COALESCE(AVG(r.total_output_tokens), 0)::float AS avg_output_tokens,
  COALESCE(AVG(r.total_cache_read_tokens), 0)::float AS avg_cache_read_tokens,
  COALESCE(AVG(r.total_cache_write_tokens), 0)::float AS avg_cache_write_tokens,
  COALESCE(SUM(r.total_input_tokens + r.total_output_tokens), 0)::int AS total_tokens,
  COALESCE(AVG(EXTRACT(EPOCH FROM (r.completed_at - r.spawned_at))), 0)::float AS avg_duration_secs,
  COUNT(CASE WHEN r.attempt_number > 0 THEN 1 END)::int AS retry_count,
  COALESCE(AVG(CASE WHEN lr.grade IN ('A','B') THEN 1.0 ELSE 0.0 END), 0)::float AS pass_rate,
  COUNT(CASE WHEN lr.grade IN ('A','B') THEN 1 END)::int AS passed,
  COUNT(CASE WHEN lr.grade IN ('C','D','F') THEN 1 END)::int AS failed,
  COUNT(CASE WHEN lr.grade = 'A' THEN 1 END)::int AS grade_a,
  COUNT(CASE WHEN lr.grade = 'B' THEN 1 END)::int AS grade_b,
  COUNT(CASE WHEN lr.grade = 'C' THEN 1 END)::int AS grade_c,
  COUNT(CASE WHEN lr.grade = 'D' THEN 1 END)::int AS grade_d,
  COUNT(CASE WHEN lr.grade = 'F' THEN 1 END)::int AS grade_f
FROM agent_runs r
LEFT JOIN (
  SELECT agent_run_id, COUNT(*) AS cnt
  FROM agent_events
  WHERE event_type = 'step_start'
  GROUP BY agent_run_id
) iter ON iter.agent_run_id = r.id
-- Pull the most-recent reviewer grade for each issue so developer runs
-- can be evaluated against the outcome of their code review.
LEFT JOIN (
  SELECT DISTINCT ON (rev.issue_number)
    rev.issue_number,
    ev.payload::json->>'grade' AS grade
  FROM agent_runs rev
  JOIN agent_events ev ON ev.agent_run_id = rev.id AND ev.event_type = 'done'
  WHERE rev.role = 'reviewer'
  ORDER BY rev.issue_number, rev.spawned_at DESC
) lr ON lr.issue_number = r.issue_number AND r.issue_number IS NOT NULL
WHERE r.role = 'developer'
  AND r.status = 'completed'
  AND r.spawned_at > NOW() - (CAST(:days AS integer) * INTERVAL '1 day')
GROUP BY COALESCE(r.prompt_variant, 'control'), r.role
ORDER BY 1, 2
""")


def _compute_cost(row: DevHealthMetrics) -> float:
    """Estimate average cost per run using Anthropic token pricing."""
    return (
        row.avg_input_tokens       / 1_000_000 * _PRICE_INPUT
        + row.avg_output_tokens    / 1_000_000 * _PRICE_OUTPUT
        + row.avg_cache_write_tokens / 1_000_000 * _PRICE_CACHE_WRITE
        + row.avg_cache_read_tokens  / 1_000_000 * _PRICE_CACHE_READ
    )


@router.get("/metrics/ab", response_model=None)
async def get_metrics_ab(
    request: Request,
    days: int = Query(default=7, ge=1, le=90, description="Lookback window in days (1–90)."),
) -> DevHealthResponse | Response:
    """Return developer run health KPIs aggregated by prompt variant.

    Grades are sourced from the most-recent reviewer run for each issue,
    joined back to the originating developer run via ``issue_number``.
    Runs with ``prompt_variant IS NULL`` appear as the ``control`` bucket.

    When the request includes the ``HX-Request: true`` header (HTMX), returns
    HTML (partials/_ab_metrics.html) for in-place swap; otherwise returns JSON.
    """
    try:
        async with get_session() as session:
            result = await session.execute(_AB_QUERY, {"days": days})
            rows = result.mappings().all()
    except Exception as exc:
        logger.warning("⚠️ get_metrics_ab DB query failed (non-fatal): %s", exc)
        response = DevHealthResponse(variants=[])
    else:
        variants: list[DevHealthMetrics] = []
        for row in rows:
            runs = int(row["runs"])
            retry_count = int(row["retry_count"])
            partial = DevHealthMetrics(
                variant=str(row["variant"]),
                role=str(row["role"]),
                runs=runs,
                avg_iterations=float(row["avg_iterations"]),
                avg_input_tokens=float(row["avg_input_tokens"]),
                avg_output_tokens=float(row["avg_output_tokens"]),
                avg_cache_read_tokens=float(row["avg_cache_read_tokens"]),
                avg_cache_write_tokens=float(row["avg_cache_write_tokens"]),
                total_tokens=int(row["total_tokens"]),
                avg_duration_secs=float(row["avg_duration_secs"]),
                retry_count=retry_count,
                pass_rate=float(row["pass_rate"]),
                passed=int(row["passed"]),
                failed=int(row["failed"]),
                grade_a=int(row["grade_a"]),
                grade_b=int(row["grade_b"]),
                grade_c=int(row["grade_c"]),
                grade_d=int(row["grade_d"]),
                grade_f=int(row["grade_f"]),
                estimated_cost_per_run=0.0,   # placeholder; computed below
                retry_rate=0.0,               # placeholder; computed below
            )
            cost = _compute_cost(partial)
            retry_rate = retry_count / runs if runs else 0.0
            variants.append(
                partial.model_copy(
                    update={"estimated_cost_per_run": cost, "retry_rate": retry_rate}
                )
            )
        response = DevHealthResponse(variants=variants)

    if request.headers.get("HX-Request") == "true":
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/_ab_metrics.html",
            {"variants": [v.model_dump() for v in response.variants]},
        )
    return response
