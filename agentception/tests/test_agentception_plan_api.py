from __future__ import annotations

"""Tests for POST /api/plan/draft (issue #872) and POST /api/plan/launch (issue #873).

POST /api/plan/draft covers:
- Valid text returns 200 with status=pending and a uuid4 draft_id.
- Empty text returns 422.
- Whitespace-only text returns 422.
- After a valid POST the .agent-task file is written with WORKFLOW=plan-spec
  and the plan text.
- asyncio.create_subprocess_exec is called with ``git worktree add``.

POST /api/plan/launch covers:
- Valid EnrichedManifest YAML → 200 with worktree, branch, agent_task_path, batch_id.
- Malformed YAML (syntax error) → 422 with error detail.
- YAML that fails EnrichedManifest validation → 422 with error detail.
- YAML with cyclic issue depends_on → 422 with cycle description.
- plan_spawn_coordinator called with correct manifest JSON.

All git subprocess calls and plan_spawn_coordinator are mocked so these tests
do not require a live git repository or network access.

Boundary: zero imports from external packages.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentception.app import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc_mock(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a mock that behaves like an asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Async httpx client wrapping the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# POST /api/plan/draft — happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_valid_dump_returns_200_pending(
    async_client: AsyncClient,
    tmp_path: Path,
) -> None:
    """POST with valid plan text must return 200 and status='pending'."""
    proc_mock = _make_proc_mock(returncode=0)

    with (
        patch(
            "agentception.routes.api.plan.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ) as mock_exec,
        patch(
            "agentception.routes.api.plan.settings.worktrees_dir",
            tmp_path,
        ),
        patch(
            "agentception.routes.api.plan.settings.host_worktrees_dir",
            tmp_path,
        ),
    ):
        response = await async_client.post(
            "/api/plan/draft",
            json={"text": "I want a song about mountains"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["draft_id"]
    # draft_id must be a valid uuid4
    parsed = uuid.UUID(body["draft_id"], version=4)
    assert str(parsed) == body["draft_id"]
    # output_path must be a specific file (not the directory) so the poller
    # can watch for it and emit plan_draft_ready when it appears.
    assert "plan-draft-" in body["output_path"]
    assert body["output_path"].endswith(".plan-output.yaml")
    assert body["task_file"].endswith(".agent-task")
    mock_exec.assert_called_once()


@pytest.mark.anyio
async def test_agent_task_written_with_workflow_plan_spec(
    async_client: AsyncClient,
    tmp_path: Path,
) -> None:
    """After a valid POST the .agent-task must contain WORKFLOW=plan-spec and the plan text."""
    plan_text = "Build a calm lo-fi track with piano and soft drums"
    proc_mock = _make_proc_mock(returncode=0)

    with (
        patch(
            "agentception.routes.api.plan.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ),
        patch(
            "agentception.routes.api.plan.settings.worktrees_dir",
            tmp_path,
        ),
        patch(
            "agentception.routes.api.plan.settings.host_worktrees_dir",
            tmp_path,
        ),
    ):
        response = await async_client.post(
            "/api/plan/draft",
            json={"text": plan_text},
        )

    assert response.status_code == 200
    body = response.json()
    task_file = Path(body["task_file"])
    assert task_file.exists(), ".agent-task file was not created"

    content = task_file.read_text(encoding="utf-8")
    assert "WORKFLOW=plan-spec" in content
    assert plan_text in content
    # Output path must be a specific file so the AgentCeption poller can watch
    # for it; the mcp_tools_hint guides the Cursor agent to call plan_get_schema.
    assert ".plan-output.yaml" in content
    assert "mcp_tools_hint=call plan_get_schema()" in content
    assert "output_schema=plan_get_schema" in content


@pytest.mark.anyio
async def test_git_worktree_add_called(
    async_client: AsyncClient,
    tmp_path: Path,
) -> None:
    """POST must call asyncio.create_subprocess_exec with 'git -C <repo> worktree add -b ...'."""
    proc_mock = _make_proc_mock(returncode=0)

    with (
        patch(
            "agentception.routes.api.plan.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ) as mock_exec,
        patch(
            "agentception.routes.api.plan.settings.worktrees_dir",
            tmp_path,
        ),
        patch(
            "agentception.routes.api.plan.settings.host_worktrees_dir",
            tmp_path,
        ),
    ):
        response = await async_client.post(
            "/api/plan/draft",
            json={"text": "Any valid plan text"},
        )

    assert response.status_code == 200
    # git -C <repo_dir> worktree add -b <branch> <path> origin/dev
    call_args = mock_exec.call_args
    assert call_args is not None
    args = call_args[0]
    assert args[0] == "git"
    assert args[1] == "-C"           # repo flag
    assert args[3] == "worktree"
    assert args[4] == "add"
    assert args[5] == "-b"           # named branch
    assert "plan-draft-" in args[6]  # branch name contains slug
    assert "plan-draft-" in str(args[7])  # worktree path


# ---------------------------------------------------------------------------
# POST /api/plan/draft — validation tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_empty_dump_returns_422(async_client: AsyncClient) -> None:
    """POST with empty text must return 422."""
    response = await async_client.post(
        "/api/plan/draft",
        json={"text": ""},
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_post_whitespace_dump_returns_422(async_client: AsyncClient) -> None:
    """POST with whitespace-only text must return 422."""
    response = await async_client.post(
        "/api/plan/draft",
        json={"text": "   \t\n  "},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/plan/launch — issue #873
#
# Test YAML uses EnrichedManifest format (initiative + phases with EnrichedIssue).
# This is the schema plan_spawn_coordinator validates against internally.
# ---------------------------------------------------------------------------

_VALID_YAML = """\
initiative: plan-p2-20260303
phases:
  - label: foundation
    description: "Core MCP and schema tooling"
    depends_on: []
    issues:
      - title: "MCP layer + schema tools"
        body: "Implement plan_get_schema and plan_validate_spec MCP tools"
        labels: [enhancement]
        phase: foundation
        depends_on: []
        can_parallel: true
        acceptance_criteria: ["plan_get_schema returns valid JSON Schema"]
        tests_required: ["test_plan_get_schema"]
        docs_required: ["docs/reference/plan-tools.md"]
      - title: "Plan tools — label context + coordinator spawn"
        body: "Implement plan_spawn_coordinator"
        labels: [enhancement]
        phase: foundation
        depends_on: ["MCP layer + schema tools"]
        can_parallel: false
        acceptance_criteria: ["plan_spawn_coordinator creates worktree"]
        tests_required: ["test_plan_spawn_coordinator"]
        docs_required: []
    parallel_groups:
      - ["MCP layer + schema tools"]
      - ["Plan tools — label context + coordinator spawn"]
"""

_SPAWN_RESULT = {
    "worktree": "/tmp/worktrees/coordinator-20260303-142201",
    "branch": "coordinator/20260303-142201",
    "agent_task_path": "/tmp/worktrees/coordinator-20260303-142201/.agent-task",
    "batch_id": "coordinator-20260303-142201",
}


@pytest.mark.anyio
async def test_post_valid_yaml_returns_200_with_worktree(
    async_client: AsyncClient,
) -> None:
    """POST a valid EnrichedManifest YAML → 200 with worktree, branch, agent_task_path, batch_id."""
    with patch(
        "agentception.routes.api.plan.plan_spawn_coordinator",
        new=AsyncMock(return_value=_SPAWN_RESULT),
    ):
        response = await async_client.post(
            "/api/plan/launch",
            json={"yaml_text": _VALID_YAML},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["worktree"] == _SPAWN_RESULT["worktree"]
    assert body["branch"] == _SPAWN_RESULT["branch"]
    assert body["agent_task_path"] == _SPAWN_RESULT["agent_task_path"]
    assert body["batch_id"] == _SPAWN_RESULT["batch_id"]


@pytest.mark.anyio
async def test_post_malformed_yaml_returns_422(async_client: AsyncClient) -> None:
    """POST a malformed YAML string (unclosed sequence) → 422 with error detail."""
    with patch(
        "agentception.routes.api.plan.plan_spawn_coordinator",
        new=AsyncMock(return_value=_SPAWN_RESULT),
    ):
        response = await async_client.post(
            "/api/plan/launch",
            json={"yaml_text": "key: [unclosed"},
        )

    assert response.status_code == 422
    body = response.json()
    assert "YAML" in body["detail"] or "yaml" in body["detail"].lower()


@pytest.mark.anyio
async def test_post_yaml_invalid_manifest_returns_422(async_client: AsyncClient) -> None:
    """POST YAML that doesn't match EnrichedManifest schema → 422 with validation detail."""
    with patch(
        "agentception.routes.api.plan.plan_spawn_coordinator",
        new=AsyncMock(return_value=_SPAWN_RESULT),
    ):
        response = await async_client.post(
            "/api/plan/launch",
            # Missing required 'phases' — will fail EnrichedManifest validation
            json={"yaml_text": "initiative: test\n"},
        )

    assert response.status_code == 422
    body = response.json()
    assert "validation" in body["detail"].lower() or "phases" in body["detail"].lower()


@pytest.mark.anyio
async def test_post_yaml_with_cyclic_deps_returns_422(async_client: AsyncClient) -> None:
    """POST YAML where issue A depends on B and B depends on A → 422 with cycle description."""
    cyclic_yaml = """\
initiative: cyclic-test
phases:
  - label: foundation
    description: "Cyclic phase"
    depends_on: []
    issues:
      - title: "Issue A"
        body: "Depends on B"
        labels: []
        phase: foundation
        depends_on: ["Issue B"]
        can_parallel: false
        acceptance_criteria: []
        tests_required: []
        docs_required: []
      - title: "Issue B"
        body: "Depends on A"
        labels: []
        phase: foundation
        depends_on: ["Issue A"]
        can_parallel: false
        acceptance_criteria: []
        tests_required: []
        docs_required: []
    parallel_groups: []
"""
    with patch(
        "agentception.routes.api.plan.plan_spawn_coordinator",
        new=AsyncMock(return_value=_SPAWN_RESULT),
    ):
        response = await async_client.post(
            "/api/plan/launch",
            json={"yaml_text": cyclic_yaml},
        )

    assert response.status_code == 422
    body = response.json()
    assert "Cycle" in body["detail"] or "cycle" in body["detail"].lower()


@pytest.mark.anyio
async def test_plan_spawn_coordinator_called_with_correct_manifest(
    async_client: AsyncClient,
) -> None:
    """plan_spawn_coordinator must be called with the serialised EnrichedManifest JSON."""
    spawn_mock = AsyncMock(return_value=_SPAWN_RESULT)

    with patch(
        "agentception.routes.api.plan.plan_spawn_coordinator",
        new=spawn_mock,
    ):
        response = await async_client.post(
            "/api/plan/launch",
            json={"yaml_text": _VALID_YAML},
        )

    assert response.status_code == 200
    spawn_mock.assert_called_once()

    # Verify the argument is valid JSON containing the initiative and phases
    call_args = spawn_mock.call_args
    assert call_args is not None
    manifest_json_arg: str = call_args[0][0]
    parsed = json.loads(manifest_json_arg)
    assert parsed["initiative"] == "plan-p2-20260303"
    assert len(parsed["phases"]) == 1
    assert parsed["phases"][0]["label"] == "foundation"
