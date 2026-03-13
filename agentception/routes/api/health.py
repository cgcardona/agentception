"""API route: GET /api/health/detailed — detailed system health snapshot.

Thin handler — all collection logic lives in ``agentception.services.health_collector``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from agentception.models.health import HealthSnapshot
from agentception.services import health_collector

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health/detailed", response_model=HealthSnapshot, tags=["health"])
async def get_health_detailed() -> HealthSnapshot:
    """Return a point-in-time system health snapshot.

    Collects uptime, memory RSS, active worktree count, and GitHub API latency.
    Always returns 200 — individual metrics signal failure through sentinel values
    (e.g. ``github_api_latency_ms: -1.0`` when the probe fails).
    """
    return await health_collector.collect()
