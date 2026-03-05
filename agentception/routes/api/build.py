from __future__ import annotations

"""Build phase API routes.

Three audiences:

1. **The Build UI** — ``POST /api/build/dispatch`` (issue-scoped leaf) and
   ``POST /api/build/dispatch-label`` (label-scoped manager/root) create a
   worktree, ``.agent-task`` file, and a ``pending_launch`` DB record.

2. **The AgentCeption Coordinator / Dispatcher** — ``GET /api/build/pending-launches``
   exposes the launch queue; ``POST /api/build/acknowledge/{run_id}``
   atomically claims a run; ``POST /api/build/spawn-child`` lets any manager
   agent create a child node atomically (worktree + .agent-task + DB + ack).

3. **Running agents** — ``POST /api/build/report/*`` lets agents push
   structured lifecycle events back to AgentCeption.

See ``agentception/docs/agent-tree-protocol.md`` for the full tier spec.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from typing import Literal

from agentception.config import settings
from agentception.db.persist import acknowledge_agent_run, persist_agent_event, persist_agent_run_dispatch
from agentception.db.queries import (
    get_agent_run_teardown,
    get_label_context,
    get_pending_launches,
)
from agentception.mcp.plan_advance_phase import plan_advance_phase as _plan_advance_phase
from agentception.routes.api._shared import _resolve_cognitive_arch
from agentception.services.spawn_child import (
    NodeType,
    SpawnChildError,
    ScopeType,
    spawn_child,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/build", tags=["build"])

# ---------------------------------------------------------------------------
# Dispatch — create a worktree + .agent-task for one issue
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class DispatchRequest(BaseModel):
    """Request body for ``POST /api/build/dispatch``."""

    issue_number: int
    issue_title: str
    issue_body: str = ""
    """Issue body text used to derive skill domains for the cognitive arch."""
    role: str
    """Role slug from ``agentception/.cursor/roles/`` (e.g. ``python-developer``)."""
    repo: str
    """``owner/repo`` string (e.g. ``cgcardona/agentception``)."""


class DispatchResponse(BaseModel):
    """Successful dispatch response."""

    run_id: str
    worktree: str
    host_worktree: str
    branch: str
    agent_task_path: str
    batch_id: str
    status: str = "pending_launch"


def _make_batch_id(issue_number: int) -> str:
    """Generate a deterministic-but-unique batch id for this dispatch."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:4]
    return f"issue-{issue_number}-{stamp}-{short}"


@router.post("/dispatch", response_model=DispatchResponse)
async def dispatch_agent(req: DispatchRequest) -> DispatchResponse:
    """Create a worktree, ``.agent-task``, and a ``pending_launch`` DB record.

    The worktree is the isolated git checkout the agent will work in.
    The ``.agent-task`` file is the agent's full briefing — role, scope,
    repo, callbacks.  The ``pending_launch`` DB record is what the
    AgentCeption Dispatcher reads via ``build_get_pending_launches`` to know
    what to spawn next.

    Agents are NOT launched here.  The Dispatcher (a Cursor prompt the user
    pastes once per wave) polls the pending queue and spawns the right role —
    which may be a leaf worker, a VP, or a CTO depending on what was selected.

    Raises:
        HTTPException 409: Worktree already exists.
        HTTPException 500: git worktree add or .agent-task write failed.
    """
    run_id = f"issue-{req.issue_number}"
    slug = f"issue-{req.issue_number}"
    branch = f"feat/issue-{req.issue_number}"
    batch_id = _make_batch_id(req.issue_number)
    worktree_path = str(Path(settings.worktrees_dir) / slug)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / slug)

    if Path(worktree_path).exists():
        raise HTTPException(
            status_code=409,
            detail=f"Worktree already exists at {worktree_path}. Remove it before re-dispatching.",
        )

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(settings.repo_dir),
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.error("❌ dispatch: git worktree add failed — %s", err)
        raise HTTPException(status_code=500, detail=f"git worktree add failed: {err}")

    logger.info("✅ dispatch: worktree created at %s", worktree_path)

    ac_url = settings.ac_url
    role_file = str(Path(settings.repo_dir) / ".agentception" / "roles" / f"{req.role}.md")
    cognitive_arch = _resolve_cognitive_arch(req.issue_body, req.role)
    agent_task = (
        f"RUN_ID={run_id}\n"
        f"ISSUE_NUMBER={req.issue_number}\n"
        f"ISSUE_TITLE={req.issue_title}\n"
        f"ROLE={req.role}\n"
        f"ROLE_FILE={role_file}\n"
        f"GH_REPO={req.repo}\n"
        f"BRANCH={branch}\n"
        f"WORKTREE={host_worktree_path}\n"
        f"BATCH_ID={batch_id}\n"
        f"SPAWN_MODE=dispatcher\n"
        f"COGNITIVE_ARCH={cognitive_arch}\n"
        f"AC_URL={ac_url}\n"
        f"\n"
        f"# How this works\n"
        f"# ──────────────\n"
        f"# 1. Read your role file at ROLE_FILE to understand your scope and children.\n"
        f"# 2. If you are a leaf worker: read the issue, implement, open PR.\n"
        f"#    If you are a manager: survey GitHub and spawn child agents via Task tool.\n"
        f"# 3. Report progress via MCP tools (preferred) or HTTP:\n"
        f"#      curl -s -X POST {ac_url}/api/build/report/step"
        f' -H "Content-Type: application/json"'
        f" -d '{{\"issue_number\":{req.issue_number},\"step_name\":\"<step>\",\"agent_run_id\":\"{run_id}\"}}'\n"
        f"#      curl -s -X POST {ac_url}/api/build/report/done"
        f' -H "Content-Type: application/json"'
        f" -d '{{\"issue_number\":{req.issue_number},\"pr_url\":\"<url>\",\"agent_run_id\":\"{run_id}\"}}'\n"
    )

    agent_task_path = str(Path(worktree_path) / ".agent-task")
    try:
        Path(agent_task_path).write_text(agent_task, encoding="utf-8")
    except Exception as exc:
        cleanup = await asyncio.create_subprocess_exec(
            "git", "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(settings.repo_dir),
        )
        await cleanup.communicate()
        logger.error("❌ dispatch: .agent-task write failed, worktree cleaned up — %s", exc)
        raise HTTPException(status_code=500, detail=f".agent-task write failed: {exc}") from exc

    logger.info("✅ dispatch: .agent-task written to %s", agent_task_path)

    # Write pending_launch record — this is what the Dispatcher reads.
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=req.issue_number,
        role=req.role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=cognitive_arch,
    )

    return DispatchResponse(
        run_id=run_id,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        branch=branch,
        agent_task_path=agent_task_path,
        batch_id=batch_id,
        status="pending_launch",
    )


# ---------------------------------------------------------------------------
# Node type helpers for the dispatch-label endpoint
# ---------------------------------------------------------------------------

#: Role slugs known to be coordinators (spawn children rather than working directly).
_COORDINATOR_ROLES: frozenset[str] = frozenset({
    # C-suite
    "cto", "csto", "ceo", "cpo", "coo", "cdo", "cfo", "ciso", "cmo",
    # Domain coordinators
    "engineering-coordinator", "qa-coordinator", "coordinator", "conductor",
    "platform-coordinator", "infrastructure-coordinator", "data-coordinator",
    "ml-coordinator", "design-coordinator", "mobile-coordinator",
    "security-coordinator", "product-coordinator",
})


def _node_type_for_role(role: str) -> NodeType:
    """Return ``"coordinator"`` for roles that spawn children; ``"leaf"`` otherwise."""
    return "coordinator" if role in _COORDINATOR_ROLES else "leaf"


#: Map role slug prefixes/exact slugs to their logical org domain.
#: Used by dispatch-label to pre-populate LOGICAL_TIER in the .agent-task file
#: so the dashboard can visualise the node in the correct branch of the org tree.
_ROLE_DOMAIN: dict[str, str] = {
    "cto": "c-suite",
    "ceo": "c-suite",
    "cpo": "c-suite",
    "coo": "c-suite",
    "cdo": "c-suite",
    "cfo": "c-suite",
    "ciso": "c-suite",
    "cmo": "c-suite",
    "csto": "c-suite",
    "engineering-coordinator": "engineering",
    "qa-coordinator": "qa",
    "platform-coordinator": "platform",
    "infrastructure-coordinator": "infrastructure",
    "data-coordinator": "data",
    "ml-coordinator": "ml",
    "design-coordinator": "design",
    "mobile-coordinator": "mobile",
    "security-coordinator": "security",
    "product-coordinator": "product",
    "pr-reviewer": "qa",
}

_ROLE_DOMAIN_PREFIXES: list[tuple[str, str]] = [
    ("python-", "engineering"),
    ("js-", "engineering"),
    ("frontend-", "engineering"),
    ("backend-", "engineering"),
    ("infra-", "infrastructure"),
    ("data-", "data"),
    ("ml-", "ml"),
    ("security-", "security"),
    ("mobile-", "mobile"),
    ("design-", "design"),
]


def _logical_tier_for_role(role: str) -> str | None:
    """Return the org domain for a role slug, or ``None`` when unknown."""
    if role in _ROLE_DOMAIN:
        return _ROLE_DOMAIN[role]
    for prefix, domain in _ROLE_DOMAIN_PREFIXES:
        if role.startswith(prefix):
            return domain
    # Unknown role — caller can set LOGICAL_TIER explicitly via spawn_child.
    return None


# ---------------------------------------------------------------------------
# dispatch-label — launch a manager or root agent scoped to a GitHub label
# ---------------------------------------------------------------------------


class LabelDispatchRequest(BaseModel):
    """Request body for ``POST /api/build/dispatch-label``.

    *scope* is the primary selector:

    - ``"full_initiative"`` — a coordinator agent surveys every open ticket
      under *label* and assembles its own child team.  ``node_type`` is
      ``"coordinator"``.
    - ``"phase"`` — a coordinator handles just one phase sub-label; supply
      *scope_label* with the sub-label string.  ``node_type`` is
      ``"coordinator"``.
    - ``"issue"`` — a single leaf agent works on one issue; supply
      *scope_issue_number*.  ``node_type`` is ``"leaf"``.

    *role* is optional.  When omitted the server derives a sensible default
    (``cto`` for ``full_initiative``, ``engineering-coordinator`` for
    ``phase``, and ``python-developer`` for ``issue``).
    """

    label: str
    """Initiative label string, e.g. ``ac-workflow``."""
    scope: Literal["full_initiative", "phase", "issue"] = "full_initiative"
    """Determines the node_type and SCOPE_VALUE written to .agent-task."""
    scope_label: str | None = None
    """Phase sub-label when *scope* is ``"phase"``."""
    scope_issue_number: int | None = None
    """Issue number when *scope* is ``"issue"``."""
    role: str | None = None
    """Entry role override.  Derived from *scope* when omitted."""
    repo: str
    """``owner/repo`` string."""
    parent_run_id: str | None = None
    """Run ID of the agent that is spawning this one (spawn-lineage tracking)."""


class LabelDispatchResponse(BaseModel):
    """Successful label-dispatch response."""

    run_id: str
    node_type: str
    role: str
    label: str
    worktree: str
    host_worktree: str
    agent_task_path: str
    batch_id: str
    status: str = "pending_launch"


def _label_slug(label: str) -> str:
    """Turn a GitHub label into a filesystem-safe slug."""
    return _SLUG_RE.sub("-", label.lower()).strip("-")[:48]


def _make_label_batch_id(label: str) -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:4]
    slug = _label_slug(label)
    return f"label-{slug}-{stamp}-{short}"


def _role_and_node_type_for_scope(
    scope: Literal["full_initiative", "phase", "issue"],
    role_override: str | None,
) -> tuple[str, NodeType]:
    """Derive the effective role and node_type from the launch scope.

    The scope is the primary structural signal — it determines ``node_type``
    first.  The role is inferred from scope when *role_override* is absent.
    """
    if scope == "issue":
        node_type: NodeType = "leaf"
        default_role = "python-developer"
    else:
        # full_initiative or phase → always a coordinator
        node_type = "coordinator"
        default_role = "cto" if scope == "full_initiative" else "engineering-coordinator"
    role = role_override.strip() if role_override and role_override.strip() else default_role
    return role, node_type


class PhaseSummaryItem(BaseModel):
    """One phase sub-label and its open-issue count, for the launch modal picker."""

    label: str
    count: int


class IssueSummaryItem(BaseModel):
    """A minimal open-issue descriptor for the launch modal single-ticket picker."""

    number: int
    title: str


class LabelContextResponse(BaseModel):
    """Response shape for ``GET /api/build/label-context``."""

    phases: list[PhaseSummaryItem]
    issues: list[IssueSummaryItem]


@router.get("/label-context", response_model=LabelContextResponse)
async def get_label_context_route(
    label: str,
    repo: str,
) -> LabelContextResponse:
    """Return phases and open issues for *label* so the Launch modal can populate pickers.

    Response shape::

        {
          "phases": [{"label": "ac-workflow/5-plan-step-v2", "count": 3}, ...],
          "issues": [{"number": 108, "title": "..."}, ...]
        }

    Falls back to empty lists when the initiative has no recorded data yet.
    """
    ctx = await get_label_context(repo=repo, initiative_label=label)
    return LabelContextResponse(
        phases=[PhaseSummaryItem(label=p["label"], count=p["count"]) for p in ctx["phases"]],
        issues=[IssueSummaryItem(number=i["number"], title=i["title"]) for i in ctx["issues"]],
    )


@router.post("/dispatch-label", response_model=LabelDispatchResponse)
async def dispatch_label_agent(req: LabelDispatchRequest) -> LabelDispatchResponse:
    """Launch an agent scoped to a GitHub label (initiative or phase) or a single issue.

    *scope* drives the structural classification:

    - ``"full_initiative"`` → coordinator, surveys all tickets, spawns child team.
    - ``"phase"`` → coordinator, owns one phase sub-label only.
    - ``"issue"`` → leaf, works on a single ticket.

    A worktree is always created so the agent runs in an isolated checkout.

    Raises:
        HTTPException 409: Worktree already exists.
        HTTPException 500: git worktree or .agent-task write failed.
    """
    role, node_type = _role_and_node_type_for_scope(req.scope, req.role)
    logical_tier = _logical_tier_for_role(role)

    # The effective scope label/value written to .agent-task
    if req.scope == "phase" and req.scope_label:
        scope_value = req.scope_label
        scope_type = "label"
    elif req.scope == "issue" and req.scope_issue_number is not None:
        scope_value = str(req.scope_issue_number)
        scope_type = "issue"
    else:
        scope_value = req.label
        scope_type = "label"

    logger.warning(
        "🚀 dispatch-label: scope=%r role=%r node_type=%r scope_value=%r repo=%r",
        req.scope, role, node_type, scope_value, req.repo,
    )

    label_slug = _label_slug(req.label)
    batch_id = _make_label_batch_id(req.label)
    run_id = f"label-{label_slug}-{uuid.uuid4().hex[:6]}"
    branch = f"agent/{label_slug}-{uuid.uuid4().hex[:4]}"

    worktree_path = str(Path(settings.worktrees_dir) / run_id)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / run_id)
    logger.warning(
        "🚀 dispatch-label: run_id=%r node_type=%r logical_tier=%r worktree_path=%r host_worktree_path=%r",
        run_id, node_type, logical_tier, worktree_path, host_worktree_path,
    )

    if Path(worktree_path).exists():
        raise HTTPException(
            status_code=409,
            detail=f"Worktree already exists at {worktree_path}.",
        )

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(settings.repo_dir),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.error("❌ dispatch-label: git worktree add failed — %s", err)
        raise HTTPException(status_code=500, detail=f"git worktree add failed: {err}")

    logger.info("✅ dispatch-label: worktree %s for label %r node_type=%s", worktree_path, req.label, node_type)

    ac_url = settings.ac_url
    role_file = str(Path(settings.repo_dir) / ".agentception" / "roles" / f"{role}.md")
    label_cognitive_arch = _resolve_cognitive_arch("", role)

    parent_run_id_val = req.parent_run_id or ""
    logical_tier_line = f"LOGICAL_TIER={logical_tier}\n" if logical_tier else ""
    agent_task = (
        f"# AgentCeption agent briefing — generated by dispatch-label\n"
        f"# See agentception/docs/agent-tree-protocol.md for the full spec.\n\n"
        f"RUN_ID={run_id}\n"
        f"ROLE={role}\n"
        f"NODE_TYPE={node_type}\n"
        f"{logical_tier_line}"
        f"SCOPE_TYPE={scope_type}\n"
        f"SCOPE_VALUE={scope_value}\n"
        f"INITIATIVE_LABEL={req.label}\n"
        f"GH_REPO={req.repo}\n"
        f"BRANCH={branch}\n"
        f"WORKTREE={host_worktree_path}\n"
        f"BATCH_ID={batch_id}\n"
        f"PARENT_RUN_ID={parent_run_id_val}\n"
        f"AC_URL={ac_url}\n"
        f"ROLE_FILE={role_file}\n"
        f"COGNITIVE_ARCH={label_cognitive_arch}\n"
        f"\n"
        f"# GitHub queries for this node (node_type={node_type}, scope_type={scope_type}):\n"
    )

    if node_type == "coordinator":
        agent_task += (
            f"# gh issue list --repo {req.repo} --label '{scope_value}' --state open --json number,title,labels --limit 200\n"
            f"# gh pr list --repo {req.repo} --base dev --state open --json number,title,headRefName --limit 200\n"
        )

    agent_task_path = str(Path(worktree_path) / ".agent-task")
    try:
        Path(agent_task_path).write_text(agent_task, encoding="utf-8")
    except Exception as exc:
        cleanup = await asyncio.create_subprocess_exec(
            "git", "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(settings.repo_dir),
        )
        await cleanup.communicate()
        logger.error("❌ dispatch-label: .agent-task write failed — %s", exc)
        raise HTTPException(status_code=500, detail=f".agent-task write failed: {exc}") from exc

    logger.warning("✅ dispatch-label: .agent-task written to %s", agent_task_path)

    logger.warning(
        "🚀 dispatch-label: calling persist_agent_run_dispatch run_id=%r host_worktree_path=%r",
        run_id, host_worktree_path,
    )
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=req.scope_issue_number if (req.scope == "issue" and req.scope_issue_number is not None) else 0,
        role=role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=label_cognitive_arch,
        node_type=node_type,
        logical_tier=logical_tier,
        parent_run_id=req.parent_run_id,
    )
    logger.warning("✅ dispatch-label: persist_agent_run_dispatch complete — run_id=%r is now pending_launch", run_id)

    return LabelDispatchResponse(
        run_id=run_id,
        node_type=node_type,
        role=role,
        label=req.label,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        agent_task_path=agent_task_path,
        batch_id=batch_id,
        status="pending_launch",
    )


# ---------------------------------------------------------------------------
# Dispatcher prompt — serve the coordinator prompt so the UI can copy it
# ---------------------------------------------------------------------------

_DISPATCHER_PROMPT_PATH = Path(settings.repo_dir) / ".agentception" / "dispatcher.md"


@router.get("/dispatcher-prompt")
async def get_dispatcher_prompt() -> dict[str, object]:
    """Return the Dispatcher prompt markdown so the UI can offer a one-click copy.

    The prompt lives at ``.agentception/dispatcher.md`` in the repo.
    Returns ``{"content": "<markdown>", "path": "<rel path>"}`` or a 404 if
    the file is missing.
    """
    path = settings.ac_dir / "dispatcher.md"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Dispatcher prompt not found at .agentception/dispatcher.md",
        )
    content = path.read_text(encoding="utf-8")
    return {"content": content, "path": ".agentception/dispatcher.md"}


# ---------------------------------------------------------------------------
# Pending launches — Dispatcher reads this to know what to spawn
# ---------------------------------------------------------------------------


@router.get("/pending-launches")
async def list_pending_launches() -> dict[str, object]:
    """Return all runs waiting to be claimed by the Dispatcher.

    The AgentCeption Dispatcher calls this once at startup to discover what
    the UI has queued.  Each item includes the run_id, role, issue number,
    and host-side worktree path so the Dispatcher can spawn the right agent
    at the right level of the tree (leaf worker, VP, or CTO).
    """
    launches = await get_pending_launches()
    return {"pending": launches, "count": len(launches)}


@router.post("/acknowledge/{run_id}")
async def acknowledge_launch(run_id: str) -> dict[str, object]:
    """Atomically claim a pending run before spawning its Task agent.

    The Dispatcher calls this immediately before it spawns the Task so the
    run cannot be double-claimed if two Dispatchers run concurrently.
    Transitions the run from ``pending_launch`` → ``implementing``.

    Returns ``{"ok": true}`` on success or ``{"ok": false, "reason": "..."}``
    when the run was not found or already claimed (idempotency guard).
    """
    ok = await acknowledge_agent_run(run_id)
    if not ok:
        return {"ok": False, "reason": f"Run {run_id!r} not found or not in pending_launch state"}
    logger.info("✅ acknowledge_launch: %s claimed", run_id)
    return {"ok": True, "run_id": run_id}


# ---------------------------------------------------------------------------
# spawn-child — universal child-node creation for manager agents
# ---------------------------------------------------------------------------


class SpawnChildRequest(BaseModel):
    """Request body for ``POST /api/build/spawn-child``."""

    parent_run_id: str
    """``run_id`` of the calling manager agent (for lineage tracking)."""
    role: str
    """Child's role slug (e.g. ``"engineering-coordinator"``, ``"python-developer"``)."""
    node_type: NodeType
    """``"coordinator"`` if the child spawns children; ``"leaf"`` if it works one issue/PR."""
    logical_tier: str | None = None
    """Organisational domain for UI visualisation (e.g. ``"qa"``, ``"engineering"``).
    Optional — when omitted the field is absent from the ``.agent-task`` file.
    A chain-spawned PR reviewer should pass ``"qa"`` so the dashboard places it
    under the QA branch even though its ``parent_run_id`` points to an engineering leaf."""
    scope_type: Literal["label", "issue", "pr"]
    """``"label"``, ``"issue"``, or ``"pr"``."""
    scope_value: str
    """Label string, issue number, or PR number (as string)."""
    gh_repo: str
    """``"owner/repo"`` string."""
    issue_body: str = ""
    """Issue body for COGNITIVE_ARCH skill extraction (issue-scoped children)."""
    issue_title: str = ""
    """Issue title written to ISSUE_TITLE field."""
    skills_hint: list[str] | None = None
    """Explicit skill list; bypasses keyword extraction when provided."""


class SpawnChildResponse(BaseModel):
    """Successful response from ``POST /api/build/spawn-child``."""

    run_id: str
    host_worktree_path: str
    worktree_path: str
    node_type: str
    logical_tier: str | None = None
    role: str
    cognitive_arch: str
    agent_task_path: str
    scope_type: str
    scope_value: str
    status: str = "implementing"


@router.post("/spawn-child", response_model=SpawnChildResponse)
async def spawn_child_node(req: SpawnChildRequest) -> SpawnChildResponse:
    """Atomically create a child node in the agent tree.

    Any manager agent (CTO, coordinator, or future tier) calls this endpoint
    to create a child with a worktree, ``.agent-task``, DB record, and
    auto-acknowledgement — all in a single atomic operation.

    The caller receives ``host_worktree_path`` and ``run_id``, then
    immediately fires a Task tool call with the briefing:

        "Read your .agent-task file at {host_worktree_path}/.agent-task
         and follow the instructions for your role."

    This endpoint is the canonical way to grow the agent tree at runtime.
    It replaces the previous pattern of manager agents manually creating
    worktrees and writing ``.agent-task`` files with hand-rolled shell
    scripts embedded in role files.

    Raises:
        HTTPException 422: Invalid ``scope_type`` value.
        HTTPException 500: Worktree creation or file I/O failure.
    """
    try:
        result = await spawn_child(
            parent_run_id=req.parent_run_id,
            role=req.role,
            node_type=req.node_type,
            logical_tier=req.logical_tier,
            scope_type=req.scope_type,
            scope_value=req.scope_value,
            gh_repo=req.gh_repo,
            issue_body=req.issue_body,
            issue_title=req.issue_title,
            skills_hint=req.skills_hint,
        )
    except SpawnChildError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return SpawnChildResponse(**result.to_dict(), status="implementing")


# ---------------------------------------------------------------------------
# Agent callbacks — agents POST to these from inside their worktree
# ---------------------------------------------------------------------------


class StepReport(BaseModel):
    issue_number: int
    step_name: str
    agent_run_id: str | None = None


class BlockerReport(BaseModel):
    issue_number: int
    description: str
    agent_run_id: str | None = None


class DecisionReport(BaseModel):
    issue_number: int
    decision: str
    rationale: str
    agent_run_id: str | None = None


class DoneReport(BaseModel):
    issue_number: int
    pr_url: str
    summary: str = ""
    agent_run_id: str | None = None


@router.post("/report/step")
async def report_step(req: StepReport) -> dict[str, object]:
    """Agent reports starting a named execution step."""
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="step_start",
        payload={"step": req.step_name},
        agent_run_id=req.agent_run_id,
    )
    logger.info("✅ report_step: issue=%d step=%r", req.issue_number, req.step_name)
    return {"ok": True}


@router.post("/report/blocker")
async def report_blocker(req: BlockerReport) -> dict[str, object]:
    """Agent reports being blocked."""
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="blocker",
        payload={"description": req.description},
        agent_run_id=req.agent_run_id,
    )
    logger.warning(
        "⚠️ report_blocker: issue=%d — %s", req.issue_number, req.description
    )
    return {"ok": True}


@router.post("/report/decision")
async def report_decision(req: DecisionReport) -> dict[str, object]:
    """Agent records an architectural decision."""
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="decision",
        payload={"decision": req.decision, "rationale": req.rationale},
        agent_run_id=req.agent_run_id,
    )
    logger.info(
        "✅ report_decision: issue=%d decision=%r", req.issue_number, req.decision
    )
    return {"ok": True}


async def _teardown_agent_worktree(run_id: str) -> None:
    """Remove the worktree and delete the remote branch for a completed agent run.

    Called non-blocking from ``report_done`` — errors are logged but never
    propagated so a cleanup failure cannot break the agent's done response.

    Steps:
    1. Look up ``worktree_path`` and ``branch`` from ``agent_runs``.
    2. ``git worktree remove --force <path>`` — removes the checkout directory.
    3. ``git worktree prune`` — removes stale git internal metadata.
    4. ``git push origin --delete <branch>`` — removes the remote branch so
       GitHub does not accumulate stale refs from every completed agent run.
    """
    teardown = await get_agent_run_teardown(run_id)
    if teardown is None:
        logger.warning("⚠️  _teardown_agent_worktree: no DB row for run_id=%r", run_id)
        return

    repo_dir = str(settings.repo_dir)
    worktree_path = teardown["worktree_path"]
    branch = teardown["branch"]

    # ── 1. Remove the worktree directory ─────────────────────────────────────
    if worktree_path and Path(worktree_path).exists():
        rm_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await rm_proc.communicate()
        if rm_proc.returncode == 0:
            logger.info("✅ _teardown: removed worktree %s", worktree_path)
        else:
            logger.warning("⚠️  _teardown: worktree remove failed: %s", stderr.decode().strip())

    # ── 2. Prune git metadata ─────────────────────────────────────────────────
    prune_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_dir, "worktree", "prune",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await prune_proc.communicate()

    # ── 3. Delete the remote branch ───────────────────────────────────────────
    if branch:
        push_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "push", "origin", "--delete", branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, push_stderr = await push_proc.communicate()
        if push_proc.returncode == 0:
            logger.info("✅ _teardown: deleted remote branch %r", branch)
        else:
            # Not a fatal error — branch may have been deleted already by
            # GitHub's auto-delete-on-merge or a previous teardown attempt.
            logger.info(
                "ℹ️  _teardown: remote branch %r not deleted (may already be gone): %s",
                branch,
                push_stderr.decode().strip(),
            )


@router.post("/report/done")
async def report_done(req: DoneReport) -> dict[str, object]:
    """Agent reports completion, links the PR, and tears down its worktree.

    The worktree removal and remote branch deletion run as a background task
    so the agent receives an immediate ``{"ok": True}`` response and is not
    blocked waiting for git operations to complete.
    """
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="done",
        payload={"pr_url": req.pr_url, "summary": req.summary},
        agent_run_id=req.agent_run_id,
    )
    logger.info(
        "✅ report_done: issue=%d pr_url=%r", req.issue_number, req.pr_url
    )
    if req.agent_run_id:
        asyncio.create_task(
            _teardown_agent_worktree(req.agent_run_id),
            name=f"teardown-{req.agent_run_id}",
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Phase gate — advance a phase by unlocking all to_phase issues
# ---------------------------------------------------------------------------


class AdvancePhaseRequest(BaseModel):
    """Request body for ``POST /api/build/advance-phase``."""

    initiative: str
    """The initiative label shared by all phase issues (e.g. ``"my-initiative"``)."""
    from_phase: str
    """The phase label that must be fully closed (e.g. ``"phase-1"``)."""
    to_phase: str
    """The phase label to unlock (e.g. ``"phase-2"``)."""


class AdvancePhaseOk(BaseModel):
    """Successful phase advance — all from_phase issues were closed."""

    advanced: bool
    unlocked_count: int


class AdvancePhaseBlocked(BaseModel):
    """Blocked phase advance — one or more from_phase issues remain open."""

    advanced: bool
    error: str
    open_issues: list[int]


@router.post("/advance-phase", response_model=None)
async def advance_phase(
    req: AdvancePhaseRequest,
    response: Response,
) -> AdvancePhaseOk | AdvancePhaseBlocked:
    """Advance the phase gate by unlocking all *to_phase* issues.

    Delegates to ``plan_advance_phase()`` which validates the gate condition
    (all from_phase issues closed) and mutates GitHub labels atomically.

    On success: sets ``HX-Trigger: refreshBoard`` so the Build board partial
    auto-refreshes in the same HTMX response cycle without a full-page reload.

    Returns:
        ``AdvancePhaseOk`` when the gate passes and labels are mutated.
        ``AdvancePhaseBlocked`` when open issues still block the transition.
    """
    result = await _plan_advance_phase(req.initiative, req.from_phase, req.to_phase)

    if result.get("advanced") is True:
        unlocked_raw = result.get("unlocked_count")
        unlocked_count = unlocked_raw if isinstance(unlocked_raw, int) else 0
        response.headers["HX-Trigger"] = "refreshBoard"
        logger.info(
            "✅ advance_phase: %r → %r, %d issue(s) unlocked",
            req.from_phase,
            req.to_phase,
            unlocked_count,
        )
        return AdvancePhaseOk(advanced=True, unlocked_count=unlocked_count)

    error_raw = result.get("error")
    error_str = error_raw if isinstance(error_raw, str) else "Phase advance blocked."
    open_raw = result.get("open_issues")
    open_issues: list[int] = (
        [i for i in open_raw if isinstance(i, int)]
        if isinstance(open_raw, list)
        else []
    )
    logger.warning("⚠️ advance_phase: blocked — %s", error_str)
    return AdvancePhaseBlocked(advanced=False, error=error_str, open_issues=open_issues)
