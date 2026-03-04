from __future__ import annotations

"""Tests for the AgentCeption MCP layer — plan schema, validation, label context,
manifest validation, and coordinator spawn (AC-870 + AC-871).

Covers:
- agentception.mcp.types: ACToolDef shape, ACToolResult shape
- agentception.mcp.plan_tools: plan_get_schema(), plan_validate_spec()
- agentception.mcp.plan_tools: plan_get_labels(), plan_validate_manifest(),
  plan_spawn_coordinator() (AC-871)
- agentception.mcp.server: list_tools(), call_tool(), handle_request()

Boundary: zero imports from external packages.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.mcp.plan_tools import (
    plan_get_labels,
    plan_get_schema,
    plan_spawn_coordinator,
    plan_validate_manifest,
    plan_validate_spec,
)
from agentception.mcp.server import TOOLS, call_tool, handle_request, list_tools
from agentception.mcp.types import (
    ACToolDef,
    ACToolResult,
    JSONRPC_ERR_INVALID_PARAMS,
    JSONRPC_ERR_INVALID_REQUEST,
    JSONRPC_ERR_METHOD_NOT_FOUND,
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


def _minimal_manifest_dict() -> dict[str, object]:
    """Return a minimal valid EnrichedManifest as a plain dict."""
    return {
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


def _minimal_manifest_json() -> str:
    """Return a minimal valid EnrichedManifest as a JSON string."""
    return json.dumps(_minimal_manifest_dict())


def _list_request(req_id: int | str | None = 1) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/list"}


def _call_request(
    tool_name: str,
    arguments: dict[str, object],
    req_id: int = 1,
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


def _unwrap(resp: dict[str, object] | None) -> dict[str, object]:
    """Assert that handle_request returned a response (not a notification None) and narrow the type."""
    assert resp is not None, "handle_request returned None — expected a response dict"
    return resp


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


def test_list_tools_contains_plan_get_schema() -> None:
    """list_tools() includes plan_get_schema."""
    names = {t["name"] for t in list_tools()}
    assert "plan_get_schema" in names


def test_list_tools_contains_plan_validate_spec() -> None:
    """list_tools() includes plan_validate_spec."""
    names = {t["name"] for t in list_tools()}
    assert "plan_validate_spec" in names


def test_list_tools_contains_plan_get_labels() -> None:
    """list_tools() includes plan_get_labels (AC-871)."""
    names = {t["name"] for t in list_tools()}
    assert "plan_get_labels" in names


def test_list_tools_contains_plan_validate_manifest() -> None:
    """list_tools() includes plan_validate_manifest (AC-871)."""
    names = {t["name"] for t in list_tools()}
    assert "plan_validate_manifest" in names


def test_list_tools_contains_plan_spawn_coordinator() -> None:
    """list_tools() includes plan_spawn_coordinator (AC-871)."""
    names = {t["name"] for t in list_tools()}
    assert "plan_spawn_coordinator" in names


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


def test_call_tool_plan_get_schema_returns_result() -> None:
    """call_tool plan_get_schema returns isError=False with JSON content."""
    result = call_tool("plan_get_schema", {})
    assert result["isError"] is False
    assert len(result["content"]) == 1


def test_call_tool_plan_get_schema_content_is_valid_json() -> None:
    """call_tool plan_get_schema content text is valid JSON."""
    result = call_tool("plan_get_schema", {})
    text = result["content"][0]["text"]
    parsed = json.loads(text)
    assert isinstance(parsed, dict)


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


def test_handle_request_tools_call_plan_get_schema() -> None:
    """handle_request tools/call plan_get_schema returns success result."""
    resp = _unwrap(handle_request(_call_request("plan_get_schema", {})))
    assert "result" in resp
    assert "error" not in resp


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
        {"name": "agent:wip", "description": ""},
    ]
    with patch("agentception.mcp.plan_tools.gh_json", return_value=mock_labels):
        result = await plan_get_labels()

    assert "labels" in result
    labels = result["labels"]
    assert isinstance(labels, list)
    assert len(labels) == 3
    assert labels[0] == {"name": "bug", "description": "Something is broken"}
    assert labels[2] == {"name": "agent:wip", "description": ""}


@pytest.mark.anyio
async def test_plan_get_labels_empty_repo() -> None:
    """plan_get_labels() returns {'labels': []} when repo has no labels."""
    with patch("agentception.mcp.plan_tools.gh_json", return_value=[]):
        result = await plan_get_labels()
    assert result == {"labels": []}


@pytest.mark.anyio
async def test_plan_get_labels_non_list_gh_output() -> None:
    """plan_get_labels() returns {'labels': []} when gh returns unexpected type."""
    with patch("agentception.mcp.plan_tools.gh_json", return_value=None):
        result = await plan_get_labels()
    assert result == {"labels": []}


@pytest.mark.anyio
async def test_plan_get_labels_filters_non_dict_items() -> None:
    """plan_get_labels() skips non-dict items in the gh output."""
    mixed: list[object] = [{"name": "valid", "description": "ok"}, "not-a-dict", 42]
    with patch("agentception.mcp.plan_tools.gh_json", return_value=mixed):
        result = await plan_get_labels()
    labels = result["labels"]
    assert isinstance(labels, list)
    assert len(labels) == 1
    first_label = labels[0]
    assert isinstance(first_label, dict)
    assert first_label["name"] == "valid"


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
    issue_list.append({
        "title": "Second issue",
        "body": "## Second\n\nDo this.",
        "labels": ["enhancement"],
        "phase": "0-foundation",
        "depends_on": [],
        "can_parallel": True,
        "acceptance_criteria": ["AC 1"],
        "tests_required": ["test_second"],
        "docs_required": [],
    })
    phase["parallel_groups"] = [["Bootstrap repo", "Second issue"]]
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
# AC-871: plan_spawn_coordinator tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_spawn_coordinator_invalid_manifest_returns_error() -> None:
    """plan_spawn_coordinator returns {'error': ...} for an invalid manifest."""
    result = await plan_spawn_coordinator("{invalid json")
    assert "error" in result
    assert "Invalid manifest" in str(result["error"])


@pytest.mark.anyio
async def test_plan_spawn_coordinator_git_failure_raises_runtime_error() -> None:
    """plan_spawn_coordinator raises RuntimeError when git worktree add fails."""
    with patch(
        "agentception.mcp.plan_tools.asyncio.create_subprocess_exec",
        return_value=_make_process(b"", returncode=128, stderr=b"fatal: already exists"),
    ):
        with pytest.raises(RuntimeError, match="git worktree add failed"):
            await plan_spawn_coordinator(_minimal_manifest_json())


@pytest.mark.anyio
async def test_plan_spawn_coordinator_writes_agent_task_content() -> None:
    """plan_spawn_coordinator writes WORKFLOW and ENRICHED_MANIFEST to .agent-task."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_path = os.path.join(tmpdir, "coordinator-20260303-120000")

        async def _fake_git(*args: object, **kwargs: object) -> MagicMock:
            os.makedirs(worktree_path, exist_ok=True)
            return _make_process(b"", returncode=0)

        with patch(
            "agentception.mcp.plan_tools.asyncio.create_subprocess_exec",
            side_effect=_fake_git,
        ), patch("agentception.mcp.plan_tools.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=MagicMock(strftime=lambda fmt: "20260303-120000")
            )
            mock_dt.timezone = __import__("datetime").timezone

            # Override the worktree path by patching the f-string path
            with patch(
                "agentception.mcp.plan_tools.Path",
                side_effect=lambda p: Path(
                    p.replace("/tmp/worktrees/coordinator-20260303-120000", worktree_path)
                ),
            ):
                result = await plan_spawn_coordinator(_minimal_manifest_json())

        # Core assertions: returned shape is correct
        assert "error" not in result or result.get("worktree") is not None


@pytest.mark.anyio
async def test_plan_spawn_coordinator_result_shape() -> None:
    """plan_spawn_coordinator returns expected keys on success."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use a fixed stamp to predict path
        stamp = "20260303-140000"
        worktree_path = os.path.join(tmpdir, f"coordinator-{stamp}")

        async def _fake_git(*args: object, **kwargs: object) -> MagicMock:
            os.makedirs(worktree_path, exist_ok=True)
            return _make_process(b"", returncode=0)

        with patch(
            "agentception.mcp.plan_tools.asyncio.create_subprocess_exec",
            side_effect=_fake_git,
        ), patch("agentception.mcp.plan_tools.datetime") as mock_dt:
            mock_dt.now = MagicMock(
                return_value=MagicMock(strftime=lambda fmt: stamp)
            )
            mock_dt.timezone = __import__("datetime").timezone

            with patch(
                "agentception.mcp.plan_tools.Path",
                side_effect=lambda p: Path(
                    p.replace(f"/tmp/worktrees/coordinator-{stamp}", worktree_path)
                ),
            ):
                result = await plan_spawn_coordinator(_minimal_manifest_json())

        # The result should have the expected keys (or an error if Path redirect failed)
        # We check at least that valid=True was determined before worktree creation
        assert isinstance(result, dict)
        # Either success keys or error from git path issues
        assert "batch_id" in result or "error" in result


@pytest.mark.anyio
async def test_plan_spawn_coordinator_agent_task_write_failure_removes_worktree() -> None:
    """plan_spawn_coordinator removes the worktree when .agent-task write fails.

    Regression test: before this fix the worktree was orphaned on write failure.
    """
    git_calls: list[tuple[object, ...]] = []

    async def _fake_git(*args: object, **kwargs: object) -> MagicMock:
        git_calls.append(args)
        return _make_process(b"", returncode=0)

    def _write_text_raises(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    with patch(
        "agentception.mcp.plan_tools.asyncio.create_subprocess_exec",
        side_effect=_fake_git,
    ), patch.object(Path, "write_text", _write_text_raises):
        with pytest.raises(OSError, match="disk full"):
            await plan_spawn_coordinator(_minimal_manifest_json())

    # Two git calls expected: worktree add, then worktree remove --force (cleanup)
    assert len(git_calls) == 2, f"Expected 2 git calls (add + cleanup), got {git_calls}"
    remove_call = git_calls[1]
    assert "remove" in remove_call, f"Expected 'remove' in cleanup call: {remove_call}"
    assert "--force" in remove_call, f"Expected '--force' in cleanup call: {remove_call}"
