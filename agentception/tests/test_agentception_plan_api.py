"""Tests for POST /api/plan/launch (issue #873).

POST /api/plan/launch covers:
- Valid EnrichedManifest YAML → 200 with run_id, worktree, host_worktree, batch_id.
- Malformed YAML (syntax error) → 422 with error detail.
- YAML that fails EnrichedManifest validation → 422 with error detail.
- YAML with cyclic issue depends_on → 422 with cycle description.
- spawn_child called when a valid manifest is submitted.
- spawn_child raises SpawnChildError → 500.
- Non-dict YAML (a list) → 422 with "mapping" in detail.

_detect_issue_cycle unit tests:
- Empty phases → None (acyclic).
- Single issue, no deps → None.
- Linear chain A → B → None.
- Diamond A → B, A → C, B → D, C → D → None.
- Self-referencing A → A → cycle string.
- 3-node cycle A → B → C → A → cycle string.
- Issue depends on unknown title → None (graceful).
- Two independent chains, one cyclic → cycle detected.

All spawn_child calls are mocked so these tests do not require a live git
repository or network access.

Boundary: zero imports from external packages.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentception.app import app


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
# Helpers
# ---------------------------------------------------------------------------


def _make_spawn_result(
    run_id: str = "coord-plan-abc123",
    worktree_path: str = "/worktrees/coord-plan-abc123",
    host_worktree_path: str = "/host/worktrees/coord-plan-abc123",
) -> MagicMock:
    """Return a mock that behaves like a SpawnChildResult."""
    result = MagicMock()
    result.run_id = run_id
    result.worktree_path = worktree_path
    result.host_worktree_path = host_worktree_path
    return result


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


# ---------------------------------------------------------------------------
# POST /api/plan/launch — success path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_valid_yaml_returns_200_with_run_id(
    async_client: AsyncClient,
) -> None:
    """POST a valid EnrichedManifest YAML → 200 with run_id, worktree, host_worktree, batch_id."""
    spawn_result = _make_spawn_result()
    with patch(
        "agentception.routes.api.plan.spawn_child",
        new=AsyncMock(return_value=spawn_result),
    ):
        response = await async_client.post(
            "/api/plan/launch",
            json={"yaml_text": _VALID_YAML},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == spawn_result.run_id
    assert body["worktree"] == spawn_result.worktree_path
    assert body["host_worktree"] == spawn_result.host_worktree_path
    assert body["batch_id"] == spawn_result.run_id


@pytest.mark.anyio
async def test_spawn_child_called_for_valid_manifest(
    async_client: AsyncClient,
) -> None:
    """spawn_child must be called once when a valid EnrichedManifest is submitted."""
    spawn_mock = AsyncMock(return_value=_make_spawn_result())

    with patch("agentception.routes.api.plan.spawn_child", new=spawn_mock):
        response = await async_client.post(
            "/api/plan/launch",
            json={"yaml_text": _VALID_YAML},
        )

    assert response.status_code == 200
    spawn_mock.assert_called_once()


# ---------------------------------------------------------------------------
# POST /api/plan/launch — 422 validation paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_malformed_yaml_returns_422(async_client: AsyncClient) -> None:
    """POST a malformed YAML string (unclosed sequence) → 422 with error detail."""
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
    response = await async_client.post(
        "/api/plan/launch",
        json={"yaml_text": cyclic_yaml},
    )

    assert response.status_code == 422
    body = response.json()
    assert "Cycle" in body["detail"] or "cycle" in body["detail"].lower()


@pytest.mark.anyio
async def test_post_yaml_list_returns_422_with_mapping_detail(
    async_client: AsyncClient,
) -> None:
    """A YAML list (not a mapping) at the top level → 422 with 'mapping' in detail."""
    response = await async_client.post(
        "/api/plan/launch",
        json={"yaml_text": "- item1\n- item2\n"},
    )
    assert response.status_code == 422
    assert "mapping" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/plan/launch — 500 error path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_spawn_child_error_returns_500(
    async_client: AsyncClient,
) -> None:
    """When spawn_child raises SpawnChildError → 500 with error detail."""
    from agentception.services.spawn_child import SpawnChildError

    with patch(
        "agentception.routes.api.plan.spawn_child",
        new=AsyncMock(side_effect=SpawnChildError("git worktree add failed: disk full")),
    ):
        response = await async_client.post(
            "/api/plan/launch",
            json={"yaml_text": _VALID_YAML},
        )

    assert response.status_code == 500
    assert "Coordinator spawn failed" in response.json()["detail"]
    assert "disk full" in response.json()["detail"]


# ---------------------------------------------------------------------------
# _detect_issue_cycle — unit tests for the pure DFS helper
# ---------------------------------------------------------------------------


from agentception.models import EnrichedIssue, EnrichedPhase
from agentception.routes.api.plan import _detect_issue_cycle


def _make_phases(
    issues_by_phase: list[list[tuple[str, list[str]]]],
) -> list[EnrichedPhase]:
    """Build a list of EnrichedPhase objects for _detect_issue_cycle unit tests.

    Each inner list element is (title, depends_on).
    """
    phases: list[EnrichedPhase] = []
    for idx, issue_specs in enumerate(issues_by_phase):
        enriched_issues: list[EnrichedIssue] = [
            EnrichedIssue(
                title=title,
                body="body",
                labels=[],
                phase=f"phase-{idx}",
                depends_on=deps,
                can_parallel=True,
                acceptance_criteria=[],
                tests_required=[],
                docs_required=[],
            )
            for title, deps in issue_specs
        ]
        phases.append(
            EnrichedPhase(
                label=f"phase-{idx}",
                description="desc",
                depends_on=[],
                issues=enriched_issues,
                parallel_groups=[],
            )
        )
    return phases


def test_detect_issue_cycle_empty_phases() -> None:
    """`_detect_issue_cycle` returns None for an empty phase list."""
    assert _detect_issue_cycle([]) is None


def test_detect_issue_cycle_single_issue_no_deps() -> None:
    """`_detect_issue_cycle` returns None for a single issue with no dependencies."""
    phases = _make_phases([[("Issue A", [])]])
    assert _detect_issue_cycle(phases) is None


def test_detect_issue_cycle_linear_chain_is_acyclic() -> None:
    """`_detect_issue_cycle` returns None for a linear A → B dependency chain."""
    phases = _make_phases([[("Issue A", []), ("Issue B", ["Issue A"])]])
    assert _detect_issue_cycle(phases) is None


def test_detect_issue_cycle_diamond_is_acyclic() -> None:
    """`_detect_issue_cycle` returns None for a diamond A→B, A→C, B→D, C→D graph."""
    phases = _make_phases(
        [
            [
                ("A", []),
                ("B", ["A"]),
                ("C", ["A"]),
                ("D", ["B", "C"]),
            ]
        ]
    )
    assert _detect_issue_cycle(phases) is None


def test_detect_issue_cycle_self_reference_is_cycle() -> None:
    """`_detect_issue_cycle` returns a cycle string for a self-referencing issue."""
    phases = _make_phases([[("Issue A", ["Issue A"])]])
    result = _detect_issue_cycle(phases)
    assert result is not None
    assert "Cycle" in result
    assert "Issue A" in result


def test_detect_issue_cycle_two_node_cycle() -> None:
    """`_detect_issue_cycle` returns a cycle string for the A→B, B→A case."""
    phases = _make_phases([[("Issue A", ["Issue B"]), ("Issue B", ["Issue A"])]])
    result = _detect_issue_cycle(phases)
    assert result is not None
    assert "Cycle" in result


def test_detect_issue_cycle_three_node_cycle() -> None:
    """`_detect_issue_cycle` returns a cycle string for a 3-node A→B→C→A cycle."""
    phases = _make_phases(
        [
            [
                ("A", ["B"]),
                ("B", ["C"]),
                ("C", ["A"]),
            ]
        ]
    )
    result = _detect_issue_cycle(phases)
    assert result is not None
    assert "Cycle" in result


def test_detect_issue_cycle_unknown_dep_is_safe() -> None:
    """`_detect_issue_cycle` returns None when a dep title doesn't exist in the graph."""
    phases = _make_phases([[("Issue A", ["Nonexistent Issue"])]])
    assert _detect_issue_cycle(phases) is None


def test_detect_issue_cycle_mixed_acyclic_and_cyclic() -> None:
    """`_detect_issue_cycle` detects a cycle even when other issues are acyclic."""
    phases = _make_phases(
        [
            [
                ("Clean A", []),
                ("Clean B", ["Clean A"]),
                ("Cycle X", ["Cycle Y"]),
                ("Cycle Y", ["Cycle X"]),
            ]
        ]
    )
    result = _detect_issue_cycle(phases)
    assert result is not None
    assert "Cycle" in result
