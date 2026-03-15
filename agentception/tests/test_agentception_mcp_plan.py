from __future__ import annotations

"""Tests for the AgentCeption MCP layer — plan schema, validation, label context,
and manifest validation (AC-870 + AC-871).

Covers:
- agentception.mcp.types: ACToolDef, ACToolResult, ACResourceDef, ACResourceResult shapes
- agentception.mcp.plan_tools: plan_get_schema(), plan_validate_spec()
- agentception.mcp.plan_tools: plan_get_labels(), plan_validate_manifest()
- agentception.mcp.server: list_tools(), list_resources(), call_tool(), handle_request()
- Resources: plan_get_schema and plan_get_labels are now ac:// Resources, not Tools

Boundary: zero imports from external packages.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.mcp.plan_tools import (
    plan_get_labels,
    plan_get_schema,
    plan_validate_manifest,
    plan_validate_spec,
)
from agentception.mcp.server import (
    TOOLS,
    call_tool,
    call_tool_async,
    handle_request,
    list_resources,
    list_tools,
)
from agentception.types import JsonValue
from agentception.mcp.types import (
    ACResourceDef,
    ACResourceResult,
    ACToolDef,
    ACToolResult,
    JSONRPC_ERR_INVALID_PARAMS,
    JSONRPC_ERR_INVALID_REQUEST,
    JSONRPC_ERR_METHOD_NOT_FOUND,
    JsonRpcErrorResponse,
    JsonRpcSuccessResponse,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _minimal_plan_spec_json() -> str:
    """Return a minimal valid PlanSpec as a JSON string."""
    return json.dumps({
        "initiative": "smoke-test",
        "phases": [
            {
                "label": "0-foundation",
                "description": "Foundation",
                "depends_on": [],
                "issues": [
                    {
                        "id": "smoke-test-p0-001",
                        "title": "Bootstrap the repo",
                        "body": "Set up the project.",
                        "depends_on": [],
                    }
                ],
            }
        ],
    })


def _minimal_manifest_dict() -> dict[str, JsonValue]:
    """Return a minimal valid EnrichedManifest as a plain dict."""
    raw = {
        "initiative": "test-init",
        "phases": [
            {
                "label": "0-foundation",
                "description": "Foundation phase",
                "depends_on": [],
                "issues": [
                    {
                        "title": "Bootstrap repo",
                        "body": "## Bootstrap\n\nSet up the project.",
                        "labels": ["enhancement"],
                        "phase": "0-foundation",
                        "depends_on": [],
                        "can_parallel": True,
                        "acceptance_criteria": ["Repo is set up"],
                        "tests_required": ["test_bootstrap"],
                        "docs_required": ["docs/setup.md"],
                    }
                ],
                "parallel_groups": [["Bootstrap repo"]],
            }
        ],
    }
    result: dict[str, JsonValue] = json.loads(json.dumps(raw))
    return result


def _minimal_manifest_json() -> str:
    """Return a minimal valid EnrichedManifest as a JSON string."""
    return json.dumps(_minimal_manifest_dict())


def _list_request(req_id: int | str | None = 1) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/list"}


def _call_request(
    tool_name: str,
    arguments: dict[str, JsonValue],
    req_id: int = 1,
) -> dict[str, JsonValue]:
    params: dict[str, JsonValue] = {"name": tool_name, "arguments": arguments}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": params,
    }


def _unwrap(resp: JsonRpcSuccessResponse | JsonRpcErrorResponse | None) -> dict[str, JsonValue]:
    """Assert that handle_request returned a response (not a notification None) and narrow the type."""
    assert resp is not None, "handle_request returned None — expected a response dict"
    d: dict[str, JsonValue] = json.loads(json.dumps(resp))
    return d


def _make_process(stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# ACToolDef / ACToolResult shape tests
# ---------------------------------------------------------------------------


def test_ac_tool_def_has_required_keys() -> None:
    """ACToolDef TypedDict accepts all required keys."""
    tool: ACToolDef = ACToolDef(
        name="plan_get_schema",
        description="desc",
        inputSchema={"type": "object", "properties": {}},
    )
    assert tool["name"] == "plan_get_schema"
    assert "inputSchema" in tool


def test_ac_tool_result_has_required_keys() -> None:
    """ACToolResult TypedDict accepts all required keys."""
    result: ACToolResult = ACToolResult(
        content=[{"type": "text", "text": "{}"}],
        isError=False,
    )
    assert result["isError"] is False
    assert len(result["content"]) == 1


# ---------------------------------------------------------------------------
# plan_get_schema
# ---------------------------------------------------------------------------


def test_plan_get_schema_returns_dict() -> None:
    """plan_get_schema() returns a non-empty dict."""
    schema = plan_get_schema()
    assert isinstance(schema, dict)
    assert len(schema) > 0


def test_plan_get_schema_has_title() -> None:
    """plan_get_schema() result contains a top-level 'title' key."""
    schema = plan_get_schema()
    assert "title" in schema


def test_plan_get_schema_has_required_fields() -> None:
    """plan_get_schema() result contains a 'required' key listing mandatory fields."""
    schema = plan_get_schema()
    assert "required" in schema
    required = schema["required"]
    assert isinstance(required, list)
    assert "initiative" in required
    assert "phases" in required


def test_plan_get_schema_has_properties() -> None:
    """plan_get_schema() result contains 'properties' for known PlanSpec fields."""
    schema = plan_get_schema()
    props = schema.get("properties")
    assert isinstance(props, dict)
    assert "initiative" in props
    assert "phases" in props


def test_plan_get_schema_is_cached() -> None:
    """Calling plan_get_schema() twice returns the same dict object (module-level cache)."""
    first = plan_get_schema()
    second = plan_get_schema()
    assert first is second


# ---------------------------------------------------------------------------
# plan_validate_spec
# ---------------------------------------------------------------------------


def test_plan_validate_spec_valid_minimal() -> None:
    """plan_validate_spec returns valid=True for a minimal well-formed PlanSpec."""
    result = plan_validate_spec(_minimal_plan_spec_json())
    assert result.get("valid") is True
    assert "spec" in result


def test_plan_validate_spec_valid_returns_spec_dict() -> None:
    """plan_validate_spec 'spec' key contains an initiative string."""
    result = plan_validate_spec(_minimal_plan_spec_json())
    spec = result.get("spec")
    assert isinstance(spec, dict)
    assert spec.get("initiative") == "smoke-test"


def test_plan_validate_spec_valid_multi_phase() -> None:
    """plan_validate_spec accepts a multi-phase PlanSpec with valid DAG."""
    data = {
        "initiative": "multi",
        "phases": [
            {
                "label": "0-a",
                "description": "Phase A",
                "depends_on": [],
                "issues": [{"id": "multi-p0-001", "title": "A issue", "body": "Do A.", "depends_on": []}],
            },
            {
                "label": "1-b",
                "description": "Phase B",
                "depends_on": ["0-a"],
                "issues": [{"id": "multi-p1-001", "title": "B issue", "body": "Do B.", "depends_on": []}],
            },
        ],
    }
    result = plan_validate_spec(json.dumps(data))
    assert result.get("valid") is True


def test_plan_validate_spec_invalid_json_syntax() -> None:
    """plan_validate_spec returns valid=False for malformed JSON."""
    result = plan_validate_spec("{not valid json")
    assert result.get("valid") is False
    errors = result.get("errors")
    assert isinstance(errors, list)
    assert len(errors) > 0
    assert any("JSON parse error" in str(e) for e in errors)


def test_plan_validate_spec_empty_string() -> None:
    """plan_validate_spec returns valid=False for an empty string."""
    result = plan_validate_spec("")
    assert result.get("valid") is False


def test_plan_validate_spec_missing_initiative() -> None:
    """plan_validate_spec rejects a PlanSpec missing the initiative field."""
    data = {
        "phases": [
            {
                "label": "0-a",
                "description": "Phase A",
                "depends_on": [],
                "issues": [{"title": "A issue", "body": "Do A.", "depends_on": []}],
            }
        ]
    }
    result = plan_validate_spec(json.dumps(data))
    assert result.get("valid") is False
    assert isinstance(result.get("errors"), list)


def test_plan_validate_spec_missing_phases() -> None:
    """plan_validate_spec rejects a PlanSpec missing the phases field."""
    result = plan_validate_spec(json.dumps({"initiative": "orphan"}))
    assert result.get("valid") is False


def test_plan_validate_spec_empty_phases() -> None:
    """plan_validate_spec rejects an empty phases list."""
    result = plan_validate_spec(json.dumps({"initiative": "empty", "phases": []}))
    assert result.get("valid") is False


def test_plan_validate_spec_forward_phase_dep() -> None:
    """plan_validate_spec rejects a phase that depends_on a later phase label."""
    data = {
        "initiative": "bad-dep",
        "phases": [
            {
                "label": "0-a",
                "description": "A",
                "depends_on": ["1-b"],  # forward reference — invalid
                "issues": [{"title": "A", "body": "b", "depends_on": []}],
            },
            {
                "label": "1-b",
                "description": "B",
                "depends_on": [],
                "issues": [{"title": "B", "body": "b", "depends_on": []}],
            },
        ],
    }
    result = plan_validate_spec(json.dumps(data))
    assert result.get("valid") is False


def test_plan_validate_spec_errors_is_list_of_strings() -> None:
    """plan_validate_spec 'errors' value is always a list of strings."""
    result = plan_validate_spec(json.dumps({"initiative": "x"}))
    assert result.get("valid") is False
    errors = result.get("errors")
    assert isinstance(errors, list)
    assert all(isinstance(e, str) for e in errors)


# ---------------------------------------------------------------------------
# list_tools / TOOLS registry
# ---------------------------------------------------------------------------


def test_list_tools_returns_non_empty_list() -> None:
    """list_tools() returns a non-empty list of ACToolDef objects."""
    tools = list_tools()
    assert isinstance(tools, list)
    assert len(tools) > 0


def test_plan_get_schema_is_resource_not_tool() -> None:
    """plan_get_schema is exposed as ac://plan/schema Resource, not as a Tool."""
    tool_names = {t["name"] for t in list_tools()}
    assert "plan_get_schema" not in tool_names
    resource_uris = {r["uri"] for r in list_resources()}
    assert "ac://plan/schema" in resource_uris


def test_list_tools_contains_plan_validate_spec() -> None:
    """list_tools() includes plan_validate_spec."""
    names = {t["name"] for t in list_tools()}
    assert "plan_validate_spec" in names


def test_plan_get_labels_is_resource_not_tool() -> None:
    """plan_get_labels is exposed as ac://plan/labels Resource, not as a Tool (AC-871)."""
    tool_names = {t["name"] for t in list_tools()}
    assert "plan_get_labels" not in tool_names
    resource_uris = {r["uri"] for r in list_resources()}
    assert "ac://plan/labels" in resource_uris


def test_list_tools_contains_plan_validate_manifest() -> None:
    """list_tools() includes plan_validate_manifest (AC-871)."""
    names = {t["name"] for t in list_tools()}
    assert "plan_validate_manifest" in names



def test_list_tools_all_have_required_keys() -> None:
    """Every tool in list_tools() has name, description, inputSchema."""
    for tool in list_tools():
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool


def test_list_tools_input_schema_is_object_type() -> None:
    """Every tool's inputSchema has type='object'."""
    for tool in list_tools():
        schema = tool["inputSchema"]
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"


def test_tools_module_constant_matches_list_tools() -> None:
    """TOOLS constant and list_tools() return equivalent tool lists."""
    assert [t["name"] for t in TOOLS] == [t["name"] for t in list_tools()]


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------


def test_call_tool_plan_validate_spec_valid_returns_no_error() -> None:
    """call_tool plan_validate_spec with a valid spec returns isError=False."""
    result = call_tool("plan_validate_spec", {"spec_json": _minimal_plan_spec_json()})
    assert result["isError"] is False


def test_call_tool_plan_validate_spec_invalid_returns_error() -> None:
    """call_tool plan_validate_spec with bad JSON returns isError=True."""
    result = call_tool("plan_validate_spec", {"spec_json": "{bad}"})
    assert result["isError"] is True


def test_call_tool_plan_validate_spec_missing_arg_returns_error() -> None:
    """call_tool plan_validate_spec without spec_json argument returns isError=True."""
    result = call_tool("plan_validate_spec", {})
    assert result["isError"] is True


def test_call_tool_plan_validate_manifest_valid() -> None:
    """call_tool plan_validate_manifest with a valid manifest returns isError=False."""
    result = call_tool("plan_validate_manifest", {"json_text": _minimal_manifest_json()})
    assert result["isError"] is False


def test_call_tool_plan_validate_manifest_invalid() -> None:
    """call_tool plan_validate_manifest with bad JSON returns isError=True."""
    result = call_tool("plan_validate_manifest", {"json_text": "{not json"})
    assert result["isError"] is True


def test_call_tool_plan_validate_manifest_missing_arg_returns_error() -> None:
    """call_tool plan_validate_manifest without json_text returns isError=True."""
    result = call_tool("plan_validate_manifest", {})
    assert result["isError"] is True


def test_call_tool_unknown_returns_error() -> None:
    """call_tool for an unknown tool name returns isError=True."""
    result = call_tool("nonexistent_tool", {})
    assert result["isError"] is True


# ---------------------------------------------------------------------------
# handle_request tests — tools/list
# ---------------------------------------------------------------------------


def test_handle_request_tools_list_success() -> None:
    """handle_request tools/list returns a success response with result."""
    resp = _unwrap(handle_request(_list_request()))
    assert "result" in resp
    assert "error" not in resp


def test_handle_request_tools_list_result_has_tools_key() -> None:
    """handle_request tools/list result contains a 'tools' list."""
    resp = _unwrap(handle_request(_list_request()))
    result = resp.get("result")
    assert isinstance(result, dict)
    assert "tools" in result
    tools = result["tools"]
    assert isinstance(tools, list)
    assert len(tools) > 0


def test_handle_request_tools_list_preserves_request_id() -> None:
    """handle_request tools/list echoes back the integer request id."""
    resp = _unwrap(handle_request(_list_request(req_id=42)))
    assert resp["id"] == 42


def test_handle_request_tools_list_string_id() -> None:
    """handle_request works with string request IDs."""
    resp = _unwrap(handle_request(_list_request(req_id="abc-123")))
    assert resp["id"] == "abc-123"


# ---------------------------------------------------------------------------
# handle_request tests — tools/call
# ---------------------------------------------------------------------------


def test_handle_request_tools_call_plan_get_schema_returns_redirect() -> None:
    """handle_request tools/call plan_get_schema returns redirect error (now a Resource)."""
    resp = _unwrap(handle_request(_call_request("plan_get_schema", {})))
    assert "result" in resp
    result = resp["result"]
    assert isinstance(result, dict)
    assert result.get("isError") is True
    content = result["content"]
    assert isinstance(content, list)
    first = content[0]
    assert isinstance(first, dict)
    text = first["text"]
    assert isinstance(text, str)
    payload: dict[str, JsonValue] = json.loads(text)
    assert "ac://plan/schema" in str(payload["error"])


def test_handle_request_tools_call_plan_validate_spec_valid() -> None:
    """handle_request tools/call plan_validate_spec with valid spec succeeds."""
    resp = _unwrap(handle_request(
        _call_request("plan_validate_spec", {"spec_json": _minimal_plan_spec_json()})
    ))
    assert "result" in resp
    result = resp.get("result")
    assert isinstance(result, dict)
    assert result.get("isError") is False


def test_handle_request_tools_call_plan_validate_spec_invalid() -> None:
    """handle_request tools/call plan_validate_spec with bad spec returns isError=True."""
    resp = _unwrap(handle_request(_call_request("plan_validate_spec", {"spec_json": "{bad}"})))
    assert "result" in resp
    result = resp.get("result")
    assert isinstance(result, dict)
    assert result.get("isError") is True


# ---------------------------------------------------------------------------
# handle_request tests — error cases
# ---------------------------------------------------------------------------


def test_handle_request_wrong_jsonrpc_version() -> None:
    """handle_request returns INVALID_REQUEST for wrong jsonrpc version."""
    resp = _unwrap(handle_request({"jsonrpc": "1.0", "id": 1, "method": "tools/list"}))
    assert "error" in resp
    error = resp["error"]
    assert isinstance(error, dict)
    assert error["code"] == JSONRPC_ERR_INVALID_REQUEST


def test_handle_request_missing_jsonrpc_field() -> None:
    """handle_request returns INVALID_REQUEST when jsonrpc is absent."""
    resp = _unwrap(handle_request({"id": 1, "method": "tools/list"}))
    assert "error" in resp
    error = resp["error"]
    assert isinstance(error, dict)
    assert error["code"] == JSONRPC_ERR_INVALID_REQUEST


def test_handle_request_missing_method() -> None:
    """handle_request returns INVALID_REQUEST when method is absent."""
    resp = _unwrap(handle_request({"jsonrpc": "2.0", "id": 1}))
    assert "error" in resp
    error = resp["error"]
    assert isinstance(error, dict)
    assert error["code"] == JSONRPC_ERR_INVALID_REQUEST


def test_handle_request_unknown_method() -> None:
    """handle_request returns METHOD_NOT_FOUND for an unregistered method."""
    resp = _unwrap(handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/unknown"}))
    assert "error" in resp
    error = resp["error"]
    assert isinstance(error, dict)
    assert error["code"] == JSONRPC_ERR_METHOD_NOT_FOUND


def test_handle_request_tools_call_missing_params() -> None:
    """handle_request returns INVALID_PARAMS when params is missing for tools/call."""
    resp = _unwrap(handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}))
    assert "error" in resp
    error = resp["error"]
    assert isinstance(error, dict)
    assert error["code"] == JSONRPC_ERR_INVALID_PARAMS


def test_handle_request_tools_call_missing_name() -> None:
    """handle_request returns INVALID_PARAMS when params.name is missing."""
    resp = _unwrap(handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"arguments": {}}}
    ))
    assert "error" in resp
    error = resp["error"]
    assert isinstance(error, dict)
    assert error["code"] == JSONRPC_ERR_INVALID_PARAMS


def test_handle_request_null_id_is_preserved() -> None:
    """handle_request preserves id=null (None) per JSON-RPC 2.0 spec."""
    resp = _unwrap(handle_request({"jsonrpc": "2.0", "id": None, "method": "tools/list"}))
    assert resp["id"] is None


def test_handle_request_returns_dict() -> None:
    """handle_request always returns a dict regardless of input."""
    resp = handle_request(_list_request())
    assert isinstance(resp, dict)


# ---------------------------------------------------------------------------
# AC-871: plan_get_labels tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_get_labels_returns_label_list() -> None:
    """plan_get_labels() returns {'labels': [...]} with name/description entries."""
    mock_labels = [
        {"name": "bug", "description": "Something is broken"},
        {"name": "enhancement", "description": "New feature"},
        {"name": "agent/wip", "description": ""},
    ]
    with patch(
        "agentception.mcp.plan_tools.get_repo_labels",
        new=AsyncMock(return_value=mock_labels),
    ):
        result = await plan_get_labels()

    assert "labels" in result
    labels = result["labels"]
    assert isinstance(labels, list)
    assert len(labels) == 3
    assert labels[0] == {"name": "bug", "description": "Something is broken"}
    assert labels[2] == {"name": "agent/wip", "description": ""}


@pytest.mark.anyio
async def test_plan_get_labels_empty_repo() -> None:
    """plan_get_labels() returns {'labels': []} when repo has no labels."""
    with patch(
        "agentception.mcp.plan_tools.get_repo_labels",
        new=AsyncMock(return_value=[]),
    ):
        result = await plan_get_labels()
    assert result == {"labels": []}


@pytest.mark.anyio
async def test_plan_get_labels_filters_non_dict_items() -> None:
    """plan_get_labels() skips items without a name field."""
    mixed: list[dict[str, JsonValue]] = [
        {"name": "valid", "description": "ok"},
        {"description": "missing name"},
    ]
    with patch(
        "agentception.mcp.plan_tools.get_repo_labels",
        new=AsyncMock(return_value=mixed),
    ):
        result = await plan_get_labels()
    labels = result["labels"]
    assert isinstance(labels, list)
    assert len(labels) == 2  # both are dicts, name defaults to ""


# ---------------------------------------------------------------------------
# AC-871: plan_validate_manifest tests
# ---------------------------------------------------------------------------


def test_plan_validate_manifest_valid_json() -> None:
    """plan_validate_manifest returns valid=True for a correct EnrichedManifest."""
    result = plan_validate_manifest(_minimal_manifest_json())
    assert result.get("valid") is True
    assert result.get("total_issues") == 1
    waves = result.get("estimated_waves")
    assert isinstance(waves, int)
    assert waves >= 1
    assert "manifest" in result


def test_plan_validate_manifest_invalid_json_syntax() -> None:
    """plan_validate_manifest rejects malformed JSON."""
    result = plan_validate_manifest("{not valid json")
    assert result.get("valid") is False
    errors = result.get("errors")
    assert isinstance(errors, list)
    assert len(errors) > 0
    assert any("JSON parse error" in str(e) for e in errors)


def test_plan_validate_manifest_invalid_schema() -> None:
    """plan_validate_manifest rejects JSON that fails EnrichedManifest validation."""
    bad = json.dumps({"initiative": "bad", "phases": []})
    result = plan_validate_manifest(bad)
    assert result.get("valid") is False
    errors = result.get("errors")
    assert isinstance(errors, list)
    assert len(errors) > 0


def test_plan_validate_manifest_computed_fields_authoritative() -> None:
    """total_issues and estimated_waves are always computed, never caller-supplied."""
    manifest = _minimal_manifest_dict()
    manifest["total_issues"] = 999
    manifest["estimated_waves"] = 42
    result = plan_validate_manifest(json.dumps(manifest))
    assert result.get("valid") is True
    assert result.get("total_issues") == 1
    assert result.get("estimated_waves") == 1


def test_plan_validate_manifest_multi_issue_total() -> None:
    """total_issues reflects actual number of issues across all phases."""
    manifest = _minimal_manifest_dict()
    phases = manifest["phases"]
    assert isinstance(phases, list)
    phase = phases[0]
    assert isinstance(phase, dict)
    issue_list = phase["issues"]
    assert isinstance(issue_list, list)
    second: dict[str, JsonValue] = json.loads(json.dumps({
        "title": "Second issue",
        "body": "## Second\n\nDo this.",
        "labels": ["enhancement"],
        "phase": "0-foundation",
        "depends_on": [],
        "can_parallel": True,
        "acceptance_criteria": ["AC 1"],
        "tests_required": ["test_second"],
        "docs_required": [],
    }))
    issue_list.append(second)
    groups: list[JsonValue] = [["Bootstrap repo", "Second issue"]]
    phase["parallel_groups"] = groups
    result = plan_validate_manifest(json.dumps(manifest))
    assert result.get("valid") is True
    assert result.get("total_issues") == 2


def test_plan_validate_manifest_errors_is_list_of_strings() -> None:
    """plan_validate_manifest 'errors' is always a list of strings."""
    result = plan_validate_manifest(json.dumps({"phases": []}))
    assert result.get("valid") is False
    errors = result.get("errors")
    assert isinstance(errors, list)
    assert all(isinstance(e, str) for e in errors)


# ---------------------------------------------------------------------------
# build_acknowledge_run — MCP tool dispatches to acknowledge_agent_run
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_claim_run_success_via_call_tool() -> None:
    """build_claim_run MCP tool returns ok=true on successful claim.

    Regression: before this tool existed the Dispatcher fell back to curl;
    then build_acknowledge_run was introduced; now renamed to build_claim_run.
    """
    with patch(
        "agentception.mcp.build_commands.acknowledge_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await call_tool_async("build_claim_run", {"run_id": "test-run-abc123"})

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["run_id"] == "test-run-abc123"


@pytest.mark.anyio
async def test_build_claim_run_already_claimed_via_call_tool() -> None:
    """build_claim_run returns isError=True when run was already claimed."""
    with patch(
        "agentception.mcp.build_commands.acknowledge_agent_run",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await call_tool_async("build_claim_run", {"run_id": "test-run-already"})

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "reason" in payload


@pytest.mark.anyio
async def test_build_claim_run_missing_run_id_returns_error() -> None:
    """build_claim_run MCP tool returns isError=True when run_id is absent."""
    result = await call_tool_async("build_claim_run", {})

    assert result["isError"] is True


def test_build_claim_run_in_tools_list() -> None:
    """build_claim_run is present in the TOOLS registry."""
    names = [t["name"] for t in TOOLS]
    assert "build_claim_run" in names
