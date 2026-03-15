"""Tests for the MCP github-tools layer.

Covers all seven GitHub tools (github_add_label, github_remove_label,
github_claim_issue, github_unclaim_issue, github_add_comment,
github_approve_pr, github_merge_pr) exercised through
the full call_tool_async / handle_request_async dispatch path.

Test categories:
  - Direct function calls (unit) with readers.github mocked
  - call_tool_async dispatch (integration through the MCP router)
  - Argument validation errors
  - Async-only guard: github tools must fail from the sync call_tool path
  - Error propagation: RuntimeError from readers.github surfaces as ok=false
"""

from __future__ import annotations


import json
from unittest.mock import AsyncMock, patch

import pytest

from agentception.mcp.server import call_tool, call_tool_async, handle_request_async
from agentception.types import JsonValue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpc_call(name: str, args: dict[str, JsonValue]) -> dict[str, JsonValue]:
    params: dict[str, JsonValue] = {"name": name, "arguments": args}
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


async def _dispatch(name: str, args: dict[str, JsonValue]) -> dict[str, JsonValue]:
    resp = await handle_request_async(_rpc_call(name, args))
    assert resp is not None
    d: dict[str, JsonValue] = json.loads(json.dumps(resp))
    return d


def _result_payload(resp: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result = resp.get("result")
    assert isinstance(result, dict)
    content = result.get("content")
    assert isinstance(content, list)
    assert len(content) == 1
    item = content[0]
    assert isinstance(item, dict)
    text = item.get("text")
    assert isinstance(text, str)
    payload: dict[str, JsonValue] = json.loads(text)
    return payload


# ---------------------------------------------------------------------------
# Async-only guard
# ---------------------------------------------------------------------------

class TestGithubToolsAreAsyncOnly:
    @pytest.mark.parametrize("name", [
        "github_add_label",
        "github_remove_label",
        "github_claim_issue",
        "github_unclaim_issue",
        "github_add_comment",
        "github_approve_pr",
        "github_merge_pr",
    ])
    def test_sync_call_tool_returns_error(self, name: str) -> None:
        result = call_tool(name, {"issue_number": 1, "label": "x", "body": "x"})
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert isinstance(text, str)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert "async" in payload["error"].lower()


# ---------------------------------------------------------------------------
# github_add_label
# ---------------------------------------------------------------------------

class TestGithubAddLabel:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.github_tools.add_label_to_issue",
            new_callable=AsyncMock,
        ):
            resp = await _dispatch(
                "github_add_label", {"issue_number": 42, "label": "phase/2"}
            )
        payload = _result_payload(resp)
        assert payload == {"ok": True, "issue_number": 42, "added": "phase/2"}

    @pytest.mark.anyio
    async def test_runtime_error_returns_ok_false(self) -> None:
        with patch(
            "agentception.mcp.github_tools.add_label_to_issue",
            new_callable=AsyncMock,
            side_effect=RuntimeError("gh CLI failed"),
        ):
            resp = await _dispatch(
                "github_add_label", {"issue_number": 42, "label": "x"}
            )
        payload = _result_payload(resp)
        assert payload["ok"] is False
        err = payload["error"]
        assert isinstance(err, str)
        assert "gh CLI failed" in err

    @pytest.mark.anyio
    async def test_missing_label_returns_error(self) -> None:
        resp = await _dispatch("github_add_label", {"issue_number": 1})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    @pytest.mark.anyio
    async def test_missing_issue_number_returns_error(self) -> None:
        resp = await _dispatch("github_add_label", {"label": "x"})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# github_remove_label
# ---------------------------------------------------------------------------

class TestGithubRemoveLabel:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.github_tools.remove_label_from_issue",
            new_callable=AsyncMock,
        ):
            resp = await _dispatch(
                "github_remove_label", {"issue_number": 10, "label": "pipeline/gated"}
            )
        payload = _result_payload(resp)
        assert payload == {"ok": True, "issue_number": 10, "removed": "pipeline/gated"}

    @pytest.mark.anyio
    async def test_runtime_error_returns_ok_false(self) -> None:
        with patch(
            "agentception.mcp.github_tools.remove_label_from_issue",
            new_callable=AsyncMock,
            side_effect=RuntimeError("not found"),
        ):
            resp = await _dispatch(
                "github_remove_label", {"issue_number": 10, "label": "x"}
            )
        payload = _result_payload(resp)
        assert payload["ok"] is False

    @pytest.mark.anyio
    async def test_missing_args_returns_error(self) -> None:
        resp = await _dispatch("github_remove_label", {"issue_number": 1})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# github_claim_issue
# ---------------------------------------------------------------------------

class TestGithubClaimIssue:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.github_tools.add_wip_label",
            new_callable=AsyncMock,
        ):
            resp = await _dispatch("github_claim_issue", {"issue_number": 77})
        payload = _result_payload(resp)
        assert payload == {"ok": True, "issue_number": 77, "claimed": True}

    @pytest.mark.anyio
    async def test_runtime_error_returns_ok_false(self) -> None:
        with patch(
            "agentception.mcp.github_tools.add_wip_label",
            new_callable=AsyncMock,
            side_effect=RuntimeError("rate limit"),
        ):
            resp = await _dispatch("github_claim_issue", {"issue_number": 77})
        payload = _result_payload(resp)
        assert payload["ok"] is False
        err = payload["error"]
        assert isinstance(err, str)
        assert "rate limit" in err

    @pytest.mark.anyio
    async def test_missing_issue_number_returns_error(self) -> None:
        resp = await _dispatch("github_claim_issue", {})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# github_unclaim_issue
# ---------------------------------------------------------------------------

class TestGithubUnclaimIssue:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.github_tools.clear_wip_label",
            new_callable=AsyncMock,
        ):
            resp = await _dispatch("github_unclaim_issue", {"issue_number": 55})
        payload = _result_payload(resp)
        assert payload == {"ok": True, "issue_number": 55, "claimed": False}

    @pytest.mark.anyio
    async def test_runtime_error_returns_ok_false(self) -> None:
        with patch(
            "agentception.mcp.github_tools.clear_wip_label",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network error"),
        ):
            resp = await _dispatch("github_unclaim_issue", {"issue_number": 55})
        payload = _result_payload(resp)
        assert payload["ok"] is False


# ---------------------------------------------------------------------------
# github_add_comment (new)
# ---------------------------------------------------------------------------

class TestGithubAddComment:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        comment_url = "https://github.com/org/repo/issues/42#issuecomment-123"
        with patch(
            "agentception.mcp.github_tools.add_comment_to_issue",
            new_callable=AsyncMock,
            return_value=comment_url,
        ):
            resp = await _dispatch(
                "github_add_comment",
                {"issue_number": 42, "body": "## Agent fingerprint\n\nStarted work."},
            )
        payload = _result_payload(resp)
        assert payload["ok"] is True
        assert payload["issue_number"] == 42
        assert payload["comment_url"] == comment_url

    @pytest.mark.anyio
    async def test_runtime_error_returns_ok_false(self) -> None:
        with patch(
            "agentception.mcp.github_tools.add_comment_to_issue",
            new_callable=AsyncMock,
            side_effect=RuntimeError("gh API error"),
        ):
            resp = await _dispatch(
                "github_add_comment", {"issue_number": 5, "body": "hello"}
            )
        payload = _result_payload(resp)
        assert payload["ok"] is False
        err = payload["error"]
        assert isinstance(err, str)
        assert "gh API error" in err

    @pytest.mark.anyio
    async def test_missing_body_returns_error(self) -> None:
        resp = await _dispatch("github_add_comment", {"issue_number": 1})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    @pytest.mark.anyio
    async def test_empty_body_returns_error(self) -> None:
        resp = await _dispatch("github_add_comment", {"issue_number": 1, "body": ""})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    @pytest.mark.anyio
    async def test_missing_issue_number_returns_error(self) -> None:
        resp = await _dispatch("github_add_comment", {"body": "hello"})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    def test_github_add_comment_is_in_tools_list(self) -> None:
        from agentception.mcp.server import list_tools
        names = [t["name"] for t in list_tools()]
        assert "github_add_comment" in names

    @pytest.mark.anyio
    async def test_call_tool_async_dispatches_correctly(self) -> None:
        comment_url = "https://github.com/org/repo/issues/1#issuecomment-1"
        with patch(
            "agentception.mcp.github_tools.add_comment_to_issue",
            new_callable=AsyncMock,
            return_value=comment_url,
        ):
            result = await call_tool_async(
                "github_add_comment", {"issue_number": 1, "body": "test"}
            )
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert isinstance(text, str)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert payload["comment_url"] == comment_url


# ---------------------------------------------------------------------------
# github_approve_pr
# ---------------------------------------------------------------------------


class TestGithubApprovePr:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.github_tools.approve_pr",
            new_callable=AsyncMock,
        ):
            resp = await _dispatch("github_approve_pr", {"pr_number": 99})
        payload = _result_payload(resp)
        assert payload == {"ok": True, "pr_number": 99}

    @pytest.mark.anyio
    async def test_runtime_error_returns_ok_false(self) -> None:
        with patch(
            "agentception.mcp.github_tools.approve_pr",
            new_callable=AsyncMock,
            side_effect=RuntimeError("already approved"),
        ):
            resp = await _dispatch("github_approve_pr", {"pr_number": 99})
        payload = _result_payload(resp)
        assert payload["ok"] is False
        err = payload["error"]
        assert isinstance(err, str)
        assert "already approved" in err

    @pytest.mark.anyio
    async def test_missing_pr_number_returns_error(self) -> None:
        resp = await _dispatch("github_approve_pr", {})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    def test_github_approve_pr_is_in_tools_list(self) -> None:
        from agentception.mcp.server import list_tools
        names = [t["name"] for t in list_tools()]
        assert "github_approve_pr" in names

    @pytest.mark.anyio
    async def test_call_tool_async_dispatches_correctly(self) -> None:
        with patch(
            "agentception.mcp.github_tools.approve_pr",
            new_callable=AsyncMock,
        ):
            result = await call_tool_async("github_approve_pr", {"pr_number": 42})
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert isinstance(payload, dict)
        assert payload["ok"] is True
        assert payload["pr_number"] == 42


# ---------------------------------------------------------------------------
# github_merge_pr
# ---------------------------------------------------------------------------


class TestGithubMergePr:
    @pytest.mark.anyio
    async def test_happy_path_default_delete_branch(self) -> None:
        with patch(
            "agentception.mcp.github_tools.merge_pr",
            new_callable=AsyncMock,
        ):
            resp = await _dispatch("github_merge_pr", {"pr_number": 88})
        payload = _result_payload(resp)
        assert payload["ok"] is True
        assert payload["pr_number"] == 88
        assert payload["delete_branch"] is True

    @pytest.mark.anyio
    async def test_happy_path_no_delete_branch(self) -> None:
        with patch(
            "agentception.mcp.github_tools.merge_pr",
            new_callable=AsyncMock,
        ):
            resp = await _dispatch(
                "github_merge_pr", {"pr_number": 88, "delete_branch": False}
            )
        payload = _result_payload(resp)
        assert payload["ok"] is True
        assert payload["delete_branch"] is False

    @pytest.mark.anyio
    async def test_runtime_error_returns_ok_false(self) -> None:
        with patch(
            "agentception.mcp.github_tools.merge_pr",
            new_callable=AsyncMock,
            side_effect=RuntimeError("merge conflict"),
        ):
            resp = await _dispatch("github_merge_pr", {"pr_number": 88})
        payload = _result_payload(resp)
        assert payload["ok"] is False
        err = payload["error"]
        assert isinstance(err, str)
        assert "merge conflict" in err

    @pytest.mark.anyio
    async def test_missing_pr_number_returns_error(self) -> None:
        resp = await _dispatch("github_merge_pr", {})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    def test_github_merge_pr_is_in_tools_list(self) -> None:
        from agentception.mcp.server import list_tools
        names = [t["name"] for t in list_tools()]
        assert "github_merge_pr" in names

    @pytest.mark.anyio
    async def test_call_tool_async_dispatches_correctly(self) -> None:
        with patch(
            "agentception.mcp.github_tools.merge_pr",
            new_callable=AsyncMock,
        ):
            result = await call_tool_async(
                "github_merge_pr", {"pr_number": 7, "delete_branch": True}
            )
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert isinstance(payload, dict)
        assert payload["ok"] is True
        assert payload["pr_number"] == 7
