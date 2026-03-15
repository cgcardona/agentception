"""Tests for GET/POST /api/system/* routes.

All heavy I/O (Qdrant, fastembed) is mocked so the test suite stays fast
and does not need external services.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.services.code_indexer import IndexStats, SearchMatch


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ── POST /api/system/index-codebase ──────────────────────────────────────────


def test_trigger_index_codebase_returns_202(client: TestClient) -> None:
    with patch(
        "agentception.routes.api.system.index_codebase",
        new_callable=AsyncMock,
        return_value=IndexStats(ok=True, files_indexed=10, chunks_indexed=50, files_skipped=0, error=None),
    ):
        resp = client.post("/api/system/index-codebase")

    assert resp.status_code == 202
    body = resp.json()
    assert body["ok"] is True
    assert "background" in body["message"].lower()


def test_trigger_index_codebase_schedules_background_task(client: TestClient) -> None:
    """The endpoint must return immediately (202) and not wait for indexing."""
    call_count = 0

    async def slow_index(**_: str | int | bool | float | None) -> IndexStats:
        nonlocal call_count
        call_count += 1
        # In real use this would take seconds; in test just record the call.
        return IndexStats(ok=True, files_indexed=1, chunks_indexed=1, files_skipped=0, error=None)

    with patch("agentception.routes.api.system.index_codebase", side_effect=slow_index):
        resp = client.post("/api/system/index-codebase")

    assert resp.status_code == 202


# ── GET /api/system/search ────────────────────────────────────────────────────


def test_semantic_search_returns_matches(client: TestClient) -> None:
    fake_matches: list[SearchMatch] = [
        SearchMatch(
            file="agentception/config.py",
            chunk="qdrant_url: str = ...",
            score=0.95,
            start_line=100,
            end_line=110,
        )
    ]

    with patch(
        "agentception.routes.api.system.search_codebase",
        new_callable=AsyncMock,
        return_value=fake_matches,
    ):
        resp = client.get("/api/system/search?q=qdrant+url&n=3")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["n_results"] == 1
    match = body["matches"][0]
    assert match["file"] == "agentception/config.py"
    assert abs(match["score"] - 0.95) < 0.001
    assert match["start_line"] == 100
    assert match["end_line"] == 110


def test_semantic_search_empty_results(client: TestClient) -> None:
    with patch(
        "agentception.routes.api.system.search_codebase",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.get("/api/system/search?q=nothing+here")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["n_results"] == 0
    assert body["matches"] == []


def test_semantic_search_requires_query_param(client: TestClient) -> None:
    resp = client.get("/api/system/search")
    assert resp.status_code == 422


def test_semantic_search_n_bounds_validated(client: TestClient) -> None:
    """n must be 1–20; values outside that range are rejected."""
    with patch(
        "agentception.routes.api.system.search_codebase",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp_zero = client.get("/api/system/search?q=test&n=0")
        resp_huge = client.get("/api/system/search?q=test&n=100")

    assert resp_zero.status_code == 422
    assert resp_huge.status_code == 422
