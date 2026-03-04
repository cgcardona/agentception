from __future__ import annotations

"""UI route: GET /ui/health/widget — HTMX-polled System Health card fragment.

Calls ``health_collector.collect()`` and renders ``partials/health_card.html``
with the four HealthSnapshot metrics and a derived ``is_nominal`` flag.

Thresholds (from issue #941):
  nominal   — github_api_latency_ms < 500 AND memory_rss_mb < 512
  elevated  — any threshold exceeded (or probe failed, i.e. latency == -1.0)
"""

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.services import health_collector

logger = logging.getLogger(__name__)

router = APIRouter()

_LATENCY_THRESHOLD_MS: float = 500.0
_MEMORY_THRESHOLD_MB: float = 512.0


def _fmt_uptime(seconds: float) -> str:
    """Format an uptime duration in seconds as a compact human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


@router.get("/ui/health/widget", response_class=HTMLResponse, tags=["ui", "health"])
async def health_widget(request: Request) -> HTMLResponse:
    """Return the System Health card as an HTMX-ready HTML fragment.

    The overview dashboard polls this endpoint every 10 seconds via
    ``hx-get="/ui/health/widget" hx-trigger="load, every 10s" hx-swap="innerHTML"``.

    The ``is_nominal`` flag controls the status-dot BEM modifier:
    - ``.health-status-dot--nominal`` (green) when latency < 500 ms AND memory < 512 MB.
    - ``.health-status-dot--elevated`` (yellow) otherwise, including when the
      GitHub probe has not yet run (latency == -1.0).
    """
    from ._shared import _TEMPLATES

    snapshot = await health_collector.collect()

    latency_ok = 0.0 <= snapshot.github_api_latency_ms < _LATENCY_THRESHOLD_MS
    memory_ok = snapshot.memory_rss_mb < _MEMORY_THRESHOLD_MB
    is_nominal = latency_ok and memory_ok

    logger.debug(
        "⚙️ health widget: uptime=%.1fs mem=%.1fMB latency=%.1fms nominal=%s",
        snapshot.uptime_seconds,
        snapshot.memory_rss_mb,
        snapshot.github_api_latency_ms,
        is_nominal,
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/health_card.html",
        {
            "snapshot": snapshot,
            "is_nominal": is_nominal,
            "uptime_str": _fmt_uptime(snapshot.uptime_seconds),
        },
    )
