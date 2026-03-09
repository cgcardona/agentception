from __future__ import annotations

"""Tests for the MCP resource layer.

Covers the full resource surface exposed by agentception/mcp/resources.py:
  - read_resource() URI dispatcher for all ac:// URIs
  - resources/list, resources/templates/list, resources/read JSON-RPC handlers
  - Redirect behaviour: calling a retired query_* / plan_get_* tool name returns
    a helpful error pointing to the correct resource URI
  - RESOURCES and RESOURCE_TEMPLATES catalogue completeness

Resources tested:
  ac://runs/active
  ac://runs/pending
  ac://runs/{run_id}
  ac://runs/{run_id}/children
  ac://runs/{run_id}/events (and ?after_id= pagination)
  ac://batches/{batch_id}/tree
  ac://system/dispatcher
  ac://system/health
  ac://plan/schema
  ac://plan/labels
  ac://plan/figures/{role}
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentception.mcp.resources import (
    RESOURCES,
    RESOURCE_TEMPLATES,
    read_resource,
)
from agentception.mcp.server import (
    TOOLS,
    call_tool_async,
    handle_request_async,
    list_resources,
    list_resource_templates,
)
from agentception.mcp.types import ACResourceResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content(result: ACResourceResult) -> dict[str, object]:
    """Decode the first content item of an ACResourceResult as JSON."""
    raw: str = result["contents"][0]["text"]
    out: dict[str, object] = json.loads(raw)
    return out


def _rpc(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    req: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        req["params"] = params
    return req


def _unwrap_rpc(resp: dict[str, object] | None) -> dict[str, object]:
    """Assert the RPC response is non-None and return it with narrowed type."""
    assert resp is not None
    return resp


def _rpc_result(resp: dict[str, object] | None) -> dict[str, object]:
    """Extract and narrow the 'result' value from an RPC response."""
    unwrapped = _unwrap_rpc(resp)
    result = unwrapped["result"]
    assert isinstance(result, dict)
    return result


def _rpc_contents(resp: dict[str, object] | None) -> list[dict[str, object]]:
    """Extract the 'contents' list from a resources/read RPC response."""
    result = _rpc_result(resp)
    raw_contents = result["contents"]
    assert isinstance(raw_contents, list)
    contents: list[dict[str, object]] = [c for c in raw_contents if isinstance(c, dict)]
    return contents


# ---------------------------------------------------------------------------
# Catalogue completeness
# ---------------------------------------------------------------------------


def test_resources_catalogue_has_expected_uris() -> None:
    """RESOURCES catalogue contains all expected static resource URIs."""
    uris = {r["uri"] for r in RESOURCES}
    assert "ac://runs/active" in uris
    assert "ac://runs/pending" in uris
    assert "ac://system/dispatcher" in uris
    assert "ac://system/health" in uris
    assert "ac://plan/schema" in uris
    assert "ac://plan/labels" in uris


def test_resource_templates_catalogue_has_expected_templates() -> None:
    """RESOURCE_TEMPLATES catalogue contains all expected URI templates."""
    templates = {t["uriTemplate"] for t in RESOURCE_TEMPLATES}
    assert "ac://runs/{run_id}" in templates
    assert "ac://runs/{run_id}/children" in templates
    assert "ac://runs/{run_id}/events" in templates
    assert "ac://batches/{batch_id}/tree" in templates
    assert "ac://plan/figures/{role}" in templates


def test_all_resources_have_required_fields() -> None:
    """Every resource definition has uri, name, description, and mimeType."""
    for r in RESOURCES:
        assert r["uri"]
        assert r["name"]
        assert r["description"]
        assert r["mimeType"] == "application/json"


def test_all_resource_templates_have_required_fields() -> None:
    """Every resource template has uriTemplate, name, description, and mimeType."""
    for t in RESOURCE_TEMPLATES:
        assert t["uriTemplate"]
        assert t["name"]
        assert t["description"]
        assert t["mimeType"] == "application/json"


def test_list_resources_returns_full_catalogue() -> None:
    assert list_resources() == RESOURCES


def test_list_resource_templates_returns_full_catalogue() -> None:
    assert list_resource_templates() == RESOURCE_TEMPLATES


# ---------------------------------------------------------------------------
# Retired query_* tools redirect to resource URIs
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_call_tool_async_returns_redirect_error_for_retired_tools() -> None:
    """Retired query_* tool names return a descriptive error pointing to the resource URI."""
    retired = {
        "query_pending_runs": "ac://runs/pending",
        "query_run": "ac://runs/{run_id}",
        "query_children": "ac://runs/{run_id}/children",
        "query_run_events": "ac://runs/{run_id}/events",
        "query_active_runs": "ac://runs/active",
        "query_run_tree": "ac://batches/{batch_id}/tree",
        "query_dispatcher_state": "ac://system/dispatcher",
        "query_system_health": "ac://system/health",
        "plan_get_schema": "ac://plan/schema",
        "plan_get_labels": "ac://plan/labels",
        "plan_get_cognitive_figures": "ac://plan/figures/{role}",
    }
    for tool_name, expected_uri in retired.items():
        result = await call_tool_async(tool_name, {})
        assert result["isError"] is True, f"{tool_name} should return isError=True"
        payload = json.loads(result["content"][0]["text"])
        assert "resources/read" in payload["error"], f"{tool_name} error should mention resources/read"
        assert expected_uri in payload["error"], f"{tool_name} error should include URI {expected_uri}"


def test_retired_query_tools_not_in_tools_list() -> None:
    """Retired query_* and plan_get_* tools are not in the TOOLS list."""
    names = {t["name"] for t in TOOLS}
    for retired in (
        "query_pending_runs",
        "query_run",
        "query_children",
        "query_run_events",
        "query_active_runs",
        "query_run_tree",
        "query_dispatcher_state",
        "query_system_health",
        "plan_get_schema",
        "plan_get_labels",
        "plan_get_cognitive_figures",
    ):
        assert retired not in names, f"{retired} should not be in TOOLS"


# ---------------------------------------------------------------------------
# JSON-RPC: resources/list
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handle_request_async_resources_list() -> None:
    """resources/list returns the full static resource catalogue."""
    resp = await handle_request_async(_rpc("resources/list"))
    result = _rpc_result(resp)
    raw_resources = result["resources"]
    assert isinstance(raw_resources, list)
    resources: list[dict[str, object]] = [r for r in raw_resources if isinstance(r, dict)]
    uris = {str(r["uri"]) for r in resources}
    assert "ac://runs/active" in uris
    assert "ac://system/health" in uris
    assert "ac://plan/schema" in uris


@pytest.mark.anyio
async def test_handle_request_async_resources_templates_list() -> None:
    """resources/templates/list returns the full resource template catalogue."""
    resp = await handle_request_async(_rpc("resources/templates/list"))
    result = _rpc_result(resp)
    raw_templates = result["resourceTemplates"]
    assert isinstance(raw_templates, list)
    templates: list[dict[str, object]] = [t for t in raw_templates if isinstance(t, dict)]
    uris = {str(t["uriTemplate"]) for t in templates}
    assert "ac://runs/{run_id}" in uris
    assert "ac://plan/figures/{role}" in uris


@pytest.mark.anyio
async def test_handle_request_async_resources_read_missing_uri_returns_error() -> None:
    """resources/read with missing params.uri returns an invalid-params error."""
    resp = await handle_request_async(_rpc("resources/read", {}))
    unwrapped = _unwrap_rpc(resp)
    assert "error" in unwrapped
    raw_error = unwrapped["error"]
    assert isinstance(raw_error, dict)
    assert raw_error["code"] == -32602


@pytest.mark.anyio
async def test_handle_request_async_resources_read_unknown_uri_returns_error_content() -> None:
    """resources/read with an unknown URI returns content with an error key (not a JSON-RPC error)."""
    resp = await handle_request_async(_rpc("resources/read", {"uri": "ac://unknown/path"}))
    contents = _rpc_contents(resp)
    assert len(contents) == 1
    payload = json.loads(str(contents[0]["text"]))
    assert "error" in payload


@pytest.mark.anyio
async def test_handle_request_async_resources_read_wrong_scheme() -> None:
    """resources/read with a non-ac:// URI returns an error content item."""
    resp = await handle_request_async(_rpc("resources/read", {"uri": "http://example.com/foo"}))
    contents = _rpc_contents(resp)
    payload = json.loads(str(contents[0]["text"]))
    assert "error" in payload


# ---------------------------------------------------------------------------
# initialize declares resources capability
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialize_declares_resources_capability() -> None:
    """initialize response includes both tools and resources capabilities."""
    resp = await handle_request_async({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    })
    result = _rpc_result(resp)
    caps = result["capabilities"]
    assert isinstance(caps, dict)
    assert "tools" in caps
    assert "resources" in caps


# ---------------------------------------------------------------------------
# ac://runs/active
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_active_runs() -> None:
    """ac://runs/active returns active run list."""
    mock_runs = [
        {
            "run_id": "issue-1",
            "status": "implementing",
            "role": "developer",
            "issue_number": 1,
            "pr_number": None,
            "branch": None,
            "worktree_path": None,
            "batch_id": "batch-x",
            "tier": "worker",
            "org_domain": "engineering",
            "parent_run_id": None,
            "spawned_at": "2026-03-01T00:00:00+00:00",
            "last_activity_at": None,
            "completed_at": None,
        }
    ]
    with patch(
        "agentception.mcp.query_tools.get_active_runs",
        new_callable=AsyncMock,
        return_value=mock_runs,
    ):
        result = await read_resource("ac://runs/active")

    payload = _content(result)
    assert result["contents"][0]["uri"] == "ac://runs/active"
    assert payload["ok"] is True
    assert payload["count"] == 1


@pytest.mark.anyio
async def test_read_resource_active_runs_via_rpc() -> None:
    """resources/read for ac://runs/active returns correct content via JSON-RPC."""
    with patch(
        "agentception.mcp.query_tools.get_active_runs",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await handle_request_async(_rpc("resources/read", {"uri": "ac://runs/active"}))

    contents = _rpc_contents(resp)
    assert contents[0]["uri"] == "ac://runs/active"
    assert contents[0]["mimeType"] == "application/json"


# ---------------------------------------------------------------------------
# ac://runs/pending
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_pending_runs() -> None:
    """ac://runs/pending returns the pending run queue."""
    mock_pending = [
        {
            "run_id": "label-batch0-a1b2",
            "issue_number": 0,
            "role": "cto",
            "branch": "agent/batch0-a1b2",
            "host_worktree_path": "/tmp/worktrees/batch0-a1b2",
            "batch_id": "label-batch0-20260301-a1b2",
        }
    ]
    with patch(
        "agentception.mcp.query_tools.get_pending_launches",
        new_callable=AsyncMock,
        return_value=mock_pending,
    ):
        result = await read_resource("ac://runs/pending")

    payload = _content(result)
    assert payload["count"] == 1
    pending = payload["pending"]
    assert isinstance(pending, list)
    first = pending[0]
    assert isinstance(first, dict)
    assert first["role"] == "cto"


# ---------------------------------------------------------------------------
# ac://runs/{run_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_run_returns_metadata() -> None:
    """ac://runs/{run_id} returns run metadata when the run exists."""
    mock_row = {
        "run_id": "issue-42",
        "status": "implementing",
        "role": "developer",
        "issue_number": 42,
        "pr_number": None,
        "branch": "ac/issue-42",
        "worktree_path": "/worktrees/issue-42",
        "batch_id": "batch-abc",
        "tier": "worker",
        "org_domain": "engineering",
        "parent_run_id": "issue-10",
        "spawned_at": "2026-03-01T00:00:00+00:00",
        "last_activity_at": None,
        "completed_at": None,
    }
    with patch("agentception.mcp.query_tools.get_run_by_id", new_callable=AsyncMock, return_value=mock_row):
        result = await read_resource("ac://runs/issue-42")

    payload = _content(result)
    assert payload["ok"] is True
    run = payload["run"]
    assert isinstance(run, dict)
    assert run["run_id"] == "issue-42"
    assert run["status"] == "implementing"


@pytest.mark.anyio
async def test_read_resource_run_returns_error_when_not_found() -> None:
    """ac://runs/{run_id} returns error payload when the run does not exist."""
    with patch("agentception.mcp.query_tools.get_run_by_id", new_callable=AsyncMock, return_value=None):
        result = await read_resource("ac://runs/issue-999")

    assert _content(result)["ok"] is False


# ---------------------------------------------------------------------------
# ac://runs/{run_id}/children
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_children_returns_list() -> None:
    """ac://runs/{run_id}/children returns child run list."""
    mock_children = [
        {
            "run_id": "issue-100",
            "status": "implementing",
            "role": "developer",
            "issue_number": 100,
            "pr_number": None,
            "branch": None,
            "worktree_path": None,
            "batch_id": "batch-abc",
            "tier": "worker",
            "org_domain": "engineering",
            "parent_run_id": "issue-42",
            "spawned_at": "2026-03-01T01:00:00+00:00",
            "last_activity_at": None,
            "completed_at": None,
        }
    ]
    with patch(
        "agentception.mcp.query_tools.get_children_by_parent_id",
        new_callable=AsyncMock,
        return_value=mock_children,
    ):
        result = await read_resource("ac://runs/issue-42/children")

    payload = _content(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    children = payload["children"]
    assert isinstance(children, list)
    first = children[0]
    assert isinstance(first, dict)
    assert first["run_id"] == "issue-100"


@pytest.mark.anyio
async def test_read_resource_children_empty_list() -> None:
    """ac://runs/{run_id}/children returns empty list when no children exist."""
    with patch(
        "agentception.mcp.query_tools.get_children_by_parent_id",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await read_resource("ac://runs/issue-42/children")

    payload = _content(result)
    assert payload["count"] == 0
    assert payload["children"] == []


# ---------------------------------------------------------------------------
# ac://runs/{run_id}/events
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_events_returns_events() -> None:
    """ac://runs/{run_id}/events returns the event log."""
    mock_events = [
        {"id": 1, "event_type": "step_start", "payload": "{}", "recorded_at": "2026-03-01T00:01:00+00:00"},
        {"id": 2, "event_type": "blocker", "payload": '{"msg":"waiting"}', "recorded_at": "2026-03-01T00:02:00+00:00"},
    ]
    with patch(
        "agentception.mcp.query_tools.get_agent_events_tail",
        new_callable=AsyncMock,
        return_value=mock_events,
    ):
        result = await read_resource("ac://runs/issue-42/events")

    payload = _content(result)
    assert payload["ok"] is True
    assert payload["count"] == 2


@pytest.mark.anyio
async def test_read_resource_events_pagination_via_query_string() -> None:
    """ac://runs/{run_id}/events?after_id=N passes after_id to the DB function."""
    with patch(
        "agentception.mcp.query_tools.get_agent_events_tail",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_fn:
        await read_resource("ac://runs/issue-42/events?after_id=5")

    mock_fn.assert_awaited_once_with("issue-42", after_id=5)


@pytest.mark.anyio
async def test_read_resource_events_default_after_id_is_zero() -> None:
    """ac://runs/{run_id}/events without after_id defaults to 0."""
    with patch(
        "agentception.mcp.query_tools.get_agent_events_tail",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_fn:
        await read_resource("ac://runs/issue-42/events")

    mock_fn.assert_awaited_once_with("issue-42", after_id=0)


# ---------------------------------------------------------------------------
# ac://batches/{batch_id}/tree
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_batch_tree_returns_nodes() -> None:
    """ac://batches/{batch_id}/tree returns all runs in the batch."""
    mock_nodes = [
        {
            "id": "issue-1",
            "role": "cto",
            "status": "implementing",
            "agent_status": "implementing",
            "tier": "coordinator",
            "org_domain": "c-suite",
            "parent_run_id": None,
            "issue_number": None,
            "pr_number": None,
            "batch_id": "batch-x",
            "spawned_at": "2026-03-01T00:00:00+00:00",
            "last_activity_at": None,
            "current_step": None,
        }
    ]
    with patch(
        "agentception.mcp.query_tools.get_run_tree_by_batch_id",
        new_callable=AsyncMock,
        return_value=mock_nodes,
    ):
        result = await read_resource("ac://batches/batch-x/tree")

    payload = _content(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    nodes = payload["nodes"]
    assert isinstance(nodes, list)
    first = nodes[0]
    assert isinstance(first, dict)
    assert first["id"] == "issue-1"


@pytest.mark.anyio
async def test_read_resource_batch_tree_unknown_uri_returns_not_found() -> None:
    """ac://batches/{batch_id} without /tree returns not-found error payload."""
    result = await read_resource("ac://batches/batch-x")
    payload = _content(result)
    assert "error" in payload


# ---------------------------------------------------------------------------
# ac://system/dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_dispatcher_state() -> None:
    """ac://system/dispatcher returns dispatcher state with status counts."""
    mock_counts = [
        {"status": "implementing", "count": 3},
        {"status": "completed", "count": 10},
    ]
    with (
        patch(
            "agentception.mcp.query_tools.get_run_status_counts",
            new_callable=AsyncMock,
            return_value=mock_counts,
        ),
        patch(
            "agentception.mcp.query_tools.get_latest_active_batch_id",
            new_callable=AsyncMock,
            return_value="batch-xyz",
        ),
    ):
        result = await read_resource("ac://system/dispatcher")

    payload = _content(result)
    assert payload["ok"] is True
    assert payload["active_count"] == 3
    assert payload["latest_batch_id"] == "batch-xyz"
    sc = payload["status_counts"]
    assert isinstance(sc, list)
    assert len(sc) == 2


# ---------------------------------------------------------------------------
# ac://system/health
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_system_health_db_up() -> None:
    """ac://system/health returns ok=True with db_ok=True when DB is reachable."""
    mock_counts = [{"status": "implementing", "count": 2}]
    with (
        patch(
            "agentception.mcp.query_tools.check_db_reachable",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agentception.mcp.query_tools.get_run_status_counts",
            new_callable=AsyncMock,
            return_value=mock_counts,
        ),
    ):
        result = await read_resource("ac://system/health")

    payload = _content(result)
    assert payload["ok"] is True
    assert payload["db_ok"] is True
    assert payload["total_runs"] == 2


@pytest.mark.anyio
async def test_read_resource_system_health_db_down() -> None:
    """Regression: ac://system/health returns db_ok=False when DB is unreachable.

    The resource must never raise — it degrades gracefully so supervisory
    agents can detect the outage without crashing.
    """
    with patch(
        "agentception.mcp.query_tools.check_db_reachable",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await read_resource("ac://system/health")

    payload = _content(result)
    assert payload["ok"] is True
    assert payload["db_ok"] is False
    assert payload["total_runs"] == 0
    assert payload["status_counts"] == []


# ---------------------------------------------------------------------------
# ac://plan/schema
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_plan_schema() -> None:
    """ac://plan/schema returns the PlanSpec JSON Schema."""
    result = await read_resource("ac://plan/schema")

    payload = _content(result)
    assert isinstance(payload, dict)
    assert "type" in payload or "$defs" in payload or "properties" in payload


# ---------------------------------------------------------------------------
# ac://plan/labels
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_plan_labels() -> None:
    """ac://plan/labels returns the GitHub label list."""
    mock_labels = {"labels": [{"name": "phase/1", "description": "Phase 1"}]}
    with patch(
        "agentception.mcp.resources.plan_get_labels",
        new_callable=AsyncMock,
        return_value=mock_labels,
    ):
        result = await read_resource("ac://plan/labels")

    payload = _content(result)
    assert "labels" in payload
    labels = payload["labels"]
    assert isinstance(labels, list)
    first_label = labels[0]
    assert isinstance(first_label, dict)
    assert first_label["name"] == "phase/1"


# ---------------------------------------------------------------------------
# ac://plan/figures/{role}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_plan_figures_known_role() -> None:
    """ac://plan/figures/{role} returns figure catalogue for a known role."""
    mock_result = {
        "role": "developer",
        "figures": [{"id": "fig-1", "display_name": "Flow State", "description": "Deep focus"}],
    }
    with patch(
        "agentception.mcp.resources.plan_get_cognitive_figures",
        return_value=mock_result,
    ):
        result = await read_resource("ac://plan/figures/developer")

    payload = _content(result)
    assert payload["role"] == "developer"
    figures = payload["figures"]
    assert isinstance(figures, list)
    assert len(figures) == 1


@pytest.mark.anyio
async def test_read_resource_plan_figures_unknown_role() -> None:
    """ac://plan/figures/{role} returns error payload for an unknown role."""
    mock_result = {"role": "unknown-role", "figures": [], "error": "Role not found"}
    with patch(
        "agentception.mcp.resources.plan_get_cognitive_figures",
        return_value=mock_result,
    ):
        result = await read_resource("ac://plan/figures/unknown-role")

    payload = _content(result)
    assert payload["role"] == "unknown-role"
    assert payload["figures"] == []


# ---------------------------------------------------------------------------
# URI edge cases
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_resource_unknown_domain_returns_not_found() -> None:
    """ac://unknowndomain/foo returns a not-found error payload."""
    result = await read_resource("ac://unknowndomain/foo")
    payload = _content(result)
    assert "error" in payload


@pytest.mark.anyio
async def test_read_resource_wrong_scheme_returns_error() -> None:
    """Non-ac:// URI scheme returns an error payload."""
    result = await read_resource("https://example.com/runs/active")
    payload = _content(result)
    assert "error" in payload


@pytest.mark.anyio
async def test_read_resource_empty_uri_returns_error() -> None:
    """Empty URI string returns an error payload gracefully."""
    result = await read_resource("")
    payload = _content(result)
    assert "error" in payload
