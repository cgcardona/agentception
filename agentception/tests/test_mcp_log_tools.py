"""Tests for the MCP log-tools layer.

Covers all five log tools (log_run_step, log_run_blocker, log_run_decision,
log_run_message, log_run_error) exercised through the full call_tool_async /
handle_request_async dispatch path so the routing, argument validation, and
result shaping are all exercised together.

Test categories:
  - Direct function calls (unit)
  - call_tool_async dispatch (integration through the MCP router)
  - Argument validation errors
  - Async tool guard: log tools are async-only and return an error from call_tool
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
# Async-only guard — call_tool must redirect log tools
# ---------------------------------------------------------------------------


class TestLogToolsAreAsyncOnly:
    """log_* tools must return an error from the sync call_tool path."""

    @pytest.mark.parametrize("name", [
        "log_run_step",
        "log_run_blocker",
        "log_run_decision",
        "log_run_message",
        "log_run_error",
        "log_run_heartbeat",
    ])
    def test_call_tool_sync_returns_error(self, name: str) -> None:
        result = call_tool(name, {"issue_number": 1, "step_name": "x", "description": "x",
                                   "decision": "x", "rationale": "x", "message": "x", "error": "x"})
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert isinstance(text, str)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert "async" in payload["error"].lower()


# ---------------------------------------------------------------------------
# log_run_step
# ---------------------------------------------------------------------------


class TestLogRunStep:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            resp = await _dispatch("log_run_step", {"issue_number": 42, "step_name": "Reading codebase"})
        payload = _result_payload(resp)
        assert payload == {"ok": True, "event": "step_start"}
        mock_persist.assert_awaited_once()
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["issue_number"] == 42
        assert call_kwargs["event_type"] == "step_start"
        assert call_kwargs["payload"] == {"step": "Reading codebase"}

    @pytest.mark.anyio
    async def test_with_agent_run_id(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            resp = await _dispatch(
                "log_run_step",
                {"issue_number": 7, "step_name": "Cloning", "agent_run_id": "issue-7"},
            )
        payload = _result_payload(resp)
        assert payload["ok"] is True
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["agent_run_id"] == "issue-7"

    @pytest.mark.anyio
    async def test_missing_step_name_returns_error(self) -> None:
        resp = await _dispatch("log_run_step", {"issue_number": 1})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    @pytest.mark.anyio
    async def test_missing_issue_number_returns_error(self) -> None:
        resp = await _dispatch("log_run_step", {"step_name": "x"})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# log_run_blocker
# ---------------------------------------------------------------------------


class TestLogRunBlocker:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            resp = await _dispatch(
                "log_run_blocker",
                {"issue_number": 99, "description": "Waiting for DB migration"},
            )
        payload = _result_payload(resp)
        assert payload == {"ok": True, "event": "blocker"}
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["event_type"] == "blocker"
        assert call_kwargs["payload"] == {"description": "Waiting for DB migration"}

    @pytest.mark.anyio
    async def test_missing_description_returns_error(self) -> None:
        resp = await _dispatch("log_run_blocker", {"issue_number": 1})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# log_run_decision
# ---------------------------------------------------------------------------


class TestLogRunDecision:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            resp = await _dispatch(
                "log_run_decision",
                {
                    "issue_number": 5,
                    "decision": "Use SQLAlchemy 2.x",
                    "rationale": "Better async support",
                    "agent_run_id": "issue-5",
                },
            )
        payload = _result_payload(resp)
        assert payload == {"ok": True, "event": "decision"}
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["event_type"] == "decision"
        assert call_kwargs["payload"]["decision"] == "Use SQLAlchemy 2.x"
        assert call_kwargs["payload"]["rationale"] == "Better async support"

    @pytest.mark.anyio
    async def test_missing_rationale_returns_error(self) -> None:
        resp = await _dispatch(
            "log_run_decision", {"issue_number": 1, "decision": "x"}
        )
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# log_run_message
# ---------------------------------------------------------------------------


class TestLogRunMessage:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            resp = await _dispatch(
                "log_run_message",
                {"issue_number": 10, "message": "Found 3 related files"},
            )
        payload = _result_payload(resp)
        assert payload == {"ok": True, "event": "message"}
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["event_type"] == "message"
        assert call_kwargs["payload"] == {"message": "Found 3 related files"}

    @pytest.mark.anyio
    async def test_missing_message_returns_error(self) -> None:
        resp = await _dispatch("log_run_message", {"issue_number": 1})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# log_run_error (new)
# ---------------------------------------------------------------------------


class TestLogRunError:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            resp = await _dispatch(
                "log_run_error",
                {"issue_number": 33, "error": "RuntimeError: DB connection lost"},
            )
        payload = _result_payload(resp)
        assert payload == {"ok": True, "event": "error"}
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["event_type"] == "error"
        assert call_kwargs["payload"] == {"error": "RuntimeError: DB connection lost"}

    @pytest.mark.anyio
    async def test_with_agent_run_id(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            resp = await _dispatch(
                "log_run_error",
                {"issue_number": 33, "error": "boom", "agent_run_id": "issue-33"},
            )
        payload = _result_payload(resp)
        assert payload["ok"] is True
        assert mock_persist.call_args.kwargs["agent_run_id"] == "issue-33"

    @pytest.mark.anyio
    async def test_missing_error_field_returns_error(self) -> None:
        resp = await _dispatch("log_run_error", {"issue_number": 1})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    @pytest.mark.anyio
    async def test_missing_issue_number_returns_error(self) -> None:
        resp = await _dispatch("log_run_error", {"error": "oops"})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    def test_log_run_error_is_in_tools_list(self) -> None:
        from agentception.mcp.server import list_tools
        names = [t["name"] for t in list_tools()]
        assert "log_run_error" in names

    @pytest.mark.anyio
    async def test_call_tool_async_dispatches_correctly(self) -> None:
        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ):
            result = await call_tool_async(
                "log_run_error",
                {"issue_number": 1, "error": "test error"},
            )
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert isinstance(text, str)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert payload["event"] == "error"


# ---------------------------------------------------------------------------
# log_run_heartbeat
# ---------------------------------------------------------------------------

import datetime


class TestLogRunHeartbeat:
    @pytest.mark.anyio
    async def test_log_run_heartbeat_updates_timestamp(self) -> None:
        """Valid run_id: persist_run_heartbeat returns a timestamp; tool returns ok=True."""
        fixed_ts = datetime.datetime(2024, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
        with patch(
            "agentception.mcp.log_tools.persist_run_heartbeat",
            new_callable=AsyncMock,
            return_value=fixed_ts,
        ) as mock_heartbeat:
            resp = await _dispatch("log_run_heartbeat", {"run_id": "issue-275"})

        payload = _result_payload(resp)
        assert payload["ok"] is True
        assert payload["last_activity_at"] == fixed_ts.isoformat()
        mock_heartbeat.assert_awaited_once_with("issue-275")

    @pytest.mark.anyio
    async def test_log_run_heartbeat_missing_run(self) -> None:
        """Unknown run_id: persist_run_heartbeat returns None; tool returns ok=False, no raise."""
        with patch(
            "agentception.mcp.log_tools.persist_run_heartbeat",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await _dispatch("log_run_heartbeat", {"run_id": "bogus-run-id"})

        payload = _result_payload(resp)
        assert payload["ok"] is False
        assert payload["error"] == "run not found"

    @pytest.mark.anyio
    async def test_log_run_heartbeat_missing_run_id_returns_error(self) -> None:
        resp = await _dispatch("log_run_heartbeat", {})
        result = resp.get("result")
        assert isinstance(result, dict)
        assert result["isError"] is True

    def test_log_run_heartbeat_is_in_tools_list(self) -> None:
        from agentception.mcp.server import list_tools
        names = [t["name"] for t in list_tools()]
        assert "log_run_heartbeat" in names


# ---------------------------------------------------------------------------
# log_file_edit_event — datetime serialisation
# ---------------------------------------------------------------------------


class TestLogFileEditEvent:
    @pytest.mark.anyio
    async def test_datetime_timestamp_serialised_to_string(self) -> None:
        """model_dump(mode='json') must convert datetime → str before persist_agent_event."""
        from agentception.mcp.log_tools import log_file_edit_event
        from agentception.models import FileEditEvent

        fixed_ts = datetime.datetime(2024, 6, 1, 10, 30, 0, tzinfo=datetime.timezone.utc)
        event = FileEditEvent(
            path="agentception/foo.py",
            diff="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
            lines_omitted=0,
            timestamp=fixed_ts,
        )

        with patch(
            "agentception.mcp.log_tools.persist_agent_event",
            new_callable=AsyncMock,
        ) as mock_persist:
            await log_file_edit_event(issue_number=42, event=event, agent_run_id="issue-42")

        mock_persist.assert_awaited_once()
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["event_type"] == "file_edit"
        payload = call_kwargs["payload"]
        # timestamp must be a string, not a datetime — json.dumps would raise otherwise
        assert isinstance(payload["timestamp"], str), (
            "payload['timestamp'] must be a str (ISO 8601), not a datetime object"
        )
        # Verify it round-trips correctly
        assert datetime.datetime.fromisoformat(payload["timestamp"]) == fixed_ts
