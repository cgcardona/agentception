from __future__ import annotations

"""Tests for the universal child-node spawner (agentception/services/spawn_child.py).

Covers:
  - node_type parameter contract (coordinator / leaf).
  - _build_child_task() field presence and correctness for each scope type.
  - spawn_child() happy path (mocked git + DB) for coordinator and leaf.
  - spawn_child() produces NODE_TYPE= (not TIER=) in .agent-task.
  - spawn_child() worktree failure cleanup.
  - POST /api/runs/{parent_run_id}/children HTTP endpoint (valid, invalid, and propagated errors).
  - build_spawn_child MCP tool (happy path + error cases).
  - Universal tree protocol guarantee: any node can be pruned as a root.

Run targeted:
    pytest agentception/tests/test_agentception_spawn_child.py -v
"""

import asyncio
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.services.spawn_child import (
    NodeType,
    ScopeType,
    SpawnChildError,
    SpawnChildResult,
    _build_child_task,
    _make_branch,
    _make_run_id,
    spawn_child,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# _make_run_id and _make_branch — correct prefixes per scope type
# ---------------------------------------------------------------------------


def test_make_run_id_label_prefix() -> None:
    run_id = _make_run_id("label", "ac-workflow")
    assert run_id.startswith("coord-ac-workflow-")


def test_make_run_id_issue_prefix() -> None:
    run_id = _make_run_id("issue", "42")
    assert run_id.startswith("issue-42-")


def test_make_run_id_pr_prefix() -> None:
    run_id = _make_run_id("pr", "112")
    assert run_id.startswith("pr-112-")


def test_make_branch_label() -> None:
    branch = _make_branch("label", "ac-workflow")
    assert branch.startswith("agent/ac-workflow-")


def test_make_branch_issue() -> None:
    branch = _make_branch("issue", "42")
    assert branch.startswith("feat/issue-42-")


def test_make_branch_pr() -> None:
    branch = _make_branch("pr", "112")
    assert branch.startswith("review/pr-112-")


# ---------------------------------------------------------------------------
# _build_child_task — field presence, correctness, NODE_TYPE vs TIER
# ---------------------------------------------------------------------------


def _make_task(
    run_id: str = "test-run-123",
    role: str = "engineering-coordinator",
    node_type: NodeType = "coordinator",
    logical_tier: str | None = None,
    scope_type: ScopeType = "label",
    scope_value: str = "ac-workflow",
    gh_repo: str = "owner/repo",
    branch: str = "agent/ac-workflow-abcd",
    worktree_path: str = "/worktrees/test-run-123",
    host_worktree_path: str = "/host/worktrees/test-run-123",
    batch_id: str = "label-ac-workflow-20260101T000000Z-abcd",
    parent_run_id: str = "label-cto-111111",
    cognitive_arch: str = "von_neumann:python",
    ac_url: str = "http://localhost:10003",
    issue_title: str = "",
    issue_number: int | None = None,
    pr_number: int | None = None,
) -> str:
    return _build_child_task(
        run_id=run_id,
        role=role,
        node_type=node_type,
        logical_tier=logical_tier,
        scope_type=scope_type,
        scope_value=scope_value,
        gh_repo=gh_repo,
        branch=branch,
        worktree_path=worktree_path,
        host_worktree_path=host_worktree_path,
        batch_id=batch_id,
        parent_run_id=parent_run_id,
        cognitive_arch=cognitive_arch,
        ac_url=ac_url,
        issue_title=issue_title,
        issue_number=issue_number,
        pr_number=pr_number,
    )


def test_build_child_task_required_fields_present() -> None:
    task = _make_task()
    assert "RUN_ID=test-run-123" in task
    assert "ROLE=engineering-coordinator" in task
    assert "NODE_TYPE=coordinator" in task
    assert "SCOPE_TYPE=label" in task
    assert "SCOPE_VALUE=ac-workflow" in task
    assert "PARENT_RUN_ID=label-cto-111111" in task
    assert "COGNITIVE_ARCH=von_neumann:python" in task
    assert "AC_URL=http://localhost:10003" in task
    assert "ROLE_FILE=" in task


def test_build_child_task_does_not_contain_tier_field() -> None:
    """The old TIER= field must be gone — only NODE_TYPE= is written."""
    task = _make_task()
    lines = task.splitlines()
    tier_lines = [ln for ln in lines if ln.startswith("TIER=")]
    assert tier_lines == [], f"Unexpected TIER= lines: {tier_lines}"


def test_build_child_task_logical_tier_written_when_provided() -> None:
    """LOGICAL_TIER= is written to the .agent-task when logical_tier is supplied."""
    task = _make_task(logical_tier="qa")
    assert "LOGICAL_TIER=qa" in task


def test_build_child_task_logical_tier_absent_when_none() -> None:
    """LOGICAL_TIER= must not appear at all when logical_tier is None."""
    task = _make_task(logical_tier=None)
    lt_lines = [ln for ln in task.splitlines() if ln.startswith("LOGICAL_TIER=")]
    assert lt_lines == [], f"Unexpected LOGICAL_TIER= lines: {lt_lines}"


def test_build_child_task_node_type_and_logical_tier_are_separate() -> None:
    """NODE_TYPE and LOGICAL_TIER are written as two independent lines."""
    task = _make_task(node_type="leaf", logical_tier="qa")
    assert "NODE_TYPE=leaf" in task
    assert "LOGICAL_TIER=qa" in task
    # Structural type must not bleed into org domain
    assert "NODE_TYPE=qa" not in task
    assert "LOGICAL_TIER=leaf" not in task


def test_build_child_task_leaf_node_type() -> None:
    task = _make_task(node_type="leaf", role="python-developer", scope_type="issue",
                      scope_value="42", issue_number=42)
    assert "NODE_TYPE=leaf" in task


def test_build_child_task_coordinator_label_scope_query_hint() -> None:
    task = _make_task(scope_type="label", node_type="coordinator")
    assert "gh issue list" in task
    assert "--label 'ac-workflow'" in task


def test_build_child_task_issue_scope_includes_issue_fields() -> None:
    task = _make_task(
        scope_type="issue",
        scope_value="42",
        node_type="leaf",
        role="python-developer",
        issue_number=42,
        issue_title="Fix the thing",
    )
    assert "ISSUE_NUMBER=42" in task
    assert "ISSUE_TITLE=Fix the thing" in task
    assert "ISSUE_URL=" in task
    assert "gh issue view 42" in task


def test_build_child_task_pr_scope_includes_pr_fields() -> None:
    task = _make_task(
        scope_type="pr",
        scope_value="112",
        node_type="leaf",
        role="pr-reviewer",
        pr_number=112,
    )
    assert "PR_NUMBER=112" in task
    assert "PR_URL=" in task
    assert "gh pr view 112" in task


def test_build_child_task_coordinator_includes_both_queries() -> None:
    """A coordinator with label scope should get both issue and PR query hints."""
    task = _make_task(node_type="coordinator", role="cto", scope_type="label", scope_value="ac-workflow")
    assert "gh issue list" in task
    assert "gh pr list" in task


# ---------------------------------------------------------------------------
# spawn_child — explicit node_type parameter, happy paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_spawn_child_coordinator_label_happy_path() -> None:
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ),
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", AsyncMock()),
        patch("agentception.services.spawn_child.acknowledge_agent_run", AsyncMock(return_value=True)),
        patch.object(Path, "write_text", return_value=None),
        patch.object(Path, "exists", return_value=False),
    ):
        result = await spawn_child(
            parent_run_id="label-cto-abc123",
            role="engineering-coordinator",
            node_type="coordinator",
            scope_type="label",
            scope_value="ac-workflow",
            gh_repo="owner/repo",
        )

    assert isinstance(result, SpawnChildResult)
    assert result.node_type == "coordinator"
    assert result.role == "engineering-coordinator"
    assert result.scope_type == "label"
    assert result.scope_value == "ac-workflow"
    assert "von_neumann" in result.cognitive_arch
    assert result.run_id.startswith("coord-ac-workflow-")


@pytest.mark.anyio
async def test_spawn_child_leaf_issue_happy_path() -> None:
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ),
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", AsyncMock()),
        patch("agentception.services.spawn_child.acknowledge_agent_run", AsyncMock(return_value=True)),
        patch.object(Path, "write_text", return_value=None),
    ):
        result = await spawn_child(
            parent_run_id="coord-engineering-xyz",
            role="python-developer",
            node_type="leaf",
            scope_type="issue",
            scope_value="42",
            gh_repo="owner/repo",
            issue_title="Fix broken thing",
            issue_body="Uses FastAPI Depends and response_model",
        )

    assert result.node_type == "leaf"
    assert result.run_id.startswith("issue-42-")
    # COGNITIVE_ARCH should reflect fastapi skill from the body keyword
    assert "fastapi" in result.cognitive_arch


@pytest.mark.anyio
async def test_spawn_child_leaf_pr_happy_path() -> None:
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ),
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", AsyncMock()),
        patch("agentception.services.spawn_child.acknowledge_agent_run", AsyncMock(return_value=True)),
        patch.object(Path, "write_text", return_value=None),
    ):
        result = await spawn_child(
            parent_run_id="coord-qa-xyz",
            role="pr-reviewer",
            node_type="leaf",
            scope_type="pr",
            scope_value="112",
            gh_repo="owner/repo",
        )

    assert result.node_type == "leaf"
    assert result.run_id.startswith("pr-112-")
    assert "michael_fagan" in result.cognitive_arch


@pytest.mark.anyio
async def test_spawn_child_node_type_written_to_agent_task() -> None:
    """The .agent-task file must contain NODE_TYPE= (not TIER=)."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    written_content: list[str] = []

    def capture_write(content: str, **_: object) -> None:
        written_content.append(content)

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ),
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", AsyncMock()),
        patch("agentception.services.spawn_child.acknowledge_agent_run", AsyncMock(return_value=True)),
        patch.object(Path, "write_text", side_effect=capture_write),
    ):
        await spawn_child(
            parent_run_id="coord-engineering-xyz",
            role="python-developer",
            node_type="leaf",
            scope_type="issue",
            scope_value="5",
            gh_repo="owner/repo",
        )

    assert written_content, "write_text was never called"
    content = written_content[0]
    assert "NODE_TYPE=leaf" in content
    # Old TIER= line must not appear
    lines = content.splitlines()
    tier_lines = [ln for ln in lines if ln.startswith("TIER=")]
    assert tier_lines == [], f"Unexpected TIER= lines: {tier_lines}"


@pytest.mark.anyio
async def test_spawn_child_skills_hint_overrides_body_extraction() -> None:
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ),
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", AsyncMock()),
        patch("agentception.services.spawn_child.acknowledge_agent_run", AsyncMock(return_value=True)),
        patch.object(Path, "write_text", return_value=None),
    ):
        result = await spawn_child(
            parent_run_id="coord-xyz",
            role="frontend-developer",
            node_type="leaf",
            scope_type="issue",
            scope_value="99",
            gh_repo="owner/repo",
            issue_body="plain python code",
            skills_hint=["htmx", "jinja2"],
        )

    # skills_hint should override body keyword extraction
    assert "htmx" in result.cognitive_arch
    assert "jinja2" in result.cognitive_arch


# ---------------------------------------------------------------------------
# spawn_child — failure: worktree creation fails
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_spawn_child_worktree_failure_raises_spawn_child_error() -> None:
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"fatal: branch already exists"))

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ),
        pytest.raises(SpawnChildError, match="git worktree add failed"),
    ):
        await spawn_child(
            parent_run_id="coord-xyz",
            role="python-developer",
            node_type="leaf",
            scope_type="issue",
            scope_value="1",
            gh_repo="owner/repo",
        )


@pytest.mark.anyio
async def test_spawn_child_file_write_failure_cleans_up_worktree() -> None:
    """When .agent-task write fails, the worktree must be removed."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    cleanup_calls: list[tuple[str, ...]] = []

    async def fake_subprocess(*args: str, **kwargs: object) -> MagicMock:
        cleanup_calls.append(args)
        return mock_proc

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess,
        ),
        patch.object(Path, "write_text", side_effect=OSError("disk full")),
        pytest.raises(SpawnChildError, match=".agent-task write failed"),
    ):
        await spawn_child(
            parent_run_id="coord-xyz",
            role="python-developer",
            node_type="leaf",
            scope_type="issue",
            scope_value="2",
            gh_repo="owner/repo",
        )

    # Second subprocess call should be the cleanup worktree remove
    assert len(cleanup_calls) == 2
    assert "remove" in cleanup_calls[1]


# ---------------------------------------------------------------------------
# SpawnChildResult.to_dict — serialisation
# ---------------------------------------------------------------------------


def test_spawn_child_result_to_dict_all_keys() -> None:
    result = SpawnChildResult(
        run_id="coord-abc",
        host_worktree_path="/host/path",
        worktree_path="/container/path",
        node_type="coordinator",
        logical_tier="engineering",
        role="engineering-coordinator",
        cognitive_arch="von_neumann:python",
        agent_task_path="/container/path/.agent-task",
        scope_type="label",
        scope_value="ac-workflow",
    )
    d = result.to_dict()
    assert d["run_id"] == "coord-abc"
    assert d["node_type"] == "coordinator"
    assert d["logical_tier"] == "engineering"
    assert d["cognitive_arch"] == "von_neumann:python"
    assert d["scope_type"] == "label"
    assert "tier" not in d, "to_dict must not expose the old 'tier' key"


def test_spawn_child_result_to_dict_logical_tier_none() -> None:
    """logical_tier=None is preserved in to_dict (not silently dropped)."""
    result = SpawnChildResult(
        run_id="leaf-abc",
        host_worktree_path="/host/path",
        worktree_path="/container/path",
        node_type="leaf",
        logical_tier=None,
        role="python-developer",
        cognitive_arch="guido:python",
        agent_task_path="/container/path/.agent-task",
        scope_type="issue",
        scope_value="42",
    )
    d = result.to_dict()
    assert d["node_type"] == "leaf"
    assert d["logical_tier"] is None


# ---------------------------------------------------------------------------
# POST /api/runs/{parent_run_id}/children — HTTP endpoint
# ---------------------------------------------------------------------------


def test_spawn_child_endpoint_invalid_scope_type(client: TestClient) -> None:
    """Invalid scope_type should return HTTP 422."""
    response = client.post(
        "/api/runs/cto-abc/children",
        json={
            "role": "engineering-coordinator",
            "node_type": "coordinator",
            "scope_type": "invalid",
            "scope_value": "ac-workflow",
            "gh_repo": "owner/repo",
        },
    )
    assert response.status_code == 422


def test_spawn_child_endpoint_invalid_node_type(client: TestClient) -> None:
    """Invalid node_type should return HTTP 422."""
    response = client.post(
        "/api/runs/cto-abc/children",
        json={
            "role": "engineering-coordinator",
            "node_type": "executive",   # old value — must be rejected
            "scope_type": "label",
            "scope_value": "ac-workflow",
            "gh_repo": "owner/repo",
        },
    )
    assert response.status_code == 422


def test_spawn_child_endpoint_missing_node_type(client: TestClient) -> None:
    """Missing node_type (now required) must return HTTP 422."""
    response = client.post(
        "/api/runs/cto-abc/children",
        json={
            "role": "engineering-coordinator",
            # node_type missing
            "scope_type": "label",
            "scope_value": "ac-workflow",
            "gh_repo": "owner/repo",
        },
    )
    assert response.status_code == 422


def test_spawn_child_endpoint_missing_required_field(client: TestClient) -> None:
    response = client.post(
        "/api/runs/cto-abc/children",
        json={
            "role": "engineering-coordinator",
            "node_type": "coordinator",
            # scope_type missing
            "scope_value": "ac-workflow",
            "gh_repo": "owner/repo",
        },
    )
    assert response.status_code == 422


def test_spawn_child_endpoint_happy_path(client: TestClient) -> None:
    mock_result = SpawnChildResult(
        run_id="coord-ac-workflow-abc123",
        host_worktree_path="/host/worktrees/coord-ac-workflow-abc123",
        worktree_path="/worktrees/coord-ac-workflow-abc123",
        node_type="coordinator",
        logical_tier="engineering",
        role="engineering-coordinator",
        cognitive_arch="von_neumann:python",
        agent_task_path="/worktrees/coord-ac-workflow-abc123/.agent-task",
        scope_type="label",
        scope_value="ac-workflow",
    )
    with patch(
        "agentception.routes.api.runs.spawn_child",
        AsyncMock(return_value=mock_result),
    ):
        response = client.post(
            "/api/runs/label-cto-abc123/children",
            json={
                "role": "engineering-coordinator",
                "node_type": "coordinator",
                "logical_tier": "engineering",
                "scope_type": "label",
                "scope_value": "ac-workflow",
                "gh_repo": "owner/repo",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == "coord-ac-workflow-abc123"
    assert data["node_type"] == "coordinator"
    assert data["logical_tier"] == "engineering"
    assert "tier" not in data, "Response must not contain old 'tier' key"
    assert data["cognitive_arch"] == "von_neumann:python"
    assert data["status"] == "implementing"


def test_spawn_child_endpoint_propagates_spawn_child_error(client: TestClient) -> None:
    with patch(
        "agentception.routes.api.runs.spawn_child",
        AsyncMock(side_effect=SpawnChildError("git worktree add failed: branch exists")),
    ):
        response = client.post(
            "/api/runs/cto-abc/children",
            json={
                "role": "python-developer",
                "node_type": "leaf",
                "scope_type": "issue",
                "scope_value": "42",
                "gh_repo": "owner/repo",
            },
        )
    assert response.status_code == 500
    assert "git worktree add failed" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Universal tree protocol — any node can be pruned as root
# ---------------------------------------------------------------------------


def test_any_node_type_produces_valid_task_content() -> None:
    """Every node_type/scope combination must produce a parseable .agent-task."""
    combos: list[tuple[str, NodeType, ScopeType, str]] = [
        ("cto", "coordinator", "label", "ac-workflow"),
        ("engineering-coordinator", "coordinator", "label", "ac-ui/0-bugs"),
        ("qa-coordinator", "coordinator", "label", "ac-ui/0-bugs"),
        ("python-developer", "leaf", "issue", "42"),
        ("pr-reviewer", "leaf", "pr", "112"),
    ]
    for role, node_type, scope_type, scope_value in combos:
        task = _make_task(
            role=role,
            node_type=node_type,
            scope_type=scope_type,
            scope_value=scope_value,
        )
        # Every task must have these universal fields
        assert "RUN_ID=" in task, f"Missing RUN_ID for {role}"
        assert "NODE_TYPE=" in task, f"Missing NODE_TYPE for {role}"
        assert "PARENT_RUN_ID=" in task, f"Missing PARENT_RUN_ID for {role}"
        assert "COGNITIVE_ARCH=" in task, f"Missing COGNITIVE_ARCH for {role}"
        assert "ROLE_FILE=" in task, f"Missing ROLE_FILE for {role}"
        assert "AC_URL=" in task, f"Missing AC_URL for {role}"
        assert "SCOPE_TYPE=" in task, f"Missing SCOPE_TYPE for {role}"
        assert "SCOPE_VALUE=" in task, f"Missing SCOPE_VALUE for {role}"
        # TIER= must be absent
        tier_lines = [ln for ln in task.splitlines() if ln.startswith("TIER=")]
        assert tier_lines == [], f"Unexpected TIER= lines for {role}: {tier_lines}"


def test_pruned_subtree_root_has_same_fields_as_full_tree_root() -> None:
    """A coordinator launched as root must produce the same .agent-task fields
    as a coordinator launched as a child of the CTO."""
    # Coordinator as root (direct launch)
    task_root = _make_task(
        role="engineering-coordinator",
        node_type="coordinator",
        scope_type="label",
        scope_value="ac-workflow",
        parent_run_id="",  # no parent — it IS the root
    )
    # Coordinator as child
    task_child = _make_task(
        role="engineering-coordinator",
        node_type="coordinator",
        scope_type="label",
        scope_value="ac-workflow",
        parent_run_id="label-cto-abc123",
    )
    # Both must have all universal fields
    for field in ("RUN_ID=", "NODE_TYPE=", "COGNITIVE_ARCH=", "ROLE_FILE=", "SCOPE_TYPE=", "SCOPE_VALUE="):
        assert field in task_root
        assert field in task_child
