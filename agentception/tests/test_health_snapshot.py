"""Tests for the HealthSnapshot Pydantic model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentception.models.health import HealthSnapshot


def test_health_snapshot_valid_construction() -> None:
    snap = HealthSnapshot(
        uptime_seconds=120.5,
        memory_rss_mb=64.0,
        active_worktree_count=3,
        github_api_latency_ms=42.7,
    )
    assert snap.uptime_seconds == 120.5
    assert snap.memory_rss_mb == 64.0
    assert snap.active_worktree_count == 3
    assert snap.github_api_latency_ms == 42.7


def test_health_snapshot_negative_uptime_rejected() -> None:
    with pytest.raises(ValidationError):
        HealthSnapshot(
            uptime_seconds=-1.0,
            memory_rss_mb=64.0,
            active_worktree_count=3,
            github_api_latency_ms=42.7,
        )


def test_health_snapshot_negative_memory_rejected() -> None:
    with pytest.raises(ValidationError):
        HealthSnapshot(
            uptime_seconds=10.0,
            memory_rss_mb=-1.0,
            active_worktree_count=3,
            github_api_latency_ms=42.7,
        )


def test_health_snapshot_negative_worktree_count_rejected() -> None:
    with pytest.raises(ValidationError):
        HealthSnapshot(
            uptime_seconds=10.0,
            memory_rss_mb=64.0,
            active_worktree_count=-1,
            github_api_latency_ms=42.7,
        )


def test_health_snapshot_negative_latency_allowed() -> None:
    """github_api_latency_ms=-1.0 is the sentinel for 'not yet probed'."""
    snap = HealthSnapshot(
        uptime_seconds=10.0,
        memory_rss_mb=64.0,
        active_worktree_count=0,
        github_api_latency_ms=-1.0,
    )
    assert snap.github_api_latency_ms == -1.0


def test_health_snapshot_json_roundtrip() -> None:
    snap = HealthSnapshot(
        uptime_seconds=30.0,
        memory_rss_mb=128.0,
        active_worktree_count=2,
        github_api_latency_ms=99.9,
    )
    restored = HealthSnapshot.model_validate_json(snap.model_dump_json())
    assert restored == snap


def test_health_snapshot_openapi_schema_has_descriptions() -> None:
    schema = HealthSnapshot.model_json_schema()
    props = schema["properties"]
    assert "description" in props["uptime_seconds"]
    assert "description" in props["memory_rss_mb"]
    assert "description" in props["active_worktree_count"]
    assert "description" in props["github_api_latency_ms"]
