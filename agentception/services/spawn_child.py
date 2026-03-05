from __future__ import annotations

"""Universal child-node spawner for the AgentCeption agent tree.

Any manager agent — CTO, engineering-coordinator, qa-coordinator, or any
future tier — calls ``spawn_child()`` to atomically:

  1. Create a git worktree for the child.
  2. Resolve COGNITIVE_ARCH for the child's role and scope.
  3. Write a fully-populated ``.agent-task`` file into the worktree.
  4. Register a ``pending_launch`` DB record (with parent_run_id lineage).
  5. Auto-acknowledge the run (transition to ``implementing``).

The caller receives ``SpawnChildResult`` and immediately has everything
needed to fire a Task tool call:  ``host_worktree_path`` as the worktree
and a short prompt directing the child to read its ``.agent-task``.

Protocol guarantee
------------------
Every node in the agent tree — regardless of tier, scope type, or which
parent spawned it — gets:

- A unique ``run_id`` and git worktree.
- A ``.agent-task`` file containing ``COGNITIVE_ARCH``, ``SCOPE_TYPE``,
  ``SCOPE_VALUE``, ``TIER``, ``ROLE``, ``ROLE_FILE``, ``PARENT_RUN_ID``,
  and ``AC_URL``.
- A DB row visible on the Build board with full lineage back to its root.

Any subtree rooted at any node behaves identically to the full tree —
pruning a node makes it the entry point without any protocol change.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from agentception.config import settings
from agentception.db.persist import acknowledge_agent_run, persist_agent_run_dispatch
from agentception.routes.api._shared import ROLE_DEFAULT_FIGURE, _resolve_cognitive_arch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ScopeType = Literal["label", "issue", "pr"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class SpawnChildResult:
    """Result returned by ``spawn_child()``.

    Attributes:
        run_id:              Unique run identifier (e.g. ``coord-abc123``).
        host_worktree_path:  Absolute path on the HOST filesystem.
        worktree_path:       Absolute path inside the container.
        tier:                Protocol tier (executive | coordinator | engineer | reviewer).
        role:                Role slug (e.g. ``engineering-coordinator``).
        cognitive_arch:      Resolved COGNITIVE_ARCH string (e.g. ``von_neumann:python``).
        agent_task_path:     Path to the written ``.agent-task`` file.
        scope_type:          ``label``, ``issue``, or ``pr``.
        scope_value:         Label string or issue/PR number.
    """

    __slots__ = (
        "run_id",
        "host_worktree_path",
        "worktree_path",
        "tier",
        "role",
        "cognitive_arch",
        "agent_task_path",
        "scope_type",
        "scope_value",
    )

    def __init__(
        self,
        *,
        run_id: str,
        host_worktree_path: str,
        worktree_path: str,
        tier: str,
        role: str,
        cognitive_arch: str,
        agent_task_path: str,
        scope_type: str,
        scope_value: str,
    ) -> None:
        self.run_id = run_id
        self.host_worktree_path = host_worktree_path
        self.worktree_path = worktree_path
        self.tier = tier
        self.role = role
        self.cognitive_arch = cognitive_arch
        self.agent_task_path = agent_task_path
        self.scope_type = scope_type
        self.scope_value = scope_value

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "host_worktree_path": self.host_worktree_path,
            "worktree_path": self.worktree_path,
            "tier": self.tier,
            "role": self.role,
            "cognitive_arch": self.cognitive_arch,
            "agent_task_path": self.agent_task_path,
            "scope_type": self.scope_type,
            "scope_value": self.scope_value,
        }


# ---------------------------------------------------------------------------
# Tier mapping (mirrors agent-tree-protocol.md)
# ---------------------------------------------------------------------------

#: Maps every known role slug to its protocol tier.
#: Defaults to ``"engineer"`` for any unlisted role.
_ROLE_TIER: dict[str, str] = {
    "cto": "executive",
    "csto": "executive",
    "ceo": "executive",
    "cpo": "executive",
    "coo": "executive",
    "cdo": "executive",
    "cfo": "executive",
    "ciso": "executive",
    "cmo": "executive",
    "engineering-coordinator": "coordinator",
    "qa-coordinator": "coordinator",
    "coordinator": "coordinator",
    "conductor": "coordinator",
    "vp-platform": "coordinator",
    "vp-infrastructure": "coordinator",
    "vp-data": "coordinator",
    "vp-ml": "coordinator",
    "vp-design": "coordinator",
    "vp-mobile": "coordinator",
    "vp-security": "coordinator",
    "vp-product": "coordinator",
    "pr-reviewer": "reviewer",
}


def tier_for_role(role: str) -> str:
    """Return the protocol tier for a role slug."""
    return _ROLE_TIER.get(role, "engineer")


# ---------------------------------------------------------------------------
# ID / branch helpers
# ---------------------------------------------------------------------------

def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-")[:48]


def _make_run_id(scope_type: ScopeType, scope_value: str) -> str:
    hex6 = uuid.uuid4().hex[:6]
    if scope_type == "label":
        return f"coord-{_slug(scope_value)}-{hex6}"
    if scope_type == "issue":
        return f"issue-{scope_value}-{hex6}"
    return f"pr-{scope_value}-{hex6}"


def _make_branch(scope_type: ScopeType, scope_value: str) -> str:
    hex4 = uuid.uuid4().hex[:4]
    if scope_type == "label":
        return f"agent/{_slug(scope_value)}-{hex4}"
    if scope_type == "issue":
        return f"feat/issue-{scope_value}-{hex4}"
    return f"review/pr-{scope_value}-{hex4}"


def _make_batch_id(scope_type: ScopeType, scope_value: str) -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hex4 = uuid.uuid4().hex[:4]
    slug = _slug(scope_value) if scope_type == "label" else scope_value
    prefix = "label" if scope_type == "label" else scope_type
    return f"{prefix}-{slug}-{stamp}-{hex4}"


# ---------------------------------------------------------------------------
# .agent-task builder (universal — all scope types)
# ---------------------------------------------------------------------------

def _build_child_task(
    *,
    run_id: str,
    role: str,
    tier: str,
    scope_type: ScopeType,
    scope_value: str,
    gh_repo: str,
    branch: str,
    worktree_path: str,
    host_worktree_path: str,
    batch_id: str,
    parent_run_id: str,
    cognitive_arch: str,
    ac_url: str,
    issue_title: str = "",
    issue_number: int | None = None,
    pr_number: int | None = None,
) -> str:
    """Build the raw text content of a ``.agent-task`` file for any tree node.

    The format is identical regardless of tier or scope type so that the
    Dispatcher, the universal manager briefing, and all role files can use
    the same parsing logic.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    role_file = str(
        Path(settings.repo_dir) / ".agentception" / "roles" / f"{role}.md"
    )

    lines: list[str] = [
        "# AgentCeption agent briefing — generated by spawn_child service",
        "# See agentception/docs/agent-tree-protocol.md for the full spec.",
        "",
        f"RUN_ID={run_id}",
        f"ROLE={role}",
        f"TIER={tier}",
        f"LOGICAL_TIER={tier}",
        f"SCOPE_TYPE={scope_type}",
        f"SCOPE_VALUE={scope_value}",
        f"GH_REPO={gh_repo}",
        f"BRANCH={branch}",
        f"WORKTREE={host_worktree_path}",
        f"BATCH_ID={batch_id}",
        f"PARENT_RUN_ID={parent_run_id}",
        f"AC_URL={ac_url}",
        f"ROLE_FILE={role_file}",
        f"COGNITIVE_ARCH={cognitive_arch}",
    ]

    # Scope-specific supplemental fields
    if scope_type == "issue" and issue_number is not None:
        lines += [
            f"ISSUE_NUMBER={issue_number}",
            f"ISSUE_TITLE={issue_title}",
            f"ISSUE_URL=https://github.com/{gh_repo}/issues/{issue_number}",
        ]
    elif scope_type == "pr" and pr_number is not None:
        lines += [
            f"PR_NUMBER={pr_number}",
            f"PR_URL=https://github.com/{gh_repo}/pull/{pr_number}",
        ]

    lines += [
        f"CREATED_AT={now}",
        "",
        f"# GitHub queries for this tier ({tier}):",
    ]

    # Inline query hints by tier
    if tier == "executive":
        lines += [
            f"# gh issue list --repo {gh_repo} --label '{scope_value}' --state open --json number,title,labels,assignees --limit 200",
            f"# gh pr list --repo {gh_repo} --base dev --state open --json number,title,headRefName,reviewDecision --limit 200",
        ]
    elif tier == "coordinator" and role == "engineering-coordinator":
        lines.append(
            f"# gh issue list --repo {gh_repo} --label '{scope_value}' --state open --json number,title,labels,assignees --limit 200"
        )
    elif tier == "coordinator" and role == "qa-coordinator":
        lines.append(
            f"# gh pr list --repo {gh_repo} --base dev --state open --json number,title,headRefName,reviewDecision --limit 200"
        )
    elif tier == "engineer" and scope_type == "issue":
        lines.append(
            f"# gh issue view {scope_value} --repo {gh_repo} --json number,title,body,labels"
        )
    elif tier == "reviewer" and scope_type == "pr":
        lines.append(
            f"# gh pr view {scope_value} --repo {gh_repo} --json number,title,body,files,reviews"
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Core service function
# ---------------------------------------------------------------------------

class SpawnChildError(Exception):
    """Raised when worktree creation or file I/O fails."""


async def spawn_child(
    *,
    parent_run_id: str,
    role: str,
    scope_type: ScopeType,
    scope_value: str,
    gh_repo: str,
    issue_body: str = "",
    issue_title: str = "",
    skills_hint: list[str] | None = None,
) -> SpawnChildResult:
    """Atomically create a child agent node in the agent tree.

    Creates the worktree, writes ``.agent-task``, registers the DB record,
    and auto-acknowledges (``pending_launch`` → ``implementing``) so the
    caller can immediately fire a Task tool call.

    Args:
        parent_run_id:  ``run_id`` of the calling agent (lineage tracking).
        role:           Child's role slug (e.g. ``"engineering-coordinator"``).
        scope_type:     ``"label"``, ``"issue"``, or ``"pr"``.
        scope_value:    Label string, or issue/PR number as a string.
        gh_repo:        ``"owner/repo"`` string.
        issue_body:     Issue body text (used for COGNITIVE_ARCH skill extraction).
        issue_title:    Issue title (written to ISSUE_TITLE field).
        skills_hint:    Explicit skill list override for COGNITIVE_ARCH.

    Returns:
        :class:`SpawnChildResult` with all fields needed to fire a Task call.

    Raises:
        :class:`SpawnChildError` if worktree creation or file I/O fails.
    """
    tier = tier_for_role(role)
    run_id = _make_run_id(scope_type, scope_value)
    branch = _make_branch(scope_type, scope_value)
    batch_id = _make_batch_id(scope_type, scope_value)

    worktree_path = str(Path(settings.worktrees_dir) / run_id)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / run_id)
    ac_url = settings.ac_url

    # Derive issue/pr numbers for supplemental task fields
    issue_number: int | None = None
    pr_number: int | None = None
    if scope_type == "issue":
        try:
            issue_number = int(scope_value)
        except ValueError:
            pass
    elif scope_type == "pr":
        try:
            pr_number = int(scope_value)
        except ValueError:
            pass

    # Resolve cognitive architecture
    cognitive_arch = _resolve_cognitive_arch(
        issue_body,
        role,
        skills_hint=skills_hint,
    )
    logger.info(
        "🌳 spawn_child: role=%r tier=%r scope=%s:%s arch=%r",
        role, tier, scope_type, scope_value, cognitive_arch,
    )

    # Create git worktree
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(settings.repo_dir),
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        err = stderr_bytes.decode().strip()
        logger.error("❌ spawn_child: git worktree add failed — %s", err)
        raise SpawnChildError(f"git worktree add failed: {err}")

    logger.info("✅ spawn_child: worktree created at %s", worktree_path)

    # Write .agent-task
    task_content = _build_child_task(
        run_id=run_id,
        role=role,
        tier=tier,
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

    agent_task_path = str(Path(worktree_path) / ".agent-task")
    try:
        Path(agent_task_path).write_text(task_content, encoding="utf-8")
    except Exception as exc:
        # Clean up worktree on write failure
        cleanup = await asyncio.create_subprocess_exec(
            "git", "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(settings.repo_dir),
        )
        await cleanup.communicate()
        logger.error("❌ spawn_child: .agent-task write failed, worktree removed — %s", exc)
        raise SpawnChildError(f".agent-task write failed: {exc}") from exc

    logger.info("✅ spawn_child: .agent-task written to %s", agent_task_path)

    # Persist DB record
    db_issue_number = issue_number if issue_number is not None else (pr_number or 0)
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=db_issue_number,
        role=role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=cognitive_arch,
        logical_tier=tier,
        parent_run_id=parent_run_id,
    )

    # Auto-acknowledge: pending_launch → implementing
    await acknowledge_agent_run(run_id)
    logger.info("✅ spawn_child: run_id=%r acknowledged (implementing)", run_id)

    return SpawnChildResult(
        run_id=run_id,
        host_worktree_path=host_worktree_path,
        worktree_path=worktree_path,
        tier=tier,
        role=role,
        cognitive_arch=cognitive_arch,
        agent_task_path=agent_task_path,
        scope_type=scope_type,
        scope_value=scope_value,
    )
