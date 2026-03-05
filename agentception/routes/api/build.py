from __future__ import annotations

"""Build phase API routes.

Three audiences:

1. **The Build UI** — ``POST /api/build/dispatch`` (issue-scoped leaf) and
   ``POST /api/build/dispatch-label`` (label-scoped manager/root) create a
   worktree, ``.agent-task`` file, and a ``pending_launch`` DB record.

2. **The AgentCeption Coordinator** — ``GET /api/build/pending-launches``
   exposes the launch queue; ``POST /api/build/acknowledge/{run_id}``
   atomically claims a run before the coordinator spawns its Task worker.

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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentception.config import settings
from agentception.db.persist import acknowledge_agent_run, persist_agent_event, persist_agent_run_dispatch
from agentception.db.queries import get_pending_launches
from agentception.routes.api._shared import _resolve_cognitive_arch

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
# Tier → GitHub scope mapping (mirrors agent-tree-protocol.md)
# ---------------------------------------------------------------------------

# Maps each role slug to its canonical tier.
_ROLE_TIER: dict[str, str] = {
    "cto": "executive",
    "engineering-coordinator": "coordinator",
    "qa-coordinator": "coordinator",
    "pr-reviewer": "reviewer",
}
_ENGINEERING_ROLES = {
    "python-developer", "frontend-developer", "typescript-developer",
    "react-developer", "go-developer", "rust-developer", "api-developer",
    "devops-engineer", "data-engineer", "site-reliability-engineer",
    "security-engineer", "mobile-developer", "ios-developer",
    "android-developer", "full-stack-developer", "architect",
    "technical-writer", "test-engineer", "ml-engineer", "ml-researcher",
    "systems-programmer", "rails-developer", "database-architect",
    "muse-specialist",
}
for _r in _ENGINEERING_ROLES:
    _ROLE_TIER[_r] = "engineer"


def _tier_for_role(role: str) -> str:
    """Derive the protocol tier for a role slug."""
    return _ROLE_TIER.get(role, "engineer")


# ---------------------------------------------------------------------------
# dispatch-label — launch a manager or root agent scoped to a GitHub label
# ---------------------------------------------------------------------------


class LabelDispatchRequest(BaseModel):
    """Request body for ``POST /api/build/dispatch-label``."""

    label: str
    """GitHub label string, e.g. ``AC-UI/0-CRITICAL-BUGS``."""
    role: str = "cto"
    """Entry role. Defaults to ``cto`` (root of the tree)."""
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


@router.post("/dispatch-label", response_model=LabelDispatchResponse)
async def dispatch_label_agent(req: LabelDispatchRequest) -> LabelDispatchResponse:
    """Launch a manager or root agent scoped to a GitHub label.

    Unlike ``/dispatch`` (which is tied to a single issue), this endpoint
    creates an agent whose SCOPE_TYPE is ``label``.  The agent's role file
    and tier determine how it surveys GitHub and which children it spawns —
    see ``agentception/docs/agent-tree-protocol.md``.

    A worktree is still created so the agent has an isolated checkout to
    run ``gh`` and ``git`` commands from without touching the main repo.

    Raises:
        HTTPException 409: Worktree already exists.
        HTTPException 500: git worktree or .agent-task write failed.
    """
    logger.warning(
        "🚀 dispatch-label: received request label=%r role=%r repo=%r",
        req.label, req.role, req.repo,
    )
    tier = _tier_for_role(req.role)
    label_slug = _label_slug(req.label)
    batch_id = _make_label_batch_id(req.label)
    run_id = f"label-{label_slug}-{uuid.uuid4().hex[:6]}"
    branch = f"agent/{label_slug}-{uuid.uuid4().hex[:4]}"

    worktree_path = str(Path(settings.worktrees_dir) / run_id)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / run_id)
    logger.warning(
        "🚀 dispatch-label: run_id=%r tier=%r worktree_path=%r host_worktree_path=%r",
        run_id, tier, worktree_path, host_worktree_path,
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
    role_file = str(Path(settings.repo_dir) / ".agentception" / "roles" / f"{req.role}.md")
    # Label-scoped agents have no issue body to parse — derive arch from role only.
    label_cognitive_arch = _resolve_cognitive_arch("", req.role)

    parent_run_id_val = req.parent_run_id or ""
    agent_task = (
        f"# AgentCeption agent briefing — generated by dispatch-label\n"
        f"# See agentception/docs/agent-tree-protocol.md for the full spec.\n\n"
        f"RUN_ID={run_id}\n"
        f"ROLE={req.role}\n"
        f"TIER={tier}\n"
        f"LOGICAL_TIER={tier}\n"
        f"SCOPE_TYPE=label\n"
        f"SCOPE_VALUE={req.label}\n"
        f"GH_REPO={req.repo}\n"
        f"BRANCH={branch}\n"
        f"WORKTREE={host_worktree_path}\n"
        f"BATCH_ID={batch_id}\n"
        f"PARENT_RUN_ID={parent_run_id_val}\n"
        f"AC_URL={ac_url}\n"
        f"ROLE_FILE={role_file}\n"
        f"COGNITIVE_ARCH={label_cognitive_arch}\n"
        f"\n"
        f"# GitHub queries for this tier ({tier}):\n"
    )

    if tier == "executive":
        agent_task += (
            f"# gh issue list --repo {req.repo} --label '{req.label}' --state open --json number,title,labels --limit 200\n"
            f"# gh pr list --repo {req.repo} --base dev --state open --json number,title,headRefName --limit 200\n"
        )
    elif tier == "coordinator" and req.role == "engineering-coordinator":
        agent_task += (
            f"# gh issue list --repo {req.repo} --label '{req.label}' --state open --json number,title,labels --limit 200\n"
        )
    elif tier == "coordinator" and req.role == "qa-coordinator":
        agent_task += (
            f"# gh pr list --repo {req.repo} --base dev --state open --json number,title,headRefName,reviewDecision --limit 200\n"
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
        issue_number=0,          # no single issue — label-scoped
        role=req.role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=label_cognitive_arch,
        logical_tier=tier,
        parent_run_id=req.parent_run_id,
    )
    logger.warning("✅ dispatch-label: persist_agent_run_dispatch complete — run_id=%r is now pending_launch", run_id)

    return LabelDispatchResponse(
        run_id=run_id,
        tier=tier,
        role=req.role,
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


@router.post("/report/done")
async def report_done(req: DoneReport) -> dict[str, object]:
    """Agent reports completion and links the PR."""
    await persist_agent_event(
        issue_number=req.issue_number,
        event_type="done",
        payload={"pr_url": req.pr_url, "summary": req.summary},
        agent_run_id=req.agent_run_id,
    )
    logger.info(
        "✅ report_done: issue=%d pr_url=%r", req.issue_number, req.pr_url
    )
    return {"ok": True}
