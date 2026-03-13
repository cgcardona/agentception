"""HealthSnapshot — canonical shape for AgentCeption system health data.

Every downstream component (health_collector, /api/health/detailed endpoint,
dashboard poller, and tests) imports from here. Change this contract carefully.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HealthSnapshot(BaseModel):
    """Point-in-time snapshot of AgentCeption system health metrics.

    Returned by the health collector and serialised as JSON by the
    /api/health/detailed endpoint. Field descriptions appear verbatim
    in the auto-generated OpenAPI schema.
    """

    uptime_seconds: float = Field(
        ...,
        description="Seconds elapsed since the AgentCeption process started.",
        ge=0.0,
    )
    memory_rss_mb: float = Field(
        ...,
        description="Resident Set Size of the AgentCeption process in megabytes.",
        ge=0.0,
    )
    active_worktree_count: int = Field(
        ...,
        description="Number of active git worktrees currently checked out under the worktrees base directory.",
        ge=0,
    )
    github_api_latency_ms: float = Field(
        ...,
        description="Round-trip latency of the most recent GitHub API probe in milliseconds. -1.0 if the probe has not yet run or failed.",
    )
