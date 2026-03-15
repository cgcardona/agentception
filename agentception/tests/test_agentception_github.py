from __future__ import annotations

"""Tests for agentception/readers/github.py.

All GitHub interactions are mocked via ``unittest.mock`` patching of
``httpx.AsyncClient`` — no real HTTP requests are ever made.  Each test
controls the status code and JSON body returned by the mock client.

Run targeted:
    pytest agentception/tests/test_agentception_github.py -v
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agentception.readers.github as gh_module
from agentception.types import JsonValue
from agentception.readers.github import (
    _cache,
    _cache_invalidate,
    _api_get,
    add_label_to_issue,
    add_wip_label,
    approve_pr,
    clear_wip_label,
    close_pr,
    get_active_label,
    get_issue,
    get_issue_body,
    get_open_issues,
    get_open_prs,
    get_wip_issues,
    merge_pr,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _mock_response(payload: JsonValue, status_code: int = 200) -> MagicMock:
    """Build a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = ""
    if status_code >= 400:
        from httpx import HTTPStatusError, Request, Response
        # raise_for_status raises on 4xx/5xx
        def _raise() -> None:
            raise HTTPStatusError(
                f"HTTP {status_code}",
                request=MagicMock(spec=Request),
                response=MagicMock(spec=Response, status_code=status_code, text="error"),
            )
        resp.raise_for_status = _raise
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _mock_client(
    *,
    get: JsonValue | None = None,
    post: JsonValue | None = None,
    patch: JsonValue | None = None,
    put: JsonValue | None = None,
    delete: JsonValue | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Build a mock httpx.AsyncClient context manager.

    Pass a ``payload`` to ``get``/``post``/``patch``/``put``/``delete`` to
    control what the matching HTTP method returns.  Unset methods are stubbed
    to return an empty 200.
    """
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    def _resp(payload: JsonValue, code: int = status_code) -> MagicMock:
        return _mock_response(payload, code)

    client.get = AsyncMock(return_value=_resp(get if get is not None else []))
    client.post = AsyncMock(return_value=_resp(post if post is not None else {}))
    client.patch = AsyncMock(return_value=_resp(patch if patch is not None else {}))
    client.put = AsyncMock(return_value=_resp(put if put is not None else {}))
    client.delete = AsyncMock(return_value=_resp(delete if delete is not None else None, 204))
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_cache_before_each() -> None:
    """Ensure a clean cache state for every test."""
    _cache.clear()


# ---------------------------------------------------------------------------
# _api_get — caching behaviour
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_hit_skips_http_call() -> None:
    """A second _api_get with the same cache_key must NOT make another HTTP call."""
    payload: JsonValue = [{"number": 1, "title": "Example"}]
    mock = _mock_client(get=payload)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        r1 = await _api_get("repos/x/y/issues", {}, "test_key")
        r2 = await _api_get("repos/x/y/issues", {}, "test_key")

    # AsyncClient was constructed only once.
    assert mock.get.call_count == 1
    assert r1 == payload
    assert r2 == payload


@pytest.mark.anyio
async def test_cache_invalidated_after_write() -> None:
    """Write operations must empty the cache so the next read is fresh."""
    _cache["stale_key"] = ("stale_value", time.monotonic() + 30)

    # close_pr makes two writes (post comment + patch PR) — both use separate
    # httpx.AsyncClient instances.  Supply two mock clients via side_effect.
    mock1 = _mock_client(post={"html_url": "https://github.com/x"})
    mock2 = _mock_client()  # patch response

    with patch(
        "agentception.readers.github.httpx.AsyncClient",
        side_effect=[mock1, mock2],
    ):
        await close_pr(42, "closing")

    assert len(_cache) == 0


@pytest.mark.anyio
async def test_api_get_raises_on_http_error() -> None:
    """_api_get must raise RuntimeError when the server returns 4xx/5xx."""
    mock = _mock_client(status_code=404)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        with pytest.raises(RuntimeError, match="GitHub API GET"):
            await _api_get("repos/x/y/issues/9999", {}, "fail_key")


# ---------------------------------------------------------------------------
# get_open_issues
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_open_issues_filters_by_label() -> None:
    """get_open_issues(label=...) must pass labels= to the API and return list."""
    issues_raw = [
        {"number": 10, "title": "Issue A", "labels": [], "body": ""},
        {"number": 11, "title": "Issue B", "labels": [], "body": ""},
    ]
    issues: JsonValue = json.loads(json.dumps(issues_raw))
    mock = _mock_client(get=issues)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await get_open_issues(label="batch-01")

    call_kwargs = mock.get.call_args.kwargs
    assert call_kwargs["params"]["labels"] == "batch-01"
    assert len(result) == 2
    assert result[0]["number"] == 10


@pytest.mark.anyio
async def test_get_open_issues_no_label() -> None:
    """get_open_issues() without a label must NOT include labels= in params."""
    mock = _mock_client(get=[])

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await get_open_issues()

    call_kwargs = mock.get.call_args.kwargs
    assert "labels" not in call_kwargs["params"]
    assert result == []


@pytest.mark.anyio
async def test_get_open_issues_excludes_pull_requests() -> None:
    """get_open_issues() must filter out items that have a pull_request key."""
    items_raw = [
        {"number": 1, "title": "Real issue", "labels": []},
        {"number": 2, "title": "A PR", "labels": [], "pull_request": {"url": "..."}},
    ]
    items: JsonValue = json.loads(json.dumps(items_raw))
    mock = _mock_client(get=items)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await get_open_issues()

    assert len(result) == 1
    assert result[0]["number"] == 1


# ---------------------------------------------------------------------------
# get_wip_issues
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_wip_issues_empty() -> None:
    """get_wip_issues() must return an empty list when no agent/wip issues exist."""
    mock = _mock_client(get=[])

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await get_wip_issues()

    assert result == []


@pytest.mark.anyio
async def test_get_wip_issues_passes_agent_wip_label() -> None:
    """get_wip_issues() must delegate to get_open_issues with label='agent/wip'."""
    mock = _mock_client(get=[])

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        await get_wip_issues()

    call_kwargs = mock.get.call_args.kwargs
    assert call_kwargs["params"]["labels"] == "agent/wip"


# ---------------------------------------------------------------------------
# get_active_label
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_active_label_returns_first_match_from_config() -> None:
    """get_active_label() returns the first label in active_labels_order with open issues."""
    from agentception.models import PipelineConfig
    from agentception.readers import pipeline_config as _pc

    mock_config = PipelineConfig(
        coordinator_limits={"engineering-coordinator": 1, "qa-coordinator": 1},
        pool_size=4,
        active_labels_order=["phase/0", "phase/1", "phase/2"],
    )
    # Only phase/1 and phase/2 have open issues.
    api_issues_raw = [
        {"number": 1, "labels": [{"name": "phase/1"}]},
        {"number": 2, "labels": [{"name": "phase/2"}]},
    ]
    api_issues: JsonValue = json.loads(json.dumps(api_issues_raw))
    mock = _mock_client(get=api_issues)

    with (
        patch.object(_pc, "read_pipeline_config", return_value=mock_config),
        patch("agentception.readers.github.httpx.AsyncClient", return_value=mock),
    ):
        result = await get_active_label()

    assert result == "phase/1"


@pytest.mark.anyio
async def test_get_active_label_returns_none_when_no_match() -> None:
    """get_active_label() returns None when no configured label has open issues."""
    from agentception.models import PipelineConfig
    from agentception.readers import pipeline_config as _pc

    mock_config = PipelineConfig(
        coordinator_limits={"engineering-coordinator": 1, "qa-coordinator": 1},
        pool_size=4,
        active_labels_order=["phase/0", "phase/1"],
    )
    api_issues_raw = [
        {"number": 3, "labels": [{"name": "enhancement"}]},
    ]
    api_issues: JsonValue = json.loads(json.dumps(api_issues_raw))
    mock = _mock_client(get=api_issues)

    with (
        patch.object(_pc, "read_pipeline_config", return_value=mock_config),
        patch("agentception.readers.github.httpx.AsyncClient", return_value=mock),
    ):
        result = await get_active_label()

    assert result is None


# ---------------------------------------------------------------------------
# get_open_prs
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_open_prs_normalises_field_names() -> None:
    """get_open_prs() must map head.ref→headRefName, base.ref→baseRefName, draft→isDraft."""
    raw_pr: dict[str, JsonValue] = {
        "number": 5,
        "title": "feat: something",
        "head": {"ref": "feat/something"},
        "base": {"ref": "dev"},
        "draft": False,
        "labels": [],
        "state": "open",
        "body": "",
    }
    mock = _mock_client(get=[raw_pr])

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await get_open_prs()

    assert len(result) == 1
    pr = result[0]
    assert pr["headRefName"] == "feat/something"
    assert pr["baseRefName"] == "dev"
    assert pr["isDraft"] is False

    call_kwargs = mock.get.call_args.kwargs
    assert call_kwargs["params"]["base"] == "dev"


# ---------------------------------------------------------------------------
# get_issue_body
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_issue_body_returns_string() -> None:
    """get_issue_body(N) must return the issue body string."""
    mock = _mock_client(get={"number": 42, "body": "This is the body."})

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await get_issue_body(42)

    assert result == "This is the body."


# ---------------------------------------------------------------------------
# close_pr
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_close_pr_posts_comment_then_patches_pr() -> None:
    """close_pr() must POST a comment and then PATCH the PR to closed."""
    comment_mock = _mock_client(post={"html_url": "https://github.com/x#c1"})
    patch_mock = _mock_client()

    with patch(
        "agentception.readers.github.httpx.AsyncClient",
        side_effect=[comment_mock, patch_mock],
    ):
        await close_pr(99, "closing: no longer needed")

    # Comment POST body
    post_json = comment_mock.post.call_args.kwargs["json"]
    assert post_json["body"] == "closing: no longer needed"

    # PR PATCH sets state=closed
    patch_json = patch_mock.patch.call_args.kwargs["json"]
    assert patch_json["state"] == "closed"


@pytest.mark.anyio
async def test_close_pr_raises_on_api_failure() -> None:
    """close_pr() must propagate RuntimeError when the comment POST fails."""
    mock = _mock_client(status_code=403)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        with pytest.raises(RuntimeError, match="GitHub API POST"):
            await close_pr(1, "test")


# ---------------------------------------------------------------------------
# clear_wip_label
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_clear_wip_label_removes_agent_wip() -> None:
    """clear_wip_label() must DELETE the agent/wip label from the issue."""
    mock = _mock_client()

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        await clear_wip_label(613)

    url: str = mock.delete.call_args.args[0]
    assert "agent%2Fwip" in url or "agent/wip" in url


@pytest.mark.anyio
async def test_clear_wip_label_invalidates_cache() -> None:
    """clear_wip_label() must empty the cache as a side effect."""
    _cache["some_key"] = ("value", time.monotonic() + 60)

    mock = _mock_client()
    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        await clear_wip_label(613)

    assert len(_cache) == 0


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_issue_normalises_labels_to_strings() -> None:
    """get_issue() must return labels as a list of name strings."""
    raw: dict[str, JsonValue] = {
        "number": 42,
        "state": "open",
        "title": "Fix it",
        "body": "details",
        "labels": [{"name": "enhancement"}, {"name": "bug"}],
    }
    mock = _mock_client(get=raw)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await get_issue(42)

    assert isinstance(result, dict)
    assert result["state"] == "open"
    assert result["title"] == "Fix it"
    assert result["labels"] == ["enhancement", "bug"]


@pytest.mark.anyio
async def test_get_issue_raises_on_404() -> None:
    """get_issue() must raise RuntimeError when the API returns 404."""
    mock = _mock_client(status_code=404)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        with pytest.raises(RuntimeError, match="GitHub API GET"):
            await get_issue(9999)


# ---------------------------------------------------------------------------
# add_wip_label
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_add_wip_label_posts_correct_label() -> None:
    """add_wip_label() must POST labels=['agent/wip'] to the issues labels endpoint."""
    mock = _mock_client(post=[{"name": "agent/wip"}])

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        await add_wip_label(42)

    post_json = mock.post.call_args.kwargs["json"]
    assert post_json["labels"] == ["agent/wip"]
    url: str = mock.post.call_args.args[0]
    assert "/issues/42/labels" in url


@pytest.mark.anyio
async def test_add_wip_label_invalidates_cache() -> None:
    """add_wip_label() must empty the cache as a side effect."""
    _cache["some_key"] = ("value", time.monotonic() + 60)

    mock = _mock_client(post=[{"name": "agent/wip"}])
    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        await add_wip_label(42)

    assert len(_cache) == 0


@pytest.mark.anyio
async def test_add_wip_label_raises_on_api_failure() -> None:
    """add_wip_label() must raise RuntimeError when the API returns an error."""
    mock = _mock_client(status_code=422)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        with pytest.raises(RuntimeError, match="GitHub API POST"):
            await add_wip_label(42)


# ---------------------------------------------------------------------------
# approve_pr
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_approve_pr_posts_approve_event() -> None:
    """approve_pr() must POST event=APPROVE to the PR reviews endpoint."""
    mock = _mock_client(post={"id": 1, "state": "APPROVED"})

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        await approve_pr(99)

    post_json = mock.post.call_args.kwargs["json"]
    assert post_json["event"] == "APPROVE"
    url: str = mock.post.call_args.args[0]
    assert "/pulls/99/reviews" in url


@pytest.mark.anyio
async def test_approve_pr_invalidates_cache() -> None:
    """approve_pr() must empty the cache on success."""
    _cache["some_key"] = ("value", time.monotonic() + 60)

    mock = _mock_client(post={"id": 1})
    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        await approve_pr(99)

    assert len(_cache) == 0


@pytest.mark.anyio
async def test_approve_pr_raises_on_failure() -> None:
    """approve_pr() must raise RuntimeError when the API returns an error."""
    mock = _mock_client(status_code=422)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        with pytest.raises(RuntimeError, match="GitHub API POST"):
            await approve_pr(99)


# ---------------------------------------------------------------------------
# merge_pr
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_merge_pr_puts_squash_merge() -> None:
    """merge_pr() must PUT merge_method=squash to the PR merge endpoint."""
    pr_data: dict[str, JsonValue] = {"head": {"ref": "feat/my-feature"}, "number": 99}
    get_mock = _mock_client(get=pr_data)
    put_mock = _mock_client(put={"merged": True, "sha": "abc123"})
    delete_mock = _mock_client()

    with patch(
        "agentception.readers.github.httpx.AsyncClient",
        side_effect=[get_mock, put_mock, delete_mock],
    ):
        await merge_pr(99, delete_branch=True)

    put_json = put_mock.put.call_args.kwargs["json"]
    assert put_json["merge_method"] == "squash"
    url: str = put_mock.put.call_args.args[0]
    assert "/pulls/99/merge" in url


@pytest.mark.anyio
async def test_merge_pr_deletes_branch_by_default() -> None:
    """merge_pr() must DELETE the head branch after merging when delete_branch=True."""
    pr_data: dict[str, JsonValue] = {"head": {"ref": "feat/my-feature"}, "number": 99}
    get_mock = _mock_client(get=pr_data)
    put_mock = _mock_client(put={"merged": True})
    delete_mock = _mock_client()

    with patch(
        "agentception.readers.github.httpx.AsyncClient",
        side_effect=[get_mock, put_mock, delete_mock],
    ):
        await merge_pr(99, delete_branch=True)

    delete_url: str = delete_mock.delete.call_args.args[0]
    assert "feat" in delete_url or "my-feature" in delete_url


@pytest.mark.anyio
async def test_merge_pr_skip_delete_when_false() -> None:
    """merge_pr(delete_branch=False) must not issue a DELETE request."""
    # No GET for head ref, no DELETE — only PUT.
    put_mock = _mock_client(put={"merged": True})

    with patch(
        "agentception.readers.github.httpx.AsyncClient",
        return_value=put_mock,
    ):
        await merge_pr(99, delete_branch=False)

    assert put_mock.delete.call_count == 0


@pytest.mark.anyio
async def test_merge_pr_invalidates_cache() -> None:
    """merge_pr() must empty the cache after the merge succeeds."""
    _cache["some_key"] = ("value", time.monotonic() + 60)

    put_mock = _mock_client(put={"merged": True})
    with patch(
        "agentception.readers.github.httpx.AsyncClient",
        return_value=put_mock,
    ):
        await merge_pr(99, delete_branch=False)

    assert len(_cache) == 0


@pytest.mark.anyio
async def test_merge_pr_raises_on_api_failure() -> None:
    """merge_pr() must raise RuntimeError when the PUT merge returns an error."""
    mock = _mock_client(status_code=405)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        with pytest.raises(RuntimeError, match="GitHub API PUT"):
            await merge_pr(99, delete_branch=False)


# ---------------------------------------------------------------------------
# 429 retry / backoff coverage (Gap 4)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_api_get_retries_on_429() -> None:
    """_api_get must retry after a 429 and succeed on the second attempt."""
    import asyncio

    from agentception.readers.github import _api_get

    payload: JsonValue = {"number": 1, "title": "Retried"}

    resp_429 = _mock_response(None, 429)
    resp_429.headers = {"retry-after": "0"}
    resp_200 = _mock_response(payload, 200)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=[resp_429, resp_200])

    with (
        patch("agentception.readers.github.httpx.AsyncClient", return_value=client),
        patch("agentception.readers.github.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await _api_get("repos/x/y/issues/1", {}, "retry_key")

    assert result == payload
    assert client.get.call_count == 2


@pytest.mark.anyio
async def test_api_get_raises_after_max_retries_on_429() -> None:
    """_api_get must raise RuntimeError when all retries are exhausted on 429."""
    from agentception.readers.github import _MAX_RETRIES, _api_get

    resp_429 = _mock_response(None, 429)
    resp_429.headers = {"retry-after": "0"}

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp_429)

    with (
        patch("agentception.readers.github.httpx.AsyncClient", return_value=client),
        patch("agentception.readers.github.asyncio.sleep", new_callable=AsyncMock),
    ):
        with pytest.raises(RuntimeError, match="429"):
            await _api_get("repos/x/y/issues/1", {}, "exhaust_key")

    assert client.get.call_count == _MAX_RETRIES + 1


@pytest.mark.anyio
async def test_api_post_retries_on_429() -> None:
    """_api_post must retry after a 429 and succeed on the second attempt."""
    from agentception.readers.github import _api_post

    payload: JsonValue = {"number": 42, "html_url": "https://github.com/x/y/issues/42"}

    resp_429 = _mock_response(None, 429)
    resp_429.headers = {"retry-after": "0"}
    resp_200 = _mock_response(payload, 200)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=[resp_429, resp_200])

    with (
        patch("agentception.readers.github.httpx.AsyncClient", return_value=client),
        patch("agentception.readers.github.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await _api_post("repos/x/y/issues", {"title": "Test"})

    assert result == payload
    assert client.post.call_count == 2


@pytest.mark.anyio
async def test_create_issue_returns_issue_dict() -> None:
    """create_issue() must POST to the issues endpoint and return the response dict."""
    from agentception.readers.github import create_issue

    issue_payload: JsonValue = {
        "number": 99,
        "html_url": "https://github.com/x/y/issues/99",
        "state": "open",
        "title": "New issue",
        "body": "Details here.",
    }
    mock = _mock_client(post=issue_payload)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await create_issue("New issue", "Details here.", labels=["bug"])

    assert result["number"] == 99
    assert result["title"] == "New issue"
    post_json = mock.post.call_args.kwargs["json"]
    assert post_json["title"] == "New issue"
    assert post_json["labels"] == ["bug"]


@pytest.mark.anyio
async def test_update_issue_patches_only_provided_fields() -> None:
    """update_issue() must PATCH only the fields that are not None."""
    from agentception.readers.github import update_issue

    updated_payload: JsonValue = {
        "number": 7,
        "state": "closed",
        "title": "Original title",
        "body": "Original body",
    }
    mock = _mock_client(patch=updated_payload)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=mock):
        result = await update_issue(7, state="closed")

    assert result["state"] == "closed"
    patch_json = mock.patch.call_args.kwargs["json"]
    assert patch_json == {"state": "closed"}
    assert "title" not in patch_json
    assert "body" not in patch_json


@pytest.mark.anyio
async def test_ensure_label_exists_updates_on_422_already_exists() -> None:
    """ensure_label_exists() must PATCH when the POST returns 422 with already_exists code."""
    from agentception.readers.github import ensure_label_exists

    # POST returns 422 with "already_exists" error code; PATCH succeeds.
    already_exists_body: dict[str, JsonValue] = {
        "message": "Validation Failed",
        "errors": [{"resource": "Label", "code": "already_exists", "field": "name"}],
    }
    resp_422 = _mock_response(already_exists_body, 422)
    resp_422.raise_for_status = MagicMock()  # 422 is handled before raise_for_status
    resp_422.text = ""
    resp_200_patch = _mock_response({"name": "approved"}, 200)

    post_client = MagicMock()
    post_client.__aenter__ = AsyncMock(return_value=post_client)
    post_client.__aexit__ = AsyncMock(return_value=False)
    post_client.post = AsyncMock(return_value=resp_422)

    patch_client = MagicMock()
    patch_client.__aenter__ = AsyncMock(return_value=patch_client)
    patch_client.__aexit__ = AsyncMock(return_value=False)
    patch_client.patch = AsyncMock(return_value=resp_200_patch)

    with patch(
        "agentception.readers.github.httpx.AsyncClient",
        side_effect=[post_client, patch_client],
    ):
        await ensure_label_exists("approved", "2ea44f", "Approved by reviewer")

    # POST was attempted once, then PATCH was called to update.
    assert post_client.post.call_count == 1
    assert patch_client.patch.call_count == 1


@pytest.mark.anyio
async def test_ensure_label_exists_raises_on_422_validation_error() -> None:
    """ensure_label_exists() must raise RuntimeError when POST 422 is a real validation error.

    GitHub returns 422 both for 'already exists' and for genuine validation
    failures (e.g. label name too long).  Only the 'already_exists' code
    should trigger the PATCH fallback — other 422 bodies must surface as errors
    rather than silently attempting a PATCH that will always 404.
    """
    from agentception.readers.github import ensure_label_exists

    validation_error_body: dict[str, JsonValue] = {
        "message": "Validation Failed",
        "errors": [{"resource": "Label", "code": "invalid", "field": "name"}],
    }
    resp_422 = MagicMock()
    resp_422.status_code = 422
    resp_422.json.return_value = validation_error_body
    resp_422.text = '{"message": "Validation Failed", "errors": [{"code": "invalid"}]}'
    resp_422.raise_for_status = MagicMock()

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp_422)

    with patch("agentception.readers.github.httpx.AsyncClient", return_value=client):
        with pytest.raises(RuntimeError, match="422"):
            await ensure_label_exists("x" * 60, "2ea44f", "Too long")

