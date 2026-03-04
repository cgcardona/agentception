"""Tests for the AgentCeption settings page and auth-check partial.

Covers:
- GET /settings → 200, renders page with "Settings" heading
- GET /settings → page contains GitHub Connection and Pipeline Config sections
- GET /partials/settings/gh-check → 200, returns Authenticated or Not authenticated badge

Run targeted:
    pytest agentception/tests/test_settings_page.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app

client = TestClient(app)


def test_settings_page_renders() -> None:
    """GET /settings returns 200 and contains the page title."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text


def test_settings_page_shows_github_section() -> None:
    """GET /settings renders the GitHub Connection section."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "GitHub" in resp.text or "gh_repo" in resp.text or "cgcardona" in resp.text


def test_settings_page_shows_pipeline_config_section() -> None:
    """GET /settings renders the Pipeline Config section."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Pipeline Config" in resp.text


def test_gh_check_partial_when_auth_ok() -> None:
    """GET /partials/settings/gh-check → badge says Authenticated when gh returns 0."""
    with patch(
        "agentception.routes.ui.settings._check_gh_auth",
        new_callable=AsyncMock,
        return_value=True,
    ):
        resp = client.get("/partials/settings/gh-check")
    assert resp.status_code == 200
    assert "Authenticated" in resp.text


def test_gh_check_partial_when_auth_fails() -> None:
    """GET /partials/settings/gh-check → badge says Not authenticated when gh returns non-0."""
    with patch(
        "agentception.routes.ui.settings._check_gh_auth",
        new_callable=AsyncMock,
        return_value=False,
    ):
        resp = client.get("/partials/settings/gh-check")
    assert resp.status_code == 200
    assert "Not authenticated" in resp.text
