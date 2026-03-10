from __future__ import annotations

"""Tests for GET /api/ping.

Covers:
  - Happy path: 200 OK with {"status": "ok"}
  - Response body is valid JSON matching PingResponse schema
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Return a synchronous test client for the full FastAPI app."""
    from agentception.app import app

    return TestClient(app)


class TestPingEndpoint:
    def test_ping_returns_200(self, client: TestClient) -> None:
        """GET /api/ping must respond with HTTP 200."""
        response = client.get("/api/ping")
        assert response.status_code == 200

    def test_ping_returns_status_ok(self, client: TestClient) -> None:
        """GET /api/ping body must be {"status": "ok"}."""
        response = client.get("/api/ping")
        assert response.json() == {"status": "ok"}

    def test_ping_content_type_is_json(self, client: TestClient) -> None:
        """GET /api/ping must return application/json."""
        response = client.get("/api/ping")
        assert "application/json" in response.headers["content-type"]
