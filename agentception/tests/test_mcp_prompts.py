from __future__ import annotations

"""Tests for the MCP Prompts capability.

Covers:
  - PROMPTS catalogue completeness and structure
  - prompts/list JSON-RPC handler via handle_request and handle_request_async
  - prompts/get JSON-RPC handler — happy path, unknown name, missing params
  - get_prompt() dispatcher for role/* and agent/* names
  - ping JSON-RPC handler
  - initialize declares prompts capability
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agentception.mcp.prompts import PROMPTS, get_prompt
from agentception.mcp.server import handle_request, handle_request_async, list_prompts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: dict[str, object] | None = None, req_id: int = 1) -> dict[str, object]:
    req: dict[str, object] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        req["params"] = params
    return req


def _unwrap(resp: dict[str, object] | None) -> dict[str, object]:
    assert resp is not None
    assert isinstance(resp, dict)
    return resp


# ---------------------------------------------------------------------------
# Catalogue completeness
# ---------------------------------------------------------------------------

class TestPromptsCatalogue:
    def test_prompts_is_non_empty(self) -> None:
        assert len(PROMPTS) > 0

    def test_all_entries_have_required_fields(self) -> None:
        for p in PROMPTS:
            assert isinstance(p["name"], str) and p["name"]
            assert isinstance(p["description"], str) and p["description"]
            assert isinstance(p["arguments"], list)

    def test_contains_role_prompts(self) -> None:
        names = [p["name"] for p in PROMPTS]
        role_names = [n for n in names if n.startswith("role/")]
        assert len(role_names) > 0, "Expected at least one role/* prompt"

    def test_contains_agent_prompts(self) -> None:
        names = [p["name"] for p in PROMPTS]
        agent_names = [n for n in names if n.startswith("agent/")]
        assert len(agent_names) > 0, "Expected at least one agent/* prompt"

    def test_no_duplicate_names(self) -> None:
        names = [p["name"] for p in PROMPTS]
        assert len(names) == len(set(names)), "Duplicate prompt names detected"

    def test_list_prompts_returns_same_as_catalogue(self) -> None:
        assert list_prompts() == PROMPTS


# ---------------------------------------------------------------------------
# get_prompt() unit tests
# ---------------------------------------------------------------------------

class TestGetPrompt:
    def test_unknown_name_returns_none(self) -> None:
        result = get_prompt("unknown/nonexistent")
        assert result is None

    def test_role_prompt_returns_content(self) -> None:
        fake_content = "# CTO Role\n\nYou are the CTO.\n"
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=fake_content):
            result = get_prompt("role/cto")
        assert result is not None
        assert result["description"] != ""
        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert msg["role"] == "user"
        assert msg["content"]["type"] == "text"
        assert msg["content"]["text"] == fake_content

    def test_agent_dispatcher_returns_content(self) -> None:
        fake_content = "# Dispatcher\n\nYou dispatch agents.\n"
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=fake_content):
            result = get_prompt("agent/dispatcher")
        assert result is not None
        assert result["messages"][0]["content"]["text"] == fake_content

    def test_role_not_found_returns_none(self) -> None:
        with patch.object(Path, "exists", return_value=False):
            result = get_prompt("role/nonexistent-role-xyz")
        assert result is None

    def test_wrong_prefix_returns_none(self) -> None:
        result = get_prompt("unknown/cto")
        assert result is None


# ---------------------------------------------------------------------------
# prompts/list JSON-RPC handler
# ---------------------------------------------------------------------------

class TestPromptsListRpc:
    def test_sync_handler_returns_prompts(self) -> None:
        resp = _unwrap(handle_request(_rpc("prompts/list")))
        result = resp.get("result")
        assert isinstance(result, dict)
        prompts = result.get("prompts")
        assert isinstance(prompts, list)
        assert len(prompts) == len(PROMPTS)

    @pytest.mark.anyio
    async def test_async_handler_returns_prompts(self) -> None:
        resp = _unwrap(await handle_request_async(_rpc("prompts/list")))
        result = resp.get("result")
        assert isinstance(result, dict)
        prompts = result.get("prompts")
        assert isinstance(prompts, list)
        assert len(prompts) > 0

    def test_each_prompt_has_name_description_arguments(self) -> None:
        resp = _unwrap(handle_request(_rpc("prompts/list")))
        result = resp.get("result")
        assert isinstance(result, dict)
        prompts = result.get("prompts")
        assert isinstance(prompts, list)
        for p in prompts:
            assert isinstance(p, dict)
            assert isinstance(p.get("name"), str)
            assert isinstance(p.get("description"), str)
            assert isinstance(p.get("arguments"), list)


# ---------------------------------------------------------------------------
# prompts/get JSON-RPC handler
# ---------------------------------------------------------------------------

class TestPromptsGetRpc:
    def test_known_role_prompt_returns_content(self) -> None:
        fake_content = "# CTO\n\nYou are the CTO."
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=fake_content):
            resp = _unwrap(handle_request(_rpc("prompts/get", {"name": "role/cto"})))
        result = resp.get("result")
        assert isinstance(result, dict)
        messages = result.get("messages")
        assert isinstance(messages, list)
        assert len(messages) == 1
        msg = messages[0]
        assert isinstance(msg, dict)
        assert msg["role"] == "user"
        content = msg.get("content")
        assert isinstance(content, dict)
        assert content["text"] == fake_content

    @pytest.mark.anyio
    async def test_async_handler_returns_content(self) -> None:
        fake_content = "# Engineer\n\nYou implement issues."
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=fake_content):
            resp = _unwrap(await handle_request_async(_rpc("prompts/get", {"name": "agent/engineer"})))
        result = resp.get("result")
        assert isinstance(result, dict)
        messages = result.get("messages")
        assert isinstance(messages, list)
        assert len(messages) == 1

    def test_unknown_prompt_returns_error_response(self) -> None:
        resp = _unwrap(handle_request(_rpc("prompts/get", {"name": "role/xyzzy-not-real"})))
        assert "error" in resp
        error = resp["error"]
        assert isinstance(error, dict)
        assert error["code"] == -32602

    def test_missing_name_returns_error(self) -> None:
        resp = _unwrap(handle_request(_rpc("prompts/get", {})))
        assert "error" in resp

    def test_missing_params_returns_error(self) -> None:
        resp = _unwrap(handle_request(_rpc("prompts/get")))
        assert "error" in resp

    def test_empty_name_returns_error(self) -> None:
        resp = _unwrap(handle_request(_rpc("prompts/get", {"name": ""})))
        assert "error" in resp


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

class TestPing:
    def test_sync_ping_returns_empty_result(self) -> None:
        resp = _unwrap(handle_request(_rpc("ping")))
        assert "result" in resp
        result = resp["result"]
        assert isinstance(result, dict)
        assert result == {}

    @pytest.mark.anyio
    async def test_async_ping_returns_empty_result(self) -> None:
        resp = _unwrap(await handle_request_async(_rpc("ping")))
        assert "result" in resp
        result = resp["result"]
        assert isinstance(result, dict)
        assert result == {}


# ---------------------------------------------------------------------------
# initialize declares prompts capability
# ---------------------------------------------------------------------------

class TestInitializeCapabilities:
    def test_sync_initialize_includes_prompts(self) -> None:
        resp = _unwrap(handle_request(_rpc("initialize")))
        result = resp.get("result")
        assert isinstance(result, dict)
        capabilities = result.get("capabilities")
        assert isinstance(capabilities, dict)
        assert "prompts" in capabilities
        assert "tools" in capabilities
        assert "resources" in capabilities

    @pytest.mark.anyio
    async def test_async_initialize_includes_prompts(self) -> None:
        resp = _unwrap(await handle_request_async(_rpc("initialize")))
        result = resp.get("result")
        assert isinstance(result, dict)
        capabilities = result.get("capabilities")
        assert isinstance(capabilities, dict)
        assert "prompts" in capabilities
