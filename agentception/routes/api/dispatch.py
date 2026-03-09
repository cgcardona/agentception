from __future__ import annotations

"""Dispatch API routes — launch agents from the Ship UI.

Three endpoints drive the Ship page launch modal:

1. ``GET /api/dispatch/context`` — return phases and open issues for a
   label so the modal can populate its pickers.
2. ``POST /api/dispatch/issue`` — create a worktree + ``pending_launch``
   DB record for a single issue-scoped leaf agent.
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


class OrgNodeSpec(BaseModel):
    """One node in a user-designed agent org tree.

    Persisted to the DB and included in the agent's run context so the
    launched agent knows the exact hierarchy it was designed to spawn rather
    than inferring structure from the ticket list.

    Self-referential via ``children`` — ``model_rebuild()`` is required after
    the class definition.
    """

    id: str
    role: str
    figure: str = ""
    scope: Literal["full_initiative", "phase"] = "full_initiative"
    scope_label: str = ""
    children: list["OrgNodeSpec"] = []


OrgNodeSpec.model_rebuild()

from agentception.config import settings
from agentception.db.persist import persist_agent_run_dispatch
from agentception.db.queries import get_label_context
from agentception.services.cognitive_arch import _resolve_cognitive_arch
from agentception.services.spawn_child import (
    SpawnChildError,
    ScopeType,
    Tier,
    spawn_child,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


async def _resolve_dev_sha() -> str:
    """Return the current SHA of origin/dev.

    Pinning the worktree start point to a concrete SHA rather than the
    symbolic HEAD of the main repo prevents agents from inheriting local
    commits that are not yet on origin/dev and keeps each worktree
    reproducibly anchored to the same commit regardless of the main
    repo's checked-out branch.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "origin/dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(settings.repo_dir),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git rev-parse origin/dev failed: {stderr.decode().strip()}"
        )
    return stdout.decode().strip()

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
    """Role slug from ``.agentception/roles/`` (e.g. ``developer``)."""
    repo: str
    """``owner/repo`` string (e.g. ``cgcardona/agentception``)."""


class DispatchResponse(BaseModel):
    """Successful dispatch response."""

    run_id: str
    worktree: str
    host_worktree: str
    branch: str
    batch_id: str
    status: str = "pending_launch"


def _make_batch_id(issue_number: int) -> str:
    """Generate a deterministic-but-unique batch id for this dispatch."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:4]
    return f"issue-{issue_number}-{stamp}-{short}"


@router.post("/issue", response_model=DispatchResponse)
async def dispatch_agent(req: DispatchRequest) -> DispatchResponse:
    """Create a worktree and a ``pending_launch`` DB record.

    The worktree is the isolated git checkout the agent will work in.
    All task context is persisted to the DB row.  The ``pending_launch`` DB record is what the AgentCeption
    Dispatcher reads via ``build_get_pending_launches`` to know what to spawn.

    Agents are NOT launched here.  The Dispatcher polls the pending queue
    and spawns the right role — which may be a leaf worker, a VP, or a CTO
    depending on what was selected.

    Raises:
        HTTPException 409: Worktree already exists.
        HTTPException 500: git worktree add failed.
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

    try:
        dev_sha = await _resolve_dev_sha()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch, dev_sha,
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

    cognitive_arch = _resolve_cognitive_arch(req.issue_body, req.role)

    # Persist all task context to DB; agents read via ac://runs/{run_id}/context.
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=req.issue_number,
        role=req.role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=cognitive_arch,
        gh_repo=settings.gh_repo,
    )

    return DispatchResponse(
        run_id=run_id,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        branch=branch,
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

    All coordinator roles (C-suite and sub-coordinators alike) survey their
    scope and spawn children → ``coordinator``.  Every other role is a
    ``worker`` — it claims one unit of work and executes it, whether that
    work is implementing an issue or reviewing a PR.
    """
    if role in _COORDINATOR_ROLES:
        return "coordinator"
    return "worker"


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

    - ``"full_initiative"`` — a root coordinator (e.g. CTO) surveys every
      open ticket under *label* and assembles its own child team.  ``tier``
      is ``"coordinator"``.
    - ``"phase"`` — a coordinator handles just one phase sub-label; supply
      *scope_label* with the sub-label string.  ``tier`` is
      ``"coordinator"``.
    - ``"issue"`` — a single worker agent implements one issue; supply
      *scope_issue_number*.  ``tier`` is ``"worker"``.

    *role* is optional.  When omitted the server derives a sensible default
    (``cto`` for ``full_initiative``, ``engineering-coordinator`` for
    ``phase``, and ``developer`` for ``issue``).
    """

    label: str
    """Initiative label string, e.g. ``ac-workflow``."""
    scope: Literal["full_initiative", "phase", "issue"] = "full_initiative"
    """Determines the tier and scope for this dispatch."""
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
    cognitive_arch_override: str | None = None
    """Figure slug chosen in the Org Designer (e.g. ``"steve_jobs"``).

    When set, this figure is injected into the agent's COGNITIVE_ARCH string,
    bypassing the role-default mapping while still deriving skills from context.
    Corresponds to ``figure_override`` in ``_resolve_cognitive_arch``.
    """
    org_tree: OrgNodeSpec | None = None
    """Full org tree designed in the Org Designer.

    Persisted to the DB row as ``org_tree_json`` (compact JSON string) so
    the launched agent knows the exact hierarchy it is expected to spawn.
    When absent the agent infers its own team structure from the ticket list.
    """
    cascade_enabled: bool = True
    """When False the launched agent must not spawn any child agents.

    Used for incremental smoke-testing: prove one tier works before wiring it
    to the next.  The agent reads this flag from ``[spawn].cascade_enabled``
    via its context, and, if False, outputs its self-introduction,
    calls ``log_run_step`` + ``build_complete_run`` via MCP, and exits without
    querying GitHub or dispatching children.
    """


class LabelDispatchResponse(BaseModel):
    """Successful label-dispatch response."""

    run_id: str
    tier: str
    role: str
    label: str
    worktree: str
    host_worktree: str
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
    default_role = "developer" if scope == "issue" else (
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
    All task context is persisted to the DB row.

    Raises:
        HTTPException 409: Worktree already exists.
        HTTPException 500: git worktree add failed.
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

    try:
        dev_sha = await _resolve_dev_sha()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch, dev_sha,
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

    label_cognitive_arch = _resolve_cognitive_arch(
        "", role, figure_override=req.cognitive_arch_override
    )

    # Persist all task context to DB.
    logger.warning(
        "🚀 dispatch-label: calling persist_agent_run_dispatch run_id=%r host_worktree_path=%r",
        run_id, host_worktree_path,
    )
    issue_number = req.scope_issue_number if (req.scope == "issue" and req.scope_issue_number is not None) else 0
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=issue_number,
        role=role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=label_cognitive_arch,
        tier=tier,
        org_domain=org_domain,
        parent_run_id=req.parent_run_id,
        gh_repo=req.repo,
    )
    logger.warning("✅ dispatch-label: persist complete — run_id=%r is now pending_launch", run_id)

    return LabelDispatchResponse(
        run_id=run_id,
        tier=tier,
        role=role,
        label=req.label,
        worktree=worktree_path,
        host_worktree=host_worktree_path,
        batch_id=batch_id,
        status="pending_launch",
    )

