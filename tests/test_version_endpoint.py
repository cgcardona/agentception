from __future__ import annotations

"""Tests for GET /api/version.

Covers the happy path (200 OK, valid body) and verifies the version field
looks like a semver-style string so we catch obvious packaging regressions.
"""

import re

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture()
def client() -> TestClient:
    """Synchronous test client — no DB or background tasks needed."""
    return TestClient(app, raise_server_exceptions=True)


def test_get_version_returns_200(client: TestClient) -> None:
    """GET /api/version must return HTTP 200."""
    response = client.get("/api/version")
    assert response.status_code == 200


def test_get_version_body_is_valid_version_response(client: TestClient) -> None:
    """GET /api/version must return a JSON body with a ``version`` string field."""
    response = client.get("/api/version")
    body = response.json()
    assert "version" in body
    assert isinstance(body["version"], str)
    assert body["version"]  # non-empty


def test_get_version_version_field_is_semver_like(client: TestClient) -> None:
    """The ``version`` field must look like a semver string (e.g. '1.2.3' or '0.0.0')."""
    response = client.get("/api/version")
    version = response.json()["version"]
    # Accept N.N.N with optional pre-release/build suffixes (PEP 440 subset).
    assert re.match(r"^\d+\.\d+\.\d+", version), (
        f"version {version!r} does not start with MAJOR.MINOR.PATCH"
    )
