from __future__ import annotations

"""Tests for the universal child-node spawner (agentception/services/spawn_child.py).

Covers:
  - tier parameter contract (executive / coordinator / engineer / reviewer).
  - _build_child_task() field presence and correctness for each scope type.
  - spawn_child() happy path (mocked git + DB) for coordinator and leaf.
  - spawn_child() produces tier = (not node_type =) in .agent-task.
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
    ScopeType,
    SpawnChildError,
    SpawnChildResult,
    Tier,
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
# _build_child_task — field presence, correctness, TIER vs NODE_TYPE
# ---------------------------------------------------------------------------


def _make_task(
    run_id: str = "test-run-123",
    role: str = "engineering-coordinator",
    tier: Tier = "coordinator",
    org_domain: str | None = None,
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
        tier=tier,
        org_domain=org_domain,
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
    assert 'id = "test-run-123"' in task
    assert 'role = "engineering-coordinator"' in task
    assert 'tier = "coordinator"' in task
    assert 'scope_type = "label"' in task
    assert 'scope_value = "ac-workflow"' in task
    assert 'parent_run_id = "label-cto-111111"' in task
    assert 'cognitive_arch = "von_neumann:python"' in task
    assert 'ac_url = "http://localhost:10003"' in task
    assert 'role_file = "' in task
    assert 'host_role_file = "' in task


def test_build_child_task_does_not_contain_node_type_field() -> None:
    """node_type must not appear — only tier = is written."""
    task = _make_task()
    assert "NODE_TYPE=" not in task
    assert "node_type = " not in task


def test_build_child_task_org_domain_written_when_provided() -> None:
    """org_domain is written to the .agent-task when org_domain is supplied."""
    task = _make_task(org_domain="qa")
    assert 'org_domain = "qa"' in task


def test_build_child_task_org_domain_absent_when_none() -> None:
    """When org_domain is None, the TOML value is an empty string."""
    task = _make_task(org_domain=None)
    assert 'org_domain = ""' in task


def test_build_child_task_tier_and_org_domain_are_separate() -> None:
    """tier and org_domain are written as two independent TOML keys."""
    task = _make_task(tier="reviewer", org_domain="qa")
    assert 'tier = "reviewer"' in task
    assert 'org_domain = "qa"' in task
    # Behavioral tier must not bleed into org domain
    assert 'tier = "qa"' not in task
    assert 'org_domain = "reviewer"' not in task


def test_build_child_task_engineer_tier() -> None:
    task = _make_task(tier="engineer", role="python-developer", scope_type="issue",
                      scope_value="42", issue_number=42)
    assert 'tier = "engineer"' in task


def test_build_child_task_coordinator_label_scope_query_hint() -> None:
    task = _make_task(scope_type="label", tier="coordinator")
    assert "github_list_issues" in task
    assert "label='ac-workflow'" in task


def test_build_child_task_issue_scope_includes_issue_fields() -> None:
    task = _make_task(
        scope_type="issue",
        scope_value="42",
        tier="engineer",
        role="python-developer",
        issue_number=42,
        issue_title="Fix the thing",
    )
    assert "issue_number = 42" in task
    assert 'issue_title = "Fix the thing"' in task
    assert 'issue_url = "' in task
    assert "github_get_issue(number=42)" in task


def test_build_child_task_pr_scope_includes_pr_fields() -> None:
    task = _make_task(
        scope_type="pr",
        scope_value="112",
        tier="reviewer",
        role="pr-reviewer",
        pr_number=112,
    )
    assert "pr_number = 112" in task
    assert 'pr_url = "' in task
    assert "github_get_pr(number=112)" in task


def test_build_child_task_coordinator_includes_both_queries() -> None:
    """A coordinator with label scope should get both issue and PR query hints."""
    task = _make_task(tier="coordinator", role="cto", scope_type="label", scope_value="ac-workflow")
    assert "github_list_issues" in task
    assert "github_list_prs" in task


def test_build_child_task_coord_fingerprint_written_when_provided() -> None:
    """COORD_FINGERPRINT is written to the task file when the caller provides it."""
    task = _make_task(
        tier="engineer",
        role="python-developer",
        scope_type="issue",
        scope_value="42",
        issue_number=42,
    )
    # Without coord_fingerprint — TOML writes it as an empty string.
    assert 'coord_fingerprint = ""' in task


def test_build_child_task_coord_fingerprint_present() -> None:
    """When coord_fingerprint is supplied it appears verbatim in the task file."""
    fp = "Engineering Coordinator · batch-abc123"
    task = _build_child_task(
        run_id="issue-42-abc",
        role="python-developer",
        tier="engineer",
        org_domain="engineering",
        scope_type="issue",
        scope_value="42",
        gh_repo="owner/repo",
        branch="feat/issue-42-ab12",
        worktree_path="/worktrees/issue-42-abc",
        host_worktree_path="/host/worktrees/issue-42-abc",
        batch_id="issue-42-20260305T000000Z-ab12",
        parent_run_id="coord-ac-xyz",
        cognitive_arch="ada_lovelace:python",
        ac_url="http://localhost:10003",
        coord_fingerprint=fp,
        issue_number=42,
    )
    assert f'coord_fingerprint = "{fp}"' in task


# ---------------------------------------------------------------------------
# spawn_child — explicit tier parameter, happy paths
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
            tier="coordinator",
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
            tier="engineer",
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
            tier="reviewer",
            scope_type="pr",
            scope_value="112",
            gh_repo="owner/repo",
        )

    assert result.tier == "reviewer"
    assert result.run_id.startswith("pr-112-")
    assert "michael_fagan" in result.cognitive_arch


@pytest.mark.anyio
async def test_spawn_child_tier_written_to_agent_task() -> None:
    """The .agent-task file must contain tier = (not node_type =)."""
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
            tier="engineer",
            scope_type="issue",
            scope_value="5",
            gh_repo="owner/repo",
        )

    assert written_content, "write_text was never called"
    content = written_content[0]
    assert 'tier = "engineer"' in content
    # NODE_TYPE / node_type must not appear (internal only, derived from tier)
    assert "NODE_TYPE=" not in content
    assert "node_type =" not in content


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
            tier="engineer",
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
            tier="engineer",
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
            tier="engineer",
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
        org_domain="engineering",
        role="engineering-coordinator",
        cognitive_arch="von_neumann:python",
        agent_task_path="/container/path/.agent-task",
        scope_type="label",
        scope_value="ac-workflow",
    )
    d = result.to_dict()
    assert d["run_id"] == "coord-abc"
    assert d["tier"] == "coordinator"
    assert d["org_domain"] == "engineering"
    assert d["cognitive_arch"] == "von_neumann:python"
    assert d["scope_type"] == "label"
    assert "node_type" not in d, "to_dict must not expose the old 'node_type' key"


def test_spawn_child_result_to_dict_org_domain_none() -> None:
    """org_domain=None is preserved in to_dict (not silently dropped)."""
    result = SpawnChildResult(
        run_id="leaf-abc",
        host_worktree_path="/host/path",
        worktree_path="/container/path",
        tier="engineer",
        org_domain=None,
        role="python-developer",
        cognitive_arch="guido:python",
        agent_task_path="/container/path/.agent-task",
        scope_type="issue",
        scope_value="42",
    )
    d = result.to_dict()
    assert d["tier"] == "engineer"
    assert d["org_domain"] is None


# ---------------------------------------------------------------------------
# POST /api/runs/{parent_run_id}/children — HTTP endpoint
# ---------------------------------------------------------------------------


def test_spawn_child_endpoint_invalid_scope_type(client: TestClient) -> None:
    """Invalid scope_type should return HTTP 422."""
    response = client.post(
        "/api/runs/cto-abc/children",
        json={
            "role": "engineering-coordinator",
            "tier": "coordinator",
            "scope_type": "invalid",
            "scope_value": "ac-workflow",
            "gh_repo": "owner/repo",
        },
    )
    assert response.status_code == 422


def test_spawn_child_endpoint_invalid_tier(client: TestClient) -> None:
    """Invalid tier value should return HTTP 422."""
    response = client.post(
        "/api/runs/cto-abc/children",
        json={
            "role": "engineering-coordinator",
            "tier": "manager",   # not a valid Tier — must be rejected
            "scope_type": "label",
            "scope_value": "ac-workflow",
            "gh_repo": "owner/repo",
        },
    )
    assert response.status_code == 422


def test_spawn_child_endpoint_missing_tier(client: TestClient) -> None:
    """Missing tier (now required) must return HTTP 422."""
    response = client.post(
        "/api/runs/cto-abc/children",
        json={
            "role": "engineering-coordinator",
            # tier missing
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
            "tier": "coordinator",
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
        org_domain="engineering",
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
                "tier": "coordinator",
                "org_domain": "engineering",
                "scope_type": "label",
                "scope_value": "ac-workflow",
                "gh_repo": "owner/repo",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == "coord-ac-workflow-abc123"
    assert data["tier"] == "coordinator"
    assert data["org_domain"] == "engineering"
    assert "node_type" not in data, "Response must not contain old 'node_type' key"
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
                "tier": "engineer",
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


def test_all_tiers_produce_valid_task_content() -> None:
    """Every tier/scope combination must produce a parseable .agent-task."""
    combos: list[tuple[str, Tier, ScopeType, str]] = [
        ("cto", "executive", "label", "ac-workflow"),
        ("engineering-coordinator", "coordinator", "label", "ac-ui/0-bugs"),
        ("qa-coordinator", "coordinator", "label", "ac-ui/0-bugs"),
        ("python-developer", "engineer", "issue", "42"),
        ("pr-reviewer", "reviewer", "pr", "112"),
    ]
    for role, tier, scope_type, scope_value in combos:
        task = _make_task(
            role=role,
            tier=tier,
            scope_type=scope_type,
            scope_value=scope_value,
        )
        # Every task must have these universal TOML fields
        assert 'id = "' in task, f"Missing id for {role}"
        assert 'tier = "' in task, f"Missing tier for {role}"
        assert 'parent_run_id = "' in task, f"Missing parent_run_id for {role}"
        assert 'cognitive_arch = "' in task, f"Missing cognitive_arch for {role}"
        assert 'role_file = "' in task, f"Missing role_file for {role}"
        assert 'host_role_file = "' in task, f"Missing host_role_file for {role}"
        assert 'ac_url = "' in task, f"Missing ac_url for {role}"
        assert 'scope_value = "' in task, f"Missing scope_value for {role}"
        assert 'workflow = "' in task, f"Missing workflow for {role}"
        # NODE_TYPE / node_type must be absent
        assert "NODE_TYPE=" not in task, f"Unexpected NODE_TYPE= for {role}"
        assert "node_type =" not in task, f"Unexpected node_type = for {role}"


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
    # Both must have all universal TOML fields
    for field in ('id = "', 'tier = "', 'cognitive_arch = "', 'role_file = "', 'host_role_file = "', 'scope_value = "', 'workflow = "'):
        assert field in task_root, f"Missing {field!r} in root task"
        assert field in task_child, f"Missing {field!r} in child task"
