"""API routes: POST /api/plan/launch.

POST /api/plan/launch
---------------------
Input:  PlanLaunchRequest(yaml_text: str)  — YAML-encoded EnrichedManifest
Output: PlanLaunchResponse                 — run_id, worktree, host_worktree,
                                             branch, batch_id from the coordinator spawn

Steps:
1. Parse yaml_text → dict (422 on YAML syntax error).
2. Validate as EnrichedManifest via Pydantic (422 on field errors including
   the phase DAG invariant — EnrichedManifest.validate_phase_dag enforces that
   phases depend only on earlier phases, which prevents phase-level cycles).
3. Detect issue-level cycles in the title-based depends_on graph (422 with
   a human-readable cycle description).
4. Call spawn_child() to create a coordinator worktree backed by the DB.
5. Return PlanLaunchResponse immediately.

Boundary: zero imports from external packages.
"""
from __future__ import annotations

import logging

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentception.config import settings
from agentception.models import EnrichedManifest, EnrichedPhase
from agentception.services.spawn_child import SpawnChildError, spawn_child

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plan", tags=["plan"])


# ---------------------------------------------------------------------------
# POST /api/plan/launch — validate EnrichedManifest, spawn coordinator
# ---------------------------------------------------------------------------


class PlanLaunchRequest(BaseModel):
    """Request body for POST /api/plan/launch.

    ``yaml_text`` is the YAML-encoded :class:`~agentception.models.EnrichedManifest`
    produced by the Plan UI editor after the user reviews the LLM-generated plan.
    It must validate against :class:`~agentception.models.EnrichedManifest`; any YAML
    syntax error or schema mismatch returns 422 before the coordinator is spawned.
    """

    yaml_text: str


class PlanLaunchResponse(BaseModel):
    """Response returned after the coordinator worktree has been spawned.

    All task context is DB-backed — the coordinator reads its briefing via the
    ``task/briefing`` MCP prompt and ``ac://runs/{run_id}/context`` resource.
    """

    run_id: str
    worktree: str
    host_worktree: str
    batch_id: str


def _detect_issue_cycle(phases: list[EnrichedPhase]) -> str | None:
    """Return a human-readable cycle description if issue depends_on titles cycle.

    Iterates across all phases and builds a global issue title → depends_on
    mapping, then runs DFS to find any back-edge.  Returns ``None`` when the
    graph is acyclic.

    Note: Phase-level DAG validation (phases only depend on earlier phases) is
    enforced by :class:`~agentception.models.PlanSpec`'s ``validate_phase_dag``
    validator at model construction time, so we only need to check issue-level
    cycles here.

    Args:
        phases: List of :class:`~agentception.models.EnrichedPhase` objects.

    Returns:
        ``None`` when acyclic; a non-empty cycle description string otherwise.
    """
    deps_map: dict[str, list[str]] = {}
    for phase in phases:
        for issue in phase.issues:
            deps_map[issue.title] = list(issue.depends_on)

    visited: set[str] = set()
    in_stack: list[str] = []

    def dfs(node: str) -> str | None:
        if node in in_stack:
            cycle_start = in_stack.index(node)
            cycle_path = in_stack[cycle_start:] + [node]
            return "Cycle detected: " + " → ".join(cycle_path)
        if node in visited:
            return None
        visited.add(node)
        in_stack.append(node)
        for dep in deps_map.get(node, []):
            result = dfs(dep)
            if result is not None:
                return result
        in_stack.pop()
        return None

    for title in deps_map:
        if title not in visited:
            result = dfs(title)
            if result is not None:
                return result
    return None


@router.post("/launch")
async def post_plan_launch(request: PlanLaunchRequest) -> PlanLaunchResponse:
    """Validate an EnrichedManifest YAML, check for issue cycles, and spawn a coordinator.

    Steps:
    1. Parse ``yaml_text`` → dict (422 on YAML syntax error).
    2. Validate as :class:`~agentception.models.EnrichedManifest` via Pydantic
       (422 on field or phase-DAG errors).
    3. Run issue-level cycle detection on the title-based ``depends_on`` graph
       (422 if an issue cycle is found).
    4. Spawn a coordinator using :func:`~agentception.services.spawn_child.spawn_child`
       scoped to the manifest's label prefix.  All task context is persisted to the DB.
    5. Return :class:`PlanLaunchResponse` immediately.

    Returns:
        422 on YAML parse error, field validation error, or detected cycle.
        500 if the coordinator spawn fails unexpectedly.
        200 with :class:`PlanLaunchResponse` on success.
    """
    try:
        raw: object = yaml.safe_load(request.yaml_text)
    except yaml.YAMLError as exc:
        detail = f"YAML parse error: {exc}"
        logger.warning("⚠️ /api/plan/launch — %s", detail)
        raise HTTPException(status_code=422, detail=detail)

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail=f"Expected a YAML mapping at the top level, got {type(raw).__name__}",
        )

    try:
        manifest = EnrichedManifest.model_validate(raw)
    except Exception as exc:
        logger.warning("⚠️ /api/plan/launch — EnrichedManifest validation failed: %s", exc)
        raise HTTPException(status_code=422, detail=f"Manifest validation error: {exc}")

    cycle = _detect_issue_cycle(manifest.phases)
    if cycle is not None:
        logger.warning("⚠️ /api/plan/launch — DAG cycle in issues: %s", cycle)
        raise HTTPException(status_code=422, detail=cycle)

    # Scope the coordinator to the manifest's initiative name.
    scope_value = manifest.initiative.strip() if manifest.initiative else "plan"

    try:
        result = await spawn_child(
            parent_run_id="",
            role="coordinator",
            tier="coordinator",
            org_domain="engineering",
            scope_type="label",
            scope_value=scope_value,
            gh_repo=settings.gh_repo,
        )
    except SpawnChildError as exc:
        logger.error("❌ /api/plan/launch — coordinator spawn failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Coordinator spawn failed: {exc}",
        )

    logger.info(
        "✅ /api/plan/launch — coordinator spawned; run_id=%s worktree=%s",
        result.run_id, result.worktree_path,
    )

    return PlanLaunchResponse(
        run_id=result.run_id,
        worktree=result.worktree_path,
        host_worktree=result.host_worktree_path,
        batch_id=result.run_id,
    )
