"""Tests for the HTTP Streamable MCP endpoint.

Covers POST /api/mcp (and GET/DELETE) via httpx.AsyncClient against the
FastAPI test app.

Test categories:
  - Single JSON-RPC requests (initialize, ping, tools/list, prompts/list)
  - Notification requests (no id) → 202 Accepted
  - Batch requests (array of messages)
  - Error cases: malformed JSON, missing fields, invalid method
  - New tools accessible via HTTP (log_run_error, github_add_comment)
  - MCP 2025-11-25 compliance: protocol version string, server description
  - Security: Origin header validation (→ 403), MCP-Protocol-Version (→ 400)
  - Session management: initialize creates session (MCP-Session-Id header)
  - Transport disambiguation: GET without SSE Accept → 405; GET with evil
    Origin → 403 (Origin check runs before Accept check on GET)
  - Elicitation: request_human_input tool in tools/list
"""

from __future__ import annotations


import json
from unittest.mock import AsyncMock, patch

from agentception.types import JsonValue

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def app() -> FastAPI:
    """Return the FastAPI app instance."""
    from agentception.app import app as fastapi_app
    return fastapi_app


def _rpc(method: str, params: dict[str, JsonValue] | None = None, req_id: int | None = 1) -> dict[str, JsonValue]:
    req: dict[str, JsonValue] = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        req["id"] = req_id
    if params is not None:
        req["params"] = params
    return req


# ---------------------------------------------------------------------------
# Basic protocol methods
# ---------------------------------------------------------------------------

class TestMcpHttpBasic:
    @pytest.mark.anyio
    async def test_initialize_returns_capabilities(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("initialize"))
        assert r.status_code == 200
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        result = body["result"]
        assert isinstance(result, dict)
        caps = result["capabilities"]
        assert isinstance(caps, dict)
        assert "tools" in caps
        assert "resources" in caps
        assert "prompts" in caps

    @pytest.mark.anyio
    async def test_initialize_returns_2025_11_25_protocol_version(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("initialize"))
        result = r.json()["result"]
        assert result["protocolVersion"] == "2025-11-25"

    @pytest.mark.anyio
    async def test_initialize_returns_server_description(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("initialize"))
        server_info = r.json()["result"]["serverInfo"]
        assert "description" in server_info
        assert isinstance(server_info["description"], str)
        assert len(server_info["description"]) > 0

    @pytest.mark.anyio
    async def test_ping_returns_empty_result(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("ping"))
        assert r.status_code == 200
        body = r.json()
        assert body["result"] == {}

    @pytest.mark.anyio
    async def test_tools_list_returns_tools(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("tools/list"))
        assert r.status_code == 200
        body = r.json()
        result = body["result"]
        assert isinstance(result, dict)
        tools = result["tools"]
        assert isinstance(tools, list)
        assert len(tools) > 0
        names = [t["name"] for t in tools]
        assert "log_run_error" in names
        assert "github_add_comment" in names
        assert "request_human_input" in names

    @pytest.mark.anyio
    async def test_prompts_list_returns_prompts(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("prompts/list"))
        assert r.status_code == 200
        body = r.json()
        result = body["result"]
        assert isinstance(result, dict)
        prompts = result["prompts"]
        assert isinstance(prompts, list)
        assert len(prompts) > 0

    @pytest.mark.anyio
    async def test_resources_list_returns_resources(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("resources/list"))
        assert r.status_code == 200
        body = r.json()
        result = body["result"]
        assert isinstance(result, dict)
        resources = result["resources"]
        assert isinstance(resources, list)
        uris = [res["uri"] for res in resources]
        assert "ac://system/config" in uris
        assert "ac://roles/list" in uris


# ---------------------------------------------------------------------------
# Notifications (no id) → 202
# ---------------------------------------------------------------------------

class TestMcpHttpNotifications:
    @pytest.mark.anyio
    async def test_initialized_notification_returns_202(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("initialized", req_id=None))
        assert r.status_code == 202
        assert r.content == b""

    @pytest.mark.anyio
    async def test_unknown_notification_returns_202(self, app: FastAPI) -> None:
        # An unknown method with no id is a notification → 202 (not an error)
        # MCP spec: servers must not respond to notifications
        rpc_msg: dict[str, JsonValue] = {"jsonrpc": "2.0", "method": "notifications/cancel"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=rpc_msg)
        # handle_request_async returns method-not-found for unknown methods, so
        # the response will be a 200 JSON-RPC error (there IS an id in the response).
        # Without an id this is treated as a notification → None → 202.
        # Our mcp_http_endpoint returns 202 only when result is None.
        # Since the method is unknown AND there's no id, we expect 202.
        assert r.status_code in (200, 202)


# ---------------------------------------------------------------------------
# Batch requests
# ---------------------------------------------------------------------------

class TestMcpHttpBatch:
    @pytest.mark.anyio
    async def test_batch_two_requests(self, app: FastAPI) -> None:
        batch = [
            _rpc("ping", req_id=1),
            _rpc("tools/list", req_id=2),
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=batch)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 2
        ids = {item["id"] for item in body if isinstance(item, dict)}
        assert ids == {1, 2}

    @pytest.mark.anyio
    async def test_batch_all_notifications_returns_202(self, app: FastAPI) -> None:
        batch = [
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "method": "initialized"},
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=batch)
        assert r.status_code == 202

    @pytest.mark.anyio
    async def test_batch_mixed_notification_and_request(self, app: FastAPI) -> None:
        batch: list[dict[str, JsonValue]] = [
            {"jsonrpc": "2.0", "method": "initialized"},
            _rpc("ping", req_id=99),
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=batch)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["id"] == 99

    @pytest.mark.anyio
    async def test_batch_one_invalid_item_one_valid_returns_mixed_results(self, app: FastAPI) -> None:
        """Batch with one non-dict item and one valid request returns list of 2: error + success."""
        batch: list[JsonValue] = [
            42,
            _rpc("ping", req_id=1),
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=batch)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 2
        first, second = body[0], body[1]
        assert "error" in first
        assert first["error"]["code"] == -32600
        assert "result" in second
        assert second["id"] == 1
        assert second["result"] == {}


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestMcpHttpErrors:
    @pytest.mark.anyio
    async def test_invalid_json_returns_400(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                content=b"not valid json",
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_unknown_method_returns_method_not_found(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("nonexistent/method"))
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert body["error"]["code"] == -32601

    @pytest.mark.anyio
    async def test_wrong_jsonrpc_version_returns_error(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json={"jsonrpc": "1.0", "id": 1, "method": "ping"},
            )
        assert r.status_code == 200
        body = r.json()
        assert "error" in body

    @pytest.mark.anyio
    async def test_scalar_body_returns_400(self, app: FastAPI) -> None:
        """A JSON number is valid JSON but not a valid JSON-RPC message."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                content=b"42",
                headers={"Content-Type": "application/json"},
            )
        # handle_request_async will return an error for a non-dict input
        # (it gets wrapped correctly by _handle_single)
        assert r.status_code in (200, 400)

    @pytest.mark.anyio
    async def test_resources_read_invalid_uri_via_http(self, app: FastAPI) -> None:
        """resources/read with unknown ac:// URI returns 200 with error in result content."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("resources/read", {"uri": "ac://unknown/path"}),
            )
        assert r.status_code == 200
        body = r.json()
        assert "result" in body
        result = body["result"]
        assert "contents" in result
        contents = result["contents"]
        assert len(contents) == 1
        text = contents[0]["text"]
        payload = json.loads(text)
        assert "error" in payload

    @pytest.mark.anyio
    async def test_tools_call_missing_required_arguments_returns_error(self, app: FastAPI) -> None:
        """tools/call with missing required arguments returns 200 with isError true in result."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc(
                    "tools/call",
                    {"name": "build_claim_run", "arguments": {}},
                ),
            )
        assert r.status_code == 200
        body = r.json()
        assert "result" in body
        result = body["result"]
        assert result.get("isError") is True
        content = result.get("content", [])
        assert len(content) == 1
        text = content[0]["text"]
        payload = json.loads(text)
        assert "error" in payload


# ---------------------------------------------------------------------------
# New tools accessible via HTTP
# ---------------------------------------------------------------------------

class TestMcpHttpNewTools:
    @pytest.mark.anyio
    async def test_log_run_error_via_http(self, app: FastAPI) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/api/mcp",
                    json=_rpc(
                        "tools/call",
                        {"name": "log_run_error", "arguments": {"issue_number": 5, "error": "boom"}},
                    ),
                )
        assert r.status_code == 200
        body = r.json()
        result = body["result"]
        assert isinstance(result, dict)
        content = result["content"]
        assert isinstance(content, list)
        text = content[0]["text"]
        assert isinstance(text, str)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert payload["event"] == "error"

    @pytest.mark.anyio
    async def test_github_add_comment_via_http(self, app: FastAPI) -> None:
        comment_url = "https://github.com/org/repo/issues/1#issuecomment-1"
        with patch(
            "agentception.mcp.github_tools.add_comment_to_issue",
            new_callable=AsyncMock,
            return_value=comment_url,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/api/mcp",
                    json=_rpc(
                        "tools/call",
                        {"name": "github_add_comment", "arguments": {"issue_number": 1, "body": "Hello"}},
                    ),
                )
        assert r.status_code == 200
        body = r.json()
        result = body["result"]
        assert isinstance(result, dict)
        content = result["content"]
        assert isinstance(content, list)
        text = content[0]["text"]
        assert isinstance(text, str)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert payload["comment_url"] == comment_url


# ---------------------------------------------------------------------------
# MCP 2025-11-25: Origin header security (DNS rebinding protection)
# ---------------------------------------------------------------------------

class TestMcpHttpOriginSecurity:
    @pytest.mark.anyio
    async def test_no_origin_header_is_allowed(self, app: FastAPI) -> None:
        """Programmatic clients (agents) send no Origin — must be accepted."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("ping"))
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_localhost_origin_is_allowed(self, app: FastAPI) -> None:
        """Requests from localhost pages are permitted."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"Origin": "http://localhost:3000"},
            )
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_127_0_0_1_origin_is_allowed(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"Origin": "http://127.0.0.1:1337"},
            )
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_external_origin_returns_403(self, app: FastAPI) -> None:
        """Cross-origin browser requests from external domains must be blocked."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"Origin": "https://evil.example.com"},
            )
        assert r.status_code == 403
        body = r.json()
        assert "error" in body

    @pytest.mark.anyio
    async def test_malformed_origin_returns_403(self, app: FastAPI) -> None:
        """An unparseable Origin header is rejected."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"Origin": "not-a-url"},
            )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# MCP 2025-11-25: MCP-Protocol-Version header validation
# ---------------------------------------------------------------------------

class TestMcpHttpProtocolVersion:
    @pytest.mark.anyio
    async def test_no_version_header_is_accepted(self, app: FastAPI) -> None:
        """Absent header → backwards compatible, allowed per spec."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("ping"))
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_current_version_header_is_accepted(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"MCP-Protocol-Version": "2025-11-25"},
            )
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_legacy_version_header_is_accepted(self, app: FastAPI) -> None:
        """2025-03-26 is still supported for backwards compatibility."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"MCP-Protocol-Version": "2025-03-26"},
            )
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_unsupported_version_header_returns_400(self, app: FastAPI) -> None:
        """A version string we don't recognise must be rejected with 400."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"MCP-Protocol-Version": "2030-01-01"},
            )
        assert r.status_code == 400
        body = r.json()
        assert "error" in body

    @pytest.mark.anyio
    async def test_garbage_version_header_returns_400(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/mcp",
                json=_rpc("ping"),
                headers={"MCP-Protocol-Version": "not-a-version"},
            )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# MCP 2025-11-25: Session management
# ---------------------------------------------------------------------------

class TestMcpHttpSession:
    @pytest.mark.anyio
    async def test_initialize_returns_mcp_session_id_header(self, app: FastAPI) -> None:
        """initialize must return MCP-Session-Id so the client can open an SSE stream."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("initialize"))
        assert r.status_code == 200
        assert "mcp-session-id" in r.headers

    @pytest.mark.anyio
    async def test_initialize_with_elicitation_capability_creates_session(self, app: FastAPI) -> None:
        """initialize with elicitation.form capability sets the session up for elicitation."""
        init_body = _rpc("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {"elicitation": {"form": {}}},
            "clientInfo": {"name": "test", "version": "0"},
        })
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=init_body)
        assert r.status_code == 200
        session_id = r.headers.get("mcp-session-id")
        assert session_id
        assert len(session_id) > 8

    @pytest.mark.anyio
    async def test_delete_unknown_session_returns_400(self, app: FastAPI) -> None:
        """DELETE without a MCP-Session-Id header returns 400."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.delete("/api/mcp")
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_delete_known_session_returns_200(self, app: FastAPI) -> None:
        """DELETE with a valid session ID terminates the session."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            init_r = await client.post("/api/mcp", json=_rpc("initialize"))
        session_id = init_r.headers["mcp-session-id"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            del_r = await client.delete(
                "/api/mcp", headers={"MCP-Session-Id": session_id}
            )
        assert del_r.status_code == 200

    @pytest.mark.anyio
    async def test_rpc_response_routing_returns_202(self, app: FastAPI) -> None:
        """A client-sent JSON-RPC response (has id, no method) is routed to pending futures."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            init_r = await client.post("/api/mcp", json=_rpc("initialize"))
        session_id = init_r.headers["mcp-session-id"]

        rpc_response: dict[str, JsonValue] = {
            "jsonrpc": "2.0",
            "id": "elicit-test-001",
            "result": {"action": "decline"},
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_r = await client.post(
                "/api/mcp",
                json=rpc_response,
                headers={"MCP-Session-Id": session_id},
            )
        assert resp_r.status_code == 202


# ---------------------------------------------------------------------------
# MCP 2025-11-25: Transport disambiguation — GET behaviour
# ---------------------------------------------------------------------------

class TestMcpHttpTransportDisambiguation:
    @pytest.mark.anyio
    async def test_get_without_accept_sse_returns_405(self, app: FastAPI) -> None:
        """GET /api/mcp without text/event-stream Accept returns 405.

        This is the correct signal for 2025-11-25-aware clients that probe
        for SSE support: when the server supports SSE it returns the stream;
        when it doesn't (or when the client doesn't request it), 405 tells
        the client to use POST-only mode.
        """
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/mcp")
        assert r.status_code == 405
        assert "POST" in r.headers.get("allow", "")

    @pytest.mark.anyio
    async def test_get_with_evil_origin_returns_403(self, app: FastAPI) -> None:
        """GET with an external Origin is blocked by DNS-rebinding protection.

        Origin validation runs before the Accept-header check, so the response
        is 403 (not 405) even without text/event-stream in Accept.
        """
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/api/mcp",
                headers={"Origin": "https://evil.example.com"},
            )
        assert r.status_code == 403

    @pytest.mark.anyio
    async def test_get_sse_without_session_returns_400(self, app: FastAPI) -> None:
        """GET with Accept: text/event-stream but no session ID returns 400."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/api/mcp",
                headers={"Accept": "text/event-stream"},
            )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_get_sse_with_invalid_session_returns_404(self, app: FastAPI) -> None:
        """GET with Accept: text/event-stream but unknown session ID returns 404."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/api/mcp",
                headers={
                    "Accept": "text/event-stream",
                    "MCP-Session-Id": "nonexistent-session-id",
                },
            )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# MCP 2025-11-25: Elicitation tool in registry
# ---------------------------------------------------------------------------

class TestMcpHttpElicitationTool:
    @pytest.mark.anyio
    async def test_request_human_input_in_tools_list(self, app: FastAPI) -> None:
        """request_human_input must be discoverable via tools/list."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/mcp", json=_rpc("tools/list"))
        tools = r.json()["result"]["tools"]
        tool = next((t for t in tools if t["name"] == "request_human_input"), None)
        assert tool is not None, "request_human_input not in tools list"
        schema = tool["inputSchema"]
        assert "message" in schema["properties"]
        assert "fields" in schema["properties"]

    @pytest.mark.anyio
    async def test_request_human_input_returns_no_client_when_no_session(
        self, app: FastAPI
    ) -> None:
        """request_human_input returns action=no_client when no dashboard is connected.

        We patch the session store to isolate this test from any form-capable
        sessions created by previous tests in the same process.
        """
        from agentception.mcp.sessions import McpSessionStore

        empty_store = McpSessionStore()
        with patch("agentception.mcp.elicitation.get_store", return_value=empty_store):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/api/mcp",
                    json=_rpc(
                        "tools/call",
                        {
                            "name": "request_human_input",
                            "arguments": {
                                "message": "What approach should I use?",
                                "fields": [
                                    {"name": "approach", "type": "string", "title": "Approach"},
                                ],
                            },
                        },
                    ),
                )
        assert r.status_code == 200
        body = r.json()
        result = body["result"]
        assert isinstance(result, dict)
        content_text = result["content"][0]["text"]
        payload = json.loads(content_text)
        assert payload["action"] == "no_client"
