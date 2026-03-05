from __future__ import annotations

"""Tests for the agents page live polling additions (issue #739).

Verifies:
  GET /partials/agents → 200 with text/html content-type
  GET /partials/agents → returns an HTML fragment (no <html> root element)
  GET /agents          → page HTML contains hx-trigger with "every" (polling enabled)

Run targeted:
    pytest agentception/tests/test_agents_page.py -v
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from agentception.app import app


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client that handles lifespan correctly."""
    with TestClient(app) as c:
        yield c


def test_agents_partial_returns_200(client: TestClient) -> None:
    """GET /partials/agents must return HTTP 200."""
    response = client.get("/partials/agents")
    assert response.status_code == 200


def test_agents_partial_content_type_is_html(client: TestClient) -> None:
    """GET /partials/agents must respond with a text/html content-type."""
    response = client.get("/partials/agents")
    assert "text/html" in response.headers["content-type"]


def test_agents_partial_returns_html_fragment(client: TestClient) -> None:
    """GET /partials/agents must return a partial fragment — no <html> root element.

    A full page would contain an <html> tag from the base layout.
    A fragment must not, so HTMX can safely inject it into an existing DOM node.
    """
    response = client.get("/partials/agents")
    assert response.status_code == 200
    assert "<html" not in response.text


def test_agents_page_has_hx_polling(client: TestClient) -> None:
    """GET /agents must include hx-trigger with an 'every' interval.

    This confirms that the live polling div is present in the full page render
    so HTMX will start polling /partials/agents on page load.
    """
    response = client.get("/agents")
    assert response.status_code == 200
    assert "hx-trigger" in response.text
    assert "every" in response.text
