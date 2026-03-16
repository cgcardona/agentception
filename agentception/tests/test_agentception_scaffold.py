from __future__ import annotations

"""Tests for the AgentCeption scaffold (AC-001).

These tests verify the foundational service plumbing — settings, models, and
the FastAPI app itself — before any reader or poller logic is wired in.

Run targeted:
    pytest agentception/tests/test_agentception_scaffold.py -v
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from agentception.app import app
from agentception.config import AgentCeptionSettings
from agentception.models import AgentNode, AgentStatus, AgentTaskSpec, PipelineState


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client that handles lifespan correctly."""
    with TestClient(app) as c:
        yield c


# ─── /health ───────────────────────────────────────────────────────────────────


def test_health_returns_200(client: TestClient) -> None:
    """GET /health must return 200 with ``{"status": "ok"}``."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_health_endpoint_returns_ok() -> None:
    """GET /health must return 200 with ``{"status": "ok"}`` (async test)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ─── Config ────────────────────────────────────────────────────────────────────


def test_settings_loads_defaults() -> None:
    """AgentCeptionSettings must load without errors and expose expected fields.

    We do not assert a specific ``worktrees_dir`` path because it is
    overridden by the ``WORKTREES_DIR`` env var in the container
    (set to ``/worktrees`` in docker-compose.override.yml).  We check only
    that the field is a ``Path`` instance and is non-empty.
    """
    s = AgentCeptionSettings()
    assert s.gh_repo == "cgcardona/agentception"
    assert s.poll_interval_seconds == 5
    assert s.github_cache_seconds == 4
    assert isinstance(s.worktrees_dir, __import__("pathlib").Path)
    assert str(s.worktrees_dir) != ""


# ─── Models ────────────────────────────────────────────────────────────────────


def test_agent_node_serializes_roundtrip() -> None:
    """AgentNode must survive a JSON serialise → deserialise roundtrip without data loss."""
    node = AgentNode(
        id="eng-20260301T000000Z-abcd",
        role="developer",
        status=AgentStatus.IMPLEMENTING,
        issue_number=609,
        branch="agent/issue-609",
        batch_id="eng-20260301T211956Z-741f",
        message_count=42,
    )
    restored = AgentNode.model_validate(node.model_dump())
    assert restored.id == node.id
    assert restored.status == AgentStatus.IMPLEMENTING
    assert restored.issue_number == 609
    assert restored.message_count == 42
    assert restored.children == []


def test_pipeline_state_empty_valid() -> None:
    """PipelineState with no agents and no alerts must be valid and serialisable."""
    import time

    state = PipelineState(
        active_label="batch-01",
        issues_open=0,
        prs_open=0,
        agents=[],
        alerts=[],
        polled_at=time.time(),
    )
    assert state.issues_open == 0
    assert state.agents == []
    data = state.model_dump()
    assert "polled_at" in data


# ─── UI ────────────────────────────────────────────────────────────────────────


def test_index_returns_html_with_agentception(client: TestClient) -> None:
    """GET / must return 200 HTML containing the string 'Agentception'."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Agentception" in response.text


# ─── AgentTaskSpec ─────────────────────────────────────────────────────────────


def test_agent_task_spec_parses_known_fields() -> None:
    """AgentTaskSpec must parse a representative DB-backed payload correctly."""
    spec = AgentTaskSpec(
        task="issue-to-pr",
        gh_repo="cgcardona/agentception",
        issue_number=609,
        branch="agent/issue-609",
        role="developer",
        batch_id="eng-20260301T211956Z-741f",
        spawn_sub_agents=False,
        attempt_n=0,
    )
    assert spec.issue_number == 609
    assert spec.spawn_sub_agents is False
    assert spec.attempt_n == 0
