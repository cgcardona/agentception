from __future__ import annotations

"""Tests for agentception/readers/github.py — cache TTL behaviour."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import agentception.readers.github as gh_module
from agentception.readers.github import _cache_invalidate
from agentception.types import JsonValue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(items: list[JsonValue], status: int = 200) -> httpx.Response:
    """Build a minimal httpx.Response carrying a JSON list payload."""
    import json

    request = httpx.Request("GET", "https://api.github.com/test")
    return httpx.Response(
        status_code=status,
        content=json.dumps(items).encode(),
        headers={"content-type": "application/json"},
        request=request,
    )


# ---------------------------------------------------------------------------
# test_api_get_all_cache_expires_before_poll
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_api_get_all_cache_expires_before_poll() -> None:
    """_api_get_all makes a fresh HTTP call after the TTL has elapsed.

    Scenario:
    1. First call — cache miss → HTTP request made, result cached.
    2. Cache entry back-dated to already-expired timestamp.
    3. Second call — cache miss again → second HTTP request made.

    This verifies that github_cache_seconds < poll_interval_seconds so every
    poller tick receives live data rather than a stale cached response.
    """
    from agentception.config import settings

    # Confirm the invariant: TTL must be strictly less than the poll interval.
    assert settings.github_cache_seconds < settings.poll_interval_seconds, (
        f"github_cache_seconds ({settings.github_cache_seconds}) must be "
        f"< poll_interval_seconds ({settings.poll_interval_seconds})"
    )

    fake_items: list[JsonValue] = [{"number": 1, "title": "test issue"}]
    response = _make_response(fake_items)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=response)

    # Clear any pre-existing cache state.
    _cache_invalidate()

    with (
        patch("agentception.readers.github.settings") as mock_settings,
        patch("agentception.readers.github._headers", return_value={"Authorization": "Bearer test"}),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.github_cache_seconds = settings.github_cache_seconds
        mock_settings.gh_repo = "owner/repo"

        # ── First call: cache miss → HTTP request ──────────────────────────
        result1 = await gh_module._api_get_all(
            "repos/owner/repo/issues",
            {"state": "open"},
            "test_cache_expiry_key",
        )
        assert result1 == fake_items
        assert mock_client.get.call_count == 1

        # ── Expire the cache entry by back-dating its expiry timestamp ─────
        # Set expires_at to a time already in the past so _cache_get (which
        # calls the real time.monotonic()) sees the entry as expired.
        cache_key = "test_cache_expiry_key"
        if cache_key in gh_module._cache:
            value, _old_expires = gh_module._cache[cache_key]
            gh_module._cache[cache_key] = (value, time.monotonic() - 1.0)

        # ── Second call: cache miss (expired) → second HTTP request ────────
        result2 = await gh_module._api_get_all(
            "repos/owner/repo/issues",
            {"state": "open"},
            "test_cache_expiry_key",
        )
        assert result2 == fake_items
        assert mock_client.get.call_count == 2, (
            "Expected a second HTTP call after TTL expiry, "
            f"but got {mock_client.get.call_count} total calls"
        )

    # Clean up.
    _cache_invalidate()


# ---------------------------------------------------------------------------
# test_get_closed_issues_default_limit
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_closed_issues_default_limit() -> None:
    """get_closed_issues() with no args returns all 150 items from the API.

    The old limit=100 default would have truncated the result to 100.  With
    limit=1000 all 150 items pass through untruncated.

    Scale assumption: this test validates the 100→1000 change; it would need
    updating if the default is raised further or made configurable.
    """
    import json as _json

    from agentception.readers.github import get_closed_issues

    # Build 150 fake closed issues (no pull_request key → all pass the filter).
    fake_issues: list[JsonValue] = [
        {"number": i, "title": f"issue {i}", "state": "closed"}
        for i in range(1, 151)
    ]

    # _api_get_all paginates with per_page=100, so we need two pages.
    page1 = fake_issues[:100]
    page2 = fake_issues[100:]

    def _make_page(items: list[JsonValue]) -> httpx.Response:
        request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues")
        return httpx.Response(
            status_code=200,
            content=_json.dumps(items).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    responses = [_make_page(page1), _make_page(page2), _make_page([])]
    call_index = 0

    async def _fake_get(*args: str | int | float | bool | None, **kwargs: str | int | float | bool | None) -> httpx.Response:
        nonlocal call_index
        resp = responses[min(call_index, len(responses) - 1)]
        call_index += 1
        return resp

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = _fake_get

    _cache_invalidate()

    with (
        patch("agentception.readers.github.settings") as mock_settings,
        patch("agentception.readers.github._headers", return_value={"Authorization": "Bearer test"}),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.github_cache_seconds = 30
        mock_settings.gh_repo = "owner/repo"

        result = await get_closed_issues()

    assert len(result) == 150, (
        f"Expected 150 issues (old limit=100 would have truncated), got {len(result)}"
    )

    _cache_invalidate()

