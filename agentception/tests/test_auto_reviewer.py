"""Tests for agentception.services.auto_reviewer."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.services.auto_reviewer import (
    _PR_URL_RE,
    auto_dispatch_reviewer,
)


# ---------------------------------------------------------------------------
# PR URL parsing
# ---------------------------------------------------------------------------


def test_pr_url_re_parses_standard_url() -> None:
    m = _PR_URL_RE.search("https://github.com/owner/repo/pull/537")
    assert m is not None
    assert m.group(1) == "537"


def test_pr_url_re_parses_url_with_trailing_path() -> None:
    m = _PR_URL_RE.search("https://github.com/owner/repo/pull/100/files")
    assert m is not None
    assert m.group(1) == "100"


def test_pr_url_re_returns_none_for_invalid_url() -> None:
    assert _PR_URL_RE.search("https://github.com/owner/repo/issues/42") is None


# ---------------------------------------------------------------------------
# auto_dispatch_reviewer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_auto_dispatch_reviewer_posts_correct_payload() -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with (
        patch("agentception.services.auto_reviewer.asyncio.sleep", new_callable=AsyncMock),
        patch("agentception.services.auto_reviewer.httpx.AsyncClient", return_value=mock_client),
        patch("agentception.services.auto_reviewer.settings") as mock_settings,
    ):
        mock_settings.gh_repo = "owner/repo"
        mock_settings.ac_api_key = "test-key"

        await auto_dispatch_reviewer(
            issue_number=42,
            pr_url="https://github.com/owner/repo/pull/537",
        )

    mock_client.post.assert_awaited_once()
    _, kwargs = mock_client.post.call_args
    payload = kwargs["json"]
    assert payload["issue_number"] == 42
    assert payload["pr_number"] == 537
    assert payload["pr_branch"] == "agent/issue-42"
    assert payload["role"] == "reviewer"
    assert kwargs["headers"]["X-API-Key"] == "test-key"


@pytest.mark.anyio
async def test_auto_dispatch_reviewer_uses_explicit_branch() -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with (
        patch("agentception.services.auto_reviewer.asyncio.sleep", new_callable=AsyncMock),
        patch("agentception.services.auto_reviewer.httpx.AsyncClient", return_value=mock_client),
        patch("agentception.services.auto_reviewer.settings") as mock_settings,
    ):
        mock_settings.gh_repo = "owner/repo"
        mock_settings.ac_api_key = "test-key"

        await auto_dispatch_reviewer(
            issue_number=42,
            pr_url="https://github.com/owner/repo/pull/537",
            pr_branch="custom/branch-name",
        )

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["pr_branch"] == "custom/branch-name"


@pytest.mark.anyio
async def test_auto_dispatch_reviewer_swallows_bad_pr_url() -> None:
    """No HTTP call should be made when the PR URL cannot be parsed."""
    with patch("agentception.services.auto_reviewer.httpx.AsyncClient") as mock_cls:
        await auto_dispatch_reviewer(
            issue_number=42,
            pr_url="https://github.com/owner/repo/issues/42",
        )
    mock_cls.assert_not_called()


@pytest.mark.anyio
async def test_auto_dispatch_reviewer_swallows_http_error() -> None:
    """HTTP failures must not propagate — the implementer's run is already done."""
    import httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Server error",
            request=MagicMock(),
            response=MagicMock(status_code=500, text="Internal Server Error"),
        )
    )

    with (
        patch("agentception.services.auto_reviewer.asyncio.sleep", new_callable=AsyncMock),
        patch("agentception.services.auto_reviewer.httpx.AsyncClient", return_value=mock_client),
        patch("agentception.services.auto_reviewer.settings") as mock_settings,
    ):
        mock_settings.gh_repo = "owner/repo"
        mock_settings.ac_api_key = "test-key"

        # Must not raise
        await auto_dispatch_reviewer(
            issue_number=42,
            pr_url="https://github.com/owner/repo/pull/537",
        )
