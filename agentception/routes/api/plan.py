from __future__ import annotations

"""API routes: POST /api/plan/draft and POST /api/plan/launch.

POST /api/plan/draft
--------------------
Input:  PlanDraftRequest(text: str)  — non-empty, non-whitespace plan text
Output: PlanDraftResponse            — draft_id (uuid4), task_file path,
                                       output_path, status='pending'

Side effects
------------
1. ``git worktree add <worktrees_dir>/plan-draft-<draft_id>`` is executed
   (awaited; it is fast and must succeed before we write the task file).
   The worktree is created under ``settings.worktrees_dir`` — the canonical
   Docker-mounted path (``/worktrees`` in-container, ``~/.agentception/worktrees/agentception``
   on the host) — so mypy/pytest can reference it via ``/worktrees/<name>``
   inside the container without path mismatches.
2. A ``.agent-task`` file is written to the new worktree in TOML v2 format
   (see ``agentception.services.toml_task.render_toml_str``).

POST /api/plan/launch
---------------------
Input:  PlanLaunchRequest(yaml_text: str)  — YAML-encoded EnrichedManifest
Output: PlanLaunchResponse                 — worktree, branch, agent_task_path,
                                             batch_id from the coordinator spawn

Steps:
1. Parse yaml_text → dict (422 on YAML syntax error).
2. Validate as EnrichedManifest via Pydantic (422 on field errors including
   the phase DAG invariant — EnrichedManifest.validate_phase_dag enforces that
   phases depend only on earlier phases, which prevents phase-level cycles).
3. Detect issue-level cycles in the title-based depends_on graph (422 with
   a human-readable cycle description).
4. Call plan_spawn_coordinator(manifest_json) — fire-and-forget style.
5. Return PlanLaunchResponse immediately.

Boundary: zero imports from external packages.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from agentception.config import settings
from agentception.mcp.plan_tools import plan_spawn_coordinator
from agentception.models import EnrichedManifest, EnrichedPhase
from agentception.services.toml_task import TomlValue, render_toml_str

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plan", tags=["plan"])


class PlanDraftRequest(BaseModel):
    """Request body for POST /api/plan/draft.

    ``text`` is the raw plan text submitted by the user.
    Empty or whitespace-only strings are rejected at validation time (422).
    """

    text: str

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, v: str) -> str:
        """Reject empty or whitespace-only plan text before the handler runs."""
        if not v or not v.strip():
            raise ValueError("text must not be empty or whitespace-only")
        return v


class PlanDraftResponse(BaseModel):
    """Response returned immediately after dispatching the plan draft.

    The caller should poll or subscribe for progress; this endpoint is
    intentionally fire-and-forget from the HTTP perspective.

    ``output_path`` is the absolute path of the YAML file the Cursor agent will
    write when it completes — not the worktree directory.  The AgentCeption
    poller watches this path and emits a ``plan_draft_ready`` SSE event when
    the file appears.
    """

    draft_id: str
    task_file: str
    output_path: str
    status: str = "pending"


@router.post("/draft")
async def post_plan_draft(request: PlanDraftRequest) -> PlanDraftResponse:
    """Accept plan text, create a git worktree, and write an .agent-task file.

    Steps:
    1. Generate a uuid4 draft_id.
    2. Run ``git worktree add <worktrees_dir>/plan-draft-<draft_id>`` off origin/dev.
       Uses the canonical ``settings.worktrees_dir`` so the path is visible inside
       Docker at ``/worktrees/plan-draft-<draft_id>`` — never ``/tmp/``.
    3. Write a TOML v2 .agent-task to that path so a Cursor agent can pick it up.
    4. Return PlanDraftResponse immediately.

    Returns 422 if text is empty/whitespace (validated by PlanDraftRequest).
    Returns 500 if the git worktree add subprocess fails.
    """
    draft_id = str(uuid.uuid4())
    slug = f"plan-draft-{draft_id}"
    branch = f"feat/{slug}"
    worktree_path = settings.worktrees_dir / slug
    host_worktree_path = settings.host_worktrees_dir / slug
    task_file_path = worktree_path / ".agent-task"
    # The output file is where the Cursor agent writes the finished PlanSpec YAML.
    # The AgentCeption poller watches *this file* (not the directory) and emits
    # ``plan_draft_ready`` when it appears on disk.
    output_file_path = host_worktree_path / ".plan-output.yaml"

    logger.info("✅ Plan draft %s — creating worktree at %s", draft_id, worktree_path)

    repo_dir = str(settings.repo_dir)
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir,
        "worktree", "add", "-b", branch,
        str(worktree_path), "origin/dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.error("❌ git worktree add failed for draft %s: %s", draft_id, err)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create worktree for draft {draft_id}: {err}",
        )

    logger.info("✅ Worktree created — writing .agent-task for draft %s", draft_id)

    # Ensure the directory exists. ``git worktree add`` creates it, but we
    # mkdir with exist_ok=True so that the write is safe even if the caller
    # mocked the subprocess in tests.
    worktree_path.mkdir(parents=True, exist_ok=True)

    sections: dict[str, dict[str, TomlValue]] = {
        "task": {
            "version": "2.0",
            "workflow": "plan-spec",
            "id": draft_id,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "attempt_n": 0,
            "required_output": "plan_yaml",
            "on_block": "stop",
        },
        "output": {
            "draft_id": draft_id,
            "path": str(output_file_path),
        },
        "agent": {
            "role": "planner",
            "tier": "coordinator",
            "cognitive_arch": "hopper:python",
        },
        "worktree": {
            "path": str(host_worktree_path),
            "branch": branch,
        },
        "plan_draft": {
            "text": request.text,
            "mcp_tools_hint": "call plan_get_schema() to get the PlanSpec TOML schema first",
            "output_schema": "plan_get_schema",
        },
    }
    task_content = render_toml_str(sections)
    task_file_path.write_text(task_content, encoding="utf-8")

    logger.info("✅ .agent-task written for draft %s", draft_id)

    return PlanDraftResponse(
        draft_id=draft_id,
        task_file=str(task_file_path),
        output_path=str(output_file_path),
        status="pending",
    )


# ---------------------------------------------------------------------------
# POST /api/plan/launch — validate EnrichedManifest, spawn coordinator
# ---------------------------------------------------------------------------


class PlanLaunchRequest(BaseModel):
    """Request body for POST /api/plan/launch.

    ``yaml_text`` is the YAML-encoded :class:`~agentception.models.EnrichedManifest`
    produced by the Cursor agent after processing a brain dump.  It must
    validate against :class:`~agentception.models.EnrichedManifest`; any YAML
    syntax error or schema mismatch returns 422 before the coordinator is
    contacted.
    """

    yaml_text: str


class PlanLaunchResponse(BaseModel):
    """Response returned after the coordinator worktree has been spawned.

    Fields mirror the return value of
    :func:`agentception.mcp.plan_tools.plan_spawn_coordinator`.
    """

    worktree: str
    branch: str
    agent_task_path: str
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
    4. Await :func:`agentception.mcp.plan_tools.plan_spawn_coordinator` with
       the manifest serialised as JSON.
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

    manifest_json: str = json.dumps(manifest.model_dump())

    try:
        result = await plan_spawn_coordinator(manifest_json)
    except Exception as exc:
        logger.error("❌ /api/plan/launch — coordinator spawn failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Coordinator spawn failed: {exc}",
        )

    if "error" in result:
        raise HTTPException(status_code=422, detail=str(result["error"]))

    logger.info(
        "✅ /api/plan/launch — coordinator spawned; worktree=%s", result.get("worktree")
    )

    return PlanLaunchResponse(
        worktree=str(result["worktree"]),
        branch=str(result["branch"]),
        agent_task_path=str(result["agent_task_path"]),
        batch_id=str(result["batch_id"]),
    )
