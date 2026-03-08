"""Tests for agentception.middleware.auth — API key authentication.

Covers:
  - Auth disabled (empty AC_API_KEY): all requests pass
  - Auth enabled: valid Bearer token → 200
  - Auth enabled: valid X-API-Key header → 200
  - Auth enabled: missing auth header → 401
  - Auth enabled: wrong key → 401
  - Auth enabled: non-/api/ paths bypass auth (UI, /health, /mcp)
  - _extract_key helper handles malformed Authorization header
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app

_TEST_KEY = "test-secret-key-abc123"


@pytest.fixture()
def client() -> TestClient:
    """TestClient with auth disabled (default — empty AC_API_KEY)."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def authed_client() -> TestClient:
    """TestClient used alongside a patched AC_API_KEY."""
    return TestClient(app, raise_server_exceptions=False)


# ── Auth disabled (default) ───────────────────────────────────────────────────


def test_auth_disabled_api_requests_pass(client: TestClient) -> None:
    """When AC_API_KEY is empty every /api/ request passes without a header."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = ""
        resp = client.get("/api/health/detailed")
    # May 200 or 500 depending on live services — the point is it is NOT 401.
    assert resp.status_code != 401


def test_auth_disabled_health_passes(client: TestClient) -> None:
    """Non-API paths always pass regardless of auth config."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = ""
        resp = client.get("/health")
    assert resp.status_code == 200


# ── Auth enabled ──────────────────────────────────────────────────────────────


def test_bearer_token_accepted(authed_client: TestClient) -> None:
    """Valid 'Authorization: Bearer <key>' lets the request through."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = _TEST_KEY
        resp = authed_client.get(
            "/api/health/detailed",
            headers={"Authorization": f"Bearer {_TEST_KEY}"},
        )
    assert resp.status_code != 401


def test_x_api_key_header_accepted(authed_client: TestClient) -> None:
    """Valid 'X-API-Key: <key>' header lets the request through."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = _TEST_KEY
        resp = authed_client.get(
            "/api/health/detailed",
            headers={"X-API-Key": _TEST_KEY},
        )
    assert resp.status_code != 401


def test_missing_auth_header_returns_401(authed_client: TestClient) -> None:
    """Requests to /api/ without any auth header receive 401."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = _TEST_KEY
        resp = authed_client.get("/api/health/detailed")
    assert resp.status_code == 401
    body = resp.json()
    assert "detail" in body
    assert "Bearer" in body["detail"]


def test_wrong_key_returns_401(authed_client: TestClient) -> None:
    """A request with the wrong API key receives 401."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = _TEST_KEY
        resp = authed_client.get(
            "/api/health/detailed",
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert resp.status_code == 401


# ── Non-/api/ paths bypass auth ───────────────────────────────────────────────


def test_health_endpoint_bypasses_auth(authed_client: TestClient) -> None:
    """/health is not under /api/ — auth is never checked."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = _TEST_KEY
        resp = authed_client.get("/health")
    # Must respond with 200 even without an API key.
    assert resp.status_code == 200


def test_ui_root_bypasses_auth(authed_client: TestClient) -> None:
    """The UI dashboard (GET /) is not guarded by the API key."""
    with patch("agentception.middleware.auth.settings") as mock_settings:
        mock_settings.ac_api_key = _TEST_KEY
        resp = authed_client.get("/")
    assert resp.status_code != 401


# ── _extract_key unit tests ───────────────────────────────────────────────────


def test_extract_key_from_bearer_header() -> None:
    """_extract_key returns the token from 'Authorization: Bearer <token>'."""
    from unittest.mock import MagicMock

    from agentception.middleware.auth import _extract_key

    request = MagicMock()
    request.headers = {"Authorization": "Bearer my-secret"}
    assert _extract_key(request) == "my-secret"


def test_extract_key_from_x_api_key() -> None:
    """_extract_key returns the value of 'X-API-Key: <key>'."""
    from unittest.mock import MagicMock

    from agentception.middleware.auth import _extract_key

    request = MagicMock()
    request.headers = {"X-API-Key": "another-secret"}
    assert _extract_key(request) == "another-secret"


def test_extract_key_prefers_bearer_over_x_api_key() -> None:
    """When both headers are present, Bearer takes precedence."""
    from unittest.mock import MagicMock

    from agentception.middleware.auth import _extract_key

    request = MagicMock()
    request.headers = {
        "Authorization": "Bearer bearer-key",
        "X-API-Key": "xkey",
    }
    assert _extract_key(request) == "bearer-key"


def test_extract_key_returns_empty_when_no_headers() -> None:
    """_extract_key returns '' when no recognized auth header is present."""
    from unittest.mock import MagicMock

    from agentception.middleware.auth import _extract_key

    request = MagicMock()
    request.headers = {}
    assert _extract_key(request) == ""


def test_extract_key_ignores_non_bearer_authorization() -> None:
    """'Authorization: Basic ...' does not match Bearer — falls back to X-API-Key."""
    from unittest.mock import MagicMock

    from agentception.middleware.auth import _extract_key

    request = MagicMock()
    request.headers = {"Authorization": "Basic dXNlcjpwYXNz", "X-API-Key": "fallback"}
    assert _extract_key(request) == "fallback"
