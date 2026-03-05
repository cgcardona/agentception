from __future__ import annotations

"""Tests for the universal child-node spawner (agentception/services/spawn_child.py).

Covers:
  - tier_for_role() mapping for all protocol tiers.
  - _build_child_task() content for each scope type.
  - spawn_child() happy path (mocked git + DB).
  - spawn_child() worktree failure cleanup.
  - POST /api/build/spawn-child HTTP endpoint.
  - build_spawn_child MCP tool (happy path + error cases).

Run targeted:
    pytest agentception/tests/test_agentception_spawn_child.py -v
"""

import asyncio
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentception.app import app
from agentception.services.spawn_child import (
    SpawnChildError,
    SpawnChildResult,
    _build_child_task,
    _make_branch,
    _make_run_id,
    spawn_child,
    tier_for_role,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# tier_for_role — mapping coverage
# ---------------------------------------------------------------------------


def test_tier_for_role_executive() -> None:
    assert tier_for_role("cto") == "executive"
    assert tier_for_role("ceo") == "executive"
    assert tier_for_role("csto") == "executive"


def test_tier_for_role_coordinator() -> None:
    assert tier_for_role("engineering-coordinator") == "coordinator"
    assert tier_for_role("qa-coordinator") == "coordinator"
    assert tier_for_role("conductor") == "coordinator"
    assert tier_for_role("vp-platform") == "coordinator"


def test_tier_for_role_reviewer() -> None:
    assert tier_for_role("pr-reviewer") == "reviewer"


def test_tier_for_role_engineer_defaults() -> None:
    assert tier_for_role("python-developer") == "engineer"
    assert tier_for_role("frontend-developer") == "engineer"
    assert tier_for_role("unknown-role-xyz") == "engineer"


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
# _build_child_task — field presence and correctness
# ---------------------------------------------------------------------------


def _make_task(**overrides: object) -> str:
    defaults: dict[str, object] = dict(
        run_id="test-run-123",
        role="engineering-coordinator",
        tier="coordinator",
        scope_type="label",
        scope_value="ac-workflow",
        gh_repo="owner/repo",
        branch="agent/ac-workflow-abcd",
        worktree_path="/worktrees/test-run-123",
        host_worktree_path="/host/worktrees/test-run-123",
        batch_id="label-ac-workflow-20260101T000000Z-abcd",
        parent_run_id="label-cto-111111",
        cognitive_arch="von_neumann:python",
        ac_url="http://localhost:10003",
    )
    defaults.update(overrides)
    return _build_child_task(**defaults)  # type: ignore[arg-type]


def test_build_child_task_required_fields_present() -> None:
    task = _make_task()
    assert "RUN_ID=test-run-123" in task
    assert "ROLE=engineering-coordinator" in task
    assert "TIER=coordinator" in task
    assert "SCOPE_TYPE=label" in task
    assert "SCOPE_VALUE=ac-workflow" in task
    assert "PARENT_RUN_ID=label-cto-111111" in task
    assert "COGNITIVE_ARCH=von_neumann:python" in task
    assert "AC_URL=http://localhost:10003" in task
    assert "ROLE_FILE=" in task


def test_build_child_task_label_scope_query_hint() -> None:
    task = _make_task(scope_type="label", tier="coordinator", role="engineering-coordinator")
    assert "gh issue list" in task
    assert "--label 'ac-workflow'" in task


def test_build_child_task_qa_coordinator_query_hint() -> None:
    task = _make_task(scope_type="label", tier="coordinator", role="qa-coordinator")
    assert "gh pr list" in task


def test_build_child_task_issue_scope_includes_issue_fields() -> None:
    task = _make_task(
        scope_type="issue",
        scope_value="42",
        tier="engineer",
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
        tier="reviewer",
        role="pr-reviewer",
        pr_number=112,
    )
    assert "PR_NUMBER=112" in task
    assert "PR_URL=" in task
    assert "gh pr view 112" in task


def test_build_child_task_executive_includes_both_queries() -> None:
    task = _make_task(tier="executive", role="cto", scope_type="label", scope_value="ac-workflow")
    assert "gh issue list" in task
    assert "gh pr list" in task


# ---------------------------------------------------------------------------
# spawn_child — happy path (mocked subprocess + DB)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_spawn_child_happy_path_label() -> None:
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
            scope_type="label",
            scope_value="ac-workflow",
            gh_repo="owner/repo",
        )

    assert isinstance(result, SpawnChildResult)
    assert result.tier == "coordinator"
    assert result.role == "engineering-coordinator"
    assert result.scope_type == "label"
    assert result.scope_value == "ac-workflow"
    assert "von_neumann" in result.cognitive_arch
    assert result.run_id.startswith("coord-ac-workflow-")


@pytest.mark.anyio
async def test_spawn_child_happy_path_issue() -> None:
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
            scope_type="issue",
            scope_value="42",
            gh_repo="owner/repo",
            issue_title="Fix broken thing",
            issue_body="Uses FastAPI Depends and response_model",
        )

    assert result.tier == "engineer"
    assert result.run_id.startswith("issue-42-")
    # COGNITIVE_ARCH should reflect fastapi skill from the body keyword
    assert "fastapi" in result.cognitive_arch


@pytest.mark.anyio
async def test_spawn_child_happy_path_pr() -> None:
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
            scope_type="pr",
            scope_value="112",
            gh_repo="owner/repo",
        )

    assert result.tier == "reviewer"
    assert result.run_id.startswith("pr-112-")
    assert "michael_fagan" in result.cognitive_arch


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
        tier="coordinator",
        role="engineering-coordinator",
        cognitive_arch="von_neumann:python",
        agent_task_path="/container/path/.agent-task",
        scope_type="label",
        scope_value="ac-workflow",
    )
    d = result.to_dict()
    assert d["run_id"] == "coord-abc"
    assert d["tier"] == "coordinator"
    assert d["cognitive_arch"] == "von_neumann:python"
    assert d["scope_type"] == "label"


# ---------------------------------------------------------------------------
# POST /api/build/spawn-child — HTTP endpoint
# ---------------------------------------------------------------------------


def test_spawn_child_endpoint_invalid_scope_type(client: TestClient) -> None:
    """Invalid scope_type should return HTTP 422."""
    response = client.post(
        "/api/build/spawn-child",
        json={
            "parent_run_id": "cto-abc",
            "role": "engineering-coordinator",
            "scope_type": "invalid",
            "scope_value": "ac-workflow",
            "gh_repo": "owner/repo",
        },
    )
    assert response.status_code == 422


def test_spawn_child_endpoint_missing_required_field(client: TestClient) -> None:
    response = client.post(
        "/api/build/spawn-child",
        json={
            "parent_run_id": "cto-abc",
            "role": "engineering-coordinator",
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
        tier="coordinator",
        role="engineering-coordinator",
        cognitive_arch="von_neumann:python",
        agent_task_path="/worktrees/coord-ac-workflow-abc123/.agent-task",
        scope_type="label",
        scope_value="ac-workflow",
    )
    with patch(
        "agentception.routes.api.build.spawn_child",
        AsyncMock(return_value=mock_result),
    ):
        response = client.post(
            "/api/build/spawn-child",
            json={
                "parent_run_id": "label-cto-abc123",
                "role": "engineering-coordinator",
                "scope_type": "label",
                "scope_value": "ac-workflow",
                "gh_repo": "owner/repo",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == "coord-ac-workflow-abc123"
    assert data["tier"] == "coordinator"
    assert data["cognitive_arch"] == "von_neumann:python"
    assert data["status"] == "implementing"


def test_spawn_child_endpoint_propagates_spawn_child_error(client: TestClient) -> None:
    with patch(
        "agentception.routes.api.build.spawn_child",
        AsyncMock(side_effect=SpawnChildError("git worktree add failed: branch exists")),
    ):
        response = client.post(
            "/api/build/spawn-child",
            json={
                "parent_run_id": "cto-abc",
                "role": "python-developer",
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


def test_any_tier_produces_valid_task_content() -> None:
    """Every tier/scope combination must produce a parseable .agent-task."""
    combos: list[tuple[str, str, str]] = [
        ("cto", "label", "ac-workflow"),
        ("engineering-coordinator", "label", "ac-ui/0-bugs"),
        ("qa-coordinator", "label", "ac-ui/0-bugs"),
        ("python-developer", "issue", "42"),
        ("pr-reviewer", "pr", "112"),
    ]
    for role, scope_type, scope_value in combos:
        task = _make_task(
            role=role,
            tier=tier_for_role(role),
            scope_type=scope_type,
            scope_value=scope_value,
        )
        # Every task must have these universal fields
        assert "RUN_ID=" in task, f"Missing RUN_ID for {role}"
        assert "PARENT_RUN_ID=" in task, f"Missing PARENT_RUN_ID for {role}"
        assert "COGNITIVE_ARCH=" in task, f"Missing COGNITIVE_ARCH for {role}"
        assert "ROLE_FILE=" in task, f"Missing ROLE_FILE for {role}"
        assert "AC_URL=" in task, f"Missing AC_URL for {role}"
        assert "SCOPE_TYPE=" in task, f"Missing SCOPE_TYPE for {role}"
        assert "SCOPE_VALUE=" in task, f"Missing SCOPE_VALUE for {role}"


def test_pruned_subtree_root_has_same_fields_as_full_tree_root() -> None:
    """A coordinator launched as root must produce the same .agent-task fields
    as a coordinator launched as a child of the CTO."""
    # Coordinator as root (direct launch)
    task_root = _make_task(
        role="engineering-coordinator",
        tier="coordinator",
        scope_type="label",
        scope_value="ac-workflow",
        parent_run_id="",  # no parent — it IS the root
    )
    # Coordinator as child
    task_child = _make_task(
        role="engineering-coordinator",
        tier="coordinator",
        scope_type="label",
        scope_value="ac-workflow",
        parent_run_id="label-cto-abc123",
    )
    # Both must have all universal fields
    for field in ("RUN_ID=", "COGNITIVE_ARCH=", "ROLE_FILE=", "SCOPE_TYPE=", "SCOPE_VALUE="):
        assert field in task_root
        assert field in task_child
