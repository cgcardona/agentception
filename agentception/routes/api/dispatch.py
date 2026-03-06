from __future__ import annotations

"""Dispatch API routes — launch agents from the Ship UI.

Three endpoints drive the Ship page launch modal:

1. ``GET /api/dispatch/context`` — return phases and open issues for a
   label so the modal can populate its pickers.
2. ``POST /api/dispatch/issue`` — create a worktree + ``.agent-task`` +
   ``pending_launch`` record for a single issue-scoped leaf agent.
3. ``POST /api/dispatch/label`` — same but scoped to an initiative label or
   phase sub-label (spawns a coordinator or leaf depending on *scope*).
4. ``GET /api/dispatch/prompt`` — serve the Dispatcher prompt so the UI
   can offer a one-click copy.

See ``docs/agent-tree-protocol.md`` for the full tier spec.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from typing import Literal

from agentception.config import settings
from agentception.db.persist import persist_agent_run_dispatch
from agentception.db.queries import get_label_context
from agentception.routes.api._shared import _resolve_cognitive_arch
from agentception.services.spawn_child import (
    SpawnChildError,
    ScopeType,
    Tier,
    _tier_to_node_type,
    spawn_child,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dispatch", tags=["dispatch"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# ---------------------------------------------------------------------------
# GET /api/dispatch/context — label context for the launch modal
# ---------------------------------------------------------------------------


class PhaseSummaryItem(BaseModel):
    """One phase sub-label and its open-issue count, for the launch modal picker."""

    label: str
    count: int


class IssueSummaryItem(BaseModel):
    """A minimal open-issue descriptor for the launch modal single-ticket picker."""

    number: int
    title: str


class LabelContextResponse(BaseModel):
    """Response shape for ``GET /api/dispatch/context``."""

    phases: list[PhaseSummaryItem]
    issues: list[IssueSummaryItem]


@router.get("/context", response_model=LabelContextResponse)
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


# ---------------------------------------------------------------------------
# POST /api/dispatch/issue — single-issue leaf dispatch
# ---------------------------------------------------------------------------


class DispatchRequest(BaseModel):
    """Request body for ``POST /api/dispatch/issue``."""

    issue_number: int
    issue_title: str
    issue_body: str = ""
    """Issue body text used to derive skill domains for the cognitive arch."""
    role: str
    """Role slug from ``.agentception/roles/`` (e.g. ``python-developer``)."""
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


@router.post("/issue", response_model=DispatchResponse)
async def dispatch_agent(req: DispatchRequest) -> DispatchResponse:
    """Create a worktree, ``.agent-task``, and a ``pending_launch`` DB record.

    The worktree is the isolated git checkout the agent will work in.
    The ``.agent-task`` file is the agent's full briefing — role, scope,
    repo, callbacks.  The ``pending_launch`` DB record is what the
    AgentCeption Dispatcher reads via ``build_get_pending_launches`` to know
    what to spawn next.

    Agents are NOT launched here.  The Dispatcher polls the pending queue
    and spawns the right role — which may be a leaf worker, a VP, or a CTO
    depending on what was selected.

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
        f"#      curl -s -X POST {ac_url}/api/runs/{run_id}/step"
        f' -H "Content-Type: application/json"'
        f" -d '{{\"issue_number\":{req.issue_number},\"step_name\":\"<step>\"}}'\n"
        f"#      curl -s -X POST {ac_url}/api/runs/{run_id}/done"
        f' -H "Content-Type: application/json"'
        f" -d '{{\"issue_number\":{req.issue_number},\"pr_url\":\"<url>\"}}'\n"
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
# Node type helpers shared by dispatch-label
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


def _tier_for_role(role: str) -> Tier:
    """Return the behavioral tier for a role slug.

    Coordinator roles survey their scope and spawn children → ``coordinator``
    or ``executive`` for C-suite roles.  All other roles are leaf agents:
    PR reviewers → ``reviewer``, everything else → ``engineer``.
    """
    if role in _COORDINATOR_ROLES:
        return "executive" if role in {"cto", "ceo", "cpo", "coo", "cdo", "cfo", "ciso", "cmo", "csto"} else "coordinator"
    if role in {"pr-reviewer", "qa-coordinator"}:
        return "reviewer"
    return "engineer"


#: Map role slug prefixes/exact slugs to their org domain (UI hierarchy slot).
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
    "platform-coordinator": "engineering",
    "infrastructure-coordinator": "engineering",
    "data-coordinator": "engineering",
    "ml-coordinator": "engineering",
    "design-coordinator": "engineering",
    "mobile-coordinator": "engineering",
    "security-coordinator": "engineering",
    "product-coordinator": "engineering",
    "pr-reviewer": "qa",
}

_ROLE_DOMAIN_PREFIXES: list[tuple[str, str]] = [
    ("python-", "engineering"),
    ("js-", "engineering"),
    ("frontend-", "engineering"),
    ("backend-", "engineering"),
    ("infra-", "engineering"),
    ("data-", "engineering"),
    ("ml-", "engineering"),
    ("security-", "engineering"),
    ("mobile-", "engineering"),
    ("design-", "engineering"),
]


def _org_domain_for_role(role: str) -> str | None:
    """Return the org domain (UI hierarchy slot) for a role slug, or ``None`` when unknown."""
    if role in _ROLE_DOMAIN:
        return _ROLE_DOMAIN[role]
    for prefix, domain in _ROLE_DOMAIN_PREFIXES:
        if role.startswith(prefix):
            return domain
    return None


# ---------------------------------------------------------------------------
# POST /api/dispatch/label — launch a manager or root agent scoped to a label
# ---------------------------------------------------------------------------


class LabelDispatchRequest(BaseModel):
    """Request body for ``POST /api/dispatch/label``.

    *scope* is the primary selector:

    - ``"full_initiative"`` — an executive agent surveys every open ticket
      under *label* and assembles its own child team.  ``tier`` is
      ``"executive"``.
    - ``"phase"`` — a coordinator handles just one phase sub-label; supply
      *scope_label* with the sub-label string.  ``tier`` is
      ``"coordinator"``.
    - ``"issue"`` — a single engineer agent works on one issue; supply
      *scope_issue_number*.  ``tier`` is ``"engineer"``.

    *role* is optional.  When omitted the server derives a sensible default
    (``cto`` for ``full_initiative``, ``engineering-coordinator`` for
    ``phase``, and ``python-developer`` for ``issue``).
    """

    label: str
    """Initiative label string, e.g. ``ac-workflow``."""
    scope: Literal["full_initiative", "phase", "issue"] = "full_initiative"
    """Determines the tier and SCOPE_VALUE written to .agent-task."""
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
    tier: str
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


def _role_and_tier_for_scope(
    scope: Literal["full_initiative", "phase", "issue"],
    role_override: str | None,
) -> tuple[str, Tier]:
    """Derive the effective role and behavioral tier from the launch scope."""
    default_role = "python-developer" if scope == "issue" else (
        "cto" if scope == "full_initiative" else "engineering-coordinator"
    )
    role = role_override.strip() if role_override and role_override.strip() else default_role
    return role, _tier_for_role(role)


@router.post("/label", response_model=LabelDispatchResponse)
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
    role, tier = _role_and_tier_for_scope(req.scope, req.role)
    org_domain = _org_domain_for_role(role)

    if req.scope == "phase" and req.scope_label:
        scope_value = req.scope_label
        scope_type: ScopeType = "label"
    elif req.scope == "issue" and req.scope_issue_number is not None:
        scope_value = str(req.scope_issue_number)
        scope_type = "issue"
    else:
        scope_value = req.label
        scope_type = "label"

    logger.warning(
        "🚀 dispatch-label: scope=%r role=%r tier=%r scope_value=%r repo=%r",
        req.scope, role, tier, scope_value, req.repo,
    )

    label_slug = _label_slug(req.label)
    batch_id = _make_label_batch_id(req.label)
    run_id = f"label-{label_slug}-{uuid.uuid4().hex[:6]}"
    branch = f"agent/{label_slug}-{uuid.uuid4().hex[:4]}"

    worktree_path = str(Path(settings.worktrees_dir) / run_id)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / run_id)
    logger.warning(
        "🚀 dispatch-label: run_id=%r tier=%r org_domain=%r",
        run_id, tier, org_domain,
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

    logger.info("✅ dispatch-label: worktree %s for label %r tier=%s", worktree_path, req.label, tier)

    ac_url = settings.ac_url
    role_file = str(Path(settings.repo_dir) / ".agentception" / "roles" / f"{role}.md")
    host_role_file = str(Path(settings.host_repo_dir) / ".agentception" / "roles" / f"{role}.md")
    label_cognitive_arch = _resolve_cognitive_arch("", role)
    node_type = _tier_to_node_type(tier)

    parent_run_id_val = req.parent_run_id or ""
    org_domain_line = f"ORG_DOMAIN={org_domain}\n" if org_domain else ""
    agent_task = (
        f"# AgentCeption agent briefing — generated by dispatch-label\n"
        f"# See docs/agent-tree-protocol.md for the full spec.\n\n"
        f"RUN_ID={run_id}\n"
        f"ROLE={role}\n"
        f"TIER={tier}\n"
        f"{org_domain_line}"
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
        f"HOST_ROLE_FILE={host_role_file}\n"
        f"COGNITIVE_ARCH={label_cognitive_arch}\n"
        f"\n"
        f"# GitHub queries for this node (tier={tier}, scope_type={scope_type}):\n"
    )

    if node_type == "coordinator":
        agent_task += (
            f"# MCP: github_list_issues(label='{scope_value}', state='open')\n"
            f"# MCP: github_list_prs(state='open')\n"
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
        tier=tier,
        org_domain=org_domain,
        parent_run_id=req.parent_run_id,
    )
    logger.warning("✅ dispatch-label: persist complete — run_id=%r is now pending_launch", run_id)

    return LabelDispatchResponse(
        run_id=run_id,
        tier=tier,
        role=role,
        label=req.label,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        agent_task_path=agent_task_path,
        batch_id=batch_id,
        status="pending_launch",
    )


# ---------------------------------------------------------------------------
# GET /api/dispatch/prompt — serve the Dispatcher prompt
# ---------------------------------------------------------------------------


@router.get("/prompt")
async def get_dispatcher_prompt() -> dict[str, str]:
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
