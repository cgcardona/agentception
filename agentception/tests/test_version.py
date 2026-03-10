from __future__ import annotations

from fastapi.testclient import TestClient

from agentception.app import app

client = TestClient(app)


def test_get_version_returns_200() -> None:
    response = client.get("/api/version")
    assert response.status_code == 200


def test_get_version_body_has_version_key() -> None:
    response = client.get("/api/version")
    data = response.json()
    assert "version" in data
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0
