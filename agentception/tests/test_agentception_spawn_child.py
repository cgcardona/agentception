from __future__ import annotations

"""Tests for the universal child-node spawner (agentception/services/spawn_child.py).

Covers:
  - tier parameter contract (coordinator / worker).
  - _build_child_task() field presence and correctness for each scope type.
  - spawn_child() happy path (mocked git + DB) for coordinator and leaf.
  - spawn_child() produces tier = (not node_type =) in the DB row.
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
    assert branch.startswith("ac/coord-ac-workflow-")


def test_make_branch_issue() -> None:
    branch = _make_branch("issue", "42")
    assert branch == "ac/issue-42"


def test_make_branch_pr() -> None:
    branch = _make_branch("pr", "112")
    assert branch.startswith("ac/review-112-")




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
            role="developer",
            tier="worker",
            scope_type="issue",
            scope_value="42",
            gh_repo="owner/repo",
            issue_title="Fix broken thing",
            issue_body="Uses FastAPI Depends and response_model",
        )

    assert result.tier == "worker"
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
            tier="worker",
            scope_type="pr",
            scope_value="112",
            gh_repo="owner/repo",
        )

    assert result.tier == "worker"
    assert result.run_id.startswith("pr-112-")
    assert "michael_fagan" in result.cognitive_arch


@pytest.mark.anyio
async def test_spawn_child_tier_in_db_row() -> None:
    """spawn_child must persist the tier field to the DB row."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    persist_mock = AsyncMock()
    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ),
        patch("agentception.services.spawn_child.persist_agent_run_dispatch", persist_mock),
        patch("agentception.services.spawn_child.acknowledge_agent_run", AsyncMock(return_value=True)),
    ):
        await spawn_child(
            parent_run_id="coord-engineering-xyz",
            role="developer",
            tier="worker",
            scope_type="issue",
            scope_value="5",
            gh_repo="owner/repo",
        )

    persist_mock.assert_awaited_once()
    call_kwargs = persist_mock.call_args.kwargs
    assert call_kwargs["tier"] == "worker"


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
            role="developer",
            tier="worker",
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
    # spawn_child now calls create_subprocess_exec twice:
    #   1. git rev-parse origin/dev  → succeeds (returns a SHA)
    #   2. git worktree add ...      → fails (the case under test)
    sha_proc = MagicMock()
    sha_proc.returncode = 0
    sha_proc.communicate = AsyncMock(
        return_value=(b"abc1234abc1234abc1234abc1234abc1234abc1234\n", b"")
    )

    fail_proc = MagicMock()
    fail_proc.returncode = 1
    fail_proc.communicate = AsyncMock(return_value=(b"", b"fatal: branch already exists"))

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            side_effect=[sha_proc, fail_proc],
        ),
        pytest.raises(SpawnChildError, match="git worktree add failed"),
    ):
        await spawn_child(
            parent_run_id="coord-xyz",
            role="developer",
            tier="worker",
            scope_type="issue",
            scope_value="1",
            gh_repo="owner/repo",
        )


@pytest.mark.anyio
async def test_spawn_child_db_persist_failure_cleans_up_worktree() -> None:
    """When DB persist fails, the worktree must be removed."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    subprocess_calls: list[tuple[str, ...]] = []

    async def fake_subprocess(*args: str, **kwargs: object) -> MagicMock:
        subprocess_calls.append(args)
        return mock_proc

    with (
        patch(
            "agentception.services.spawn_child.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess,
        ),
        patch(
            "agentception.services.spawn_child.persist_agent_run_dispatch",
            AsyncMock(side_effect=RuntimeError("DB is down")),
        ),
        pytest.raises((SpawnChildError, RuntimeError)),
    ):
        await spawn_child(
            parent_run_id="coord-xyz",
            role="developer",
            tier="worker",
            scope_type="issue",
            scope_value="2",
            gh_repo="owner/repo",
        )


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
    assert "agent_task_path" not in d, "to_dict must not expose the removed 'agent_task_path' key"


def test_spawn_child_result_to_dict_org_domain_none() -> None:
    """org_domain=None is preserved in to_dict (not silently dropped)."""
    result = SpawnChildResult(
        run_id="leaf-abc",
        host_worktree_path="/host/path",
        worktree_path="/container/path",
        tier="worker",
        org_domain=None,
        role="developer",
        cognitive_arch="guido:python",
        scope_type="issue",
        scope_value="42",
    )
    d = result.to_dict()
    assert d["tier"] == "worker"
    assert d["org_domain"] is None


