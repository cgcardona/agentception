"""UI route: A/B role variant comparison dashboard."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.intelligence.ab_results import ABVariantResult, compute_ab_results
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ab-testing", response_class=HTMLResponse)
async def ab_testing_page(request: Request) -> HTMLResponse:
    """A/B role variant comparison dashboard — side-by-side outcome metrics.

    Renders the A/B results page (AC-505): two comparison cards showing
    PRs opened, merge rate, average reviewer grade, and batch count for each
    role variant.  A winner badge is shown when one variant's merge rate or
    average grade clearly outperforms the other.

    Data comes from :func:`~agentception.intelligence.ab_results.compute_ab_results`.
    On any computation error the page renders with zero-value results and an
    error banner rather than returning HTTP 500 so the UI stays accessible.
    """
    error: str | None = None
    variant_a: ABVariantResult | None = None
    variant_b: ABVariantResult | None = None
    try:
        variant_a, variant_b = await compute_ab_results()
    except Exception as exc:  # pragma: no cover — infrastructure error path
        error = f"Could not compute A/B results: {exc}"
        logger.warning("⚠️  A/B results computation failed: %s", exc)

    # Determine winner based on merge rate; fall back to no winner on a tie.
    winner: str | None = None
    if variant_a is not None and variant_b is not None:
        if variant_a.merge_rate > variant_b.merge_rate:
            winner = "A"
        elif variant_b.merge_rate > variant_a.merge_rate:
            winner = "B"

    return _TEMPLATES.TemplateResponse(
        request,
        "ab_testing.html",
        {
            "variant_a": variant_a,
            "variant_b": variant_b,
            "winner": winner,
            "error": error,
        },
    )
