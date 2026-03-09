from __future__ import annotations

"""Universal child-node spawner for the AgentCeption agent tree.

Any coordinator agent calls ``spawn_child()`` to atomically:

  1. Create a git worktree for the child.
  2. Resolve COGNITIVE_ARCH for the child's role and scope.
  3. Register a ``pending_launch`` DB record (with parent_run_id lineage).
  4. Auto-acknowledge the run (transition to ``implementing``).

No ``.agent-task`` file is written.  The child agent receives its full
task context via the DB-backed MCP surface:

- ``ac://runs/{run_id}/context`` resource — full RunContextRow with role,
  cognitive_arch, scope, issue/PR number, and all lineage fields.
- ``task/briefing`` MCP prompt — rendered task description incorporating
  context from the RunContextRow.

The caller receives ``SpawnChildResult`` and immediately has everything
needed to fire a Task tool call: ``host_worktree_path`` as the worktree.

Protocol guarantee
------------------
Every node in the agent tree — regardless of node type, scope type, or which
parent spawned it — gets:

- A unique ``run_id`` and git worktree.
- A DB row with ``role``, ``cognitive_arch``, ``scope_type``, ``scope_value``,
  ``parent_run_id``, ``tier``, ``org_domain``, ``gh_repo``, and ``batch_id``.
- A DB row visible on the Build board with full lineage back to its root.

Node types
----------
``coordinator``
    Surveys its GitHub scope and spawns child nodes (coordinators or leaves).
    Any coordinator can be the tree root — there is no special "executive" tier.

``worker``
    Works on a single GitHub issue or PR.  Does not spawn children.

Any subtree rooted at any coordinator behaves identically to the full tree —
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
from agentception.services.cognitive_arch import _resolve_cognitive_arch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ScopeType = Literal["label", "issue", "pr"]

#: Behavioral execution tier — what the agent *does* in the pipeline.
Tier = Literal["coordinator", "worker"]

#: Internal structural position derived from Tier; used only for MCP query hints.
_NodeType = Literal["coordinator", "leaf"]


def _tier_to_node_type(tier: Tier) -> _NodeType:
    """Derive structural position from behavioral tier.

    ``coordinator`` surveys its scope and spawns children.
    ``worker`` claims one unit of work (issue or PR) and executes it.
    """
    if tier == "coordinator":
        return "coordinator"
    return "leaf"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class SpawnChildResult:
    """Result returned by ``spawn_child()``.

    Attributes:
        run_id:              Unique run identifier (e.g. ``coord-abc123``).
        host_worktree_path:  Absolute path on the HOST filesystem.
        worktree_path:       Absolute path inside the container.
        tier:                Behavioral tier: ``coordinator | worker``.
        org_domain:          Organisational slot for UI hierarchy (``c-suite``,
                             ``engineering``, or ``qa``).  ``None`` when not specified.
        role:                Role slug (e.g. ``"engineering-coordinator"``).
        cognitive_arch:      Resolved cognitive architecture string.
        scope_type:          ``"label"``, ``"issue"``, or ``"pr"``.
        scope_value:         Label string or issue/PR number.
    """

    __slots__ = (
        "run_id",
        "host_worktree_path",
        "worktree_path",
        "tier",
        "org_domain",
        "role",
        "cognitive_arch",
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
        org_domain: str | None,
        role: str,
        cognitive_arch: str,
        scope_type: str,
        scope_value: str,
    ) -> None:
        self.run_id = run_id
        self.host_worktree_path = host_worktree_path
        self.worktree_path = worktree_path
        self.tier = tier
        self.org_domain = org_domain
        self.role = role
        self.cognitive_arch = cognitive_arch
        self.scope_type = scope_type
        self.scope_value = scope_value

    def to_dict(self) -> dict[str, str | None]:
        return {
            "run_id": self.run_id,
            "host_worktree_path": self.host_worktree_path,
            "worktree_path": self.worktree_path,
            "tier": self.tier,
            "org_domain": self.org_domain,
            "role": self.role,
            "cognitive_arch": self.cognitive_arch,
            "scope_type": self.scope_type,
            "scope_value": self.scope_value,
        }


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
        return f"ac/coord-{_slug(scope_value)}-{hex4}"
    if scope_type == "issue":
        return f"ac/issue-{scope_value}"
    return f"ac/review-{scope_value}-{hex4}"


def _make_batch_id(scope_type: ScopeType, scope_value: str) -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hex4 = uuid.uuid4().hex[:4]
    slug = _slug(scope_value) if scope_type == "label" else scope_value
    prefix = "label" if scope_type == "label" else scope_type
    return f"{prefix}-{slug}-{stamp}-{hex4}"




# ---------------------------------------------------------------------------
# Core service function
# ---------------------------------------------------------------------------

class SpawnChildError(Exception):
    """Raised when worktree creation or file I/O fails."""


async def spawn_child(
    *,
    parent_run_id: str,
    role: str,
    tier: Tier,
    org_domain: str | None = None,
    scope_type: ScopeType,
    scope_value: str,
    gh_repo: str,
    issue_body: str = "",
    issue_title: str = "",
    skills_hint: list[str] | None = None,
    coord_fingerprint: str | None = None,
    cognitive_arch: str | None = None,
    is_resumed: bool = False,
) -> SpawnChildResult:
    """Atomically create a child agent node in the agent tree.

    Creates the worktree, writes ``.agent-task``, registers the DB record,
    and auto-acknowledges (``pending_launch`` → ``implementing``) so the
    caller can immediately fire a Task tool call.

    Args:
        parent_run_id:      ``run_id`` of the calling agent (lineage tracking).
        role:               Child's role slug (e.g. ``"engineering-coordinator"``).
        tier:               Behavioral execution tier for this child —
                            ``"coordinator"`` or ``"worker"``.
                            Written as ``TIER=`` in the
                            ``.agent-task`` file.  The caller always knows which
                            tier it is spawning — this is never inferred.
        org_domain:         Organisational slot for UI hierarchy visualisation —
                            ``"c-suite"``, ``"engineering"``, or ``"qa"``.  Optional;
                            written as ``ORG_DOMAIN=`` when provided.  A
                            chain-spawned PR reviewer should pass ``"qa"`` so the
                            dashboard places it in the QA column even though its
                            physical ``parent_run_id`` points to an engineering leaf.
        scope_type:         ``"label"``, ``"issue"``, or ``"pr"``.
        scope_value:        Label string, or issue/PR number as a string.
        gh_repo:            ``"owner/repo"`` string.
        issue_body:         Issue body text (used for COGNITIVE_ARCH skill extraction
                            when ``cognitive_arch`` is not provided).
        issue_title:        Issue title (written to ISSUE_TITLE field).
        skills_hint:        Explicit skill list override for COGNITIVE_ARCH
                            (used when ``cognitive_arch`` is not provided).
        coord_fingerprint:  The spawning coordinator's fingerprint string.  Written
                            as ``COORD_FINGERPRINT=`` in the child's ``.agent-task``
                            so leaf agents can include it in their GitHub fingerprint
                            comments without having to re-derive it.
        cognitive_arch:     When provided, forward this exact arch string to the child
                            without re-resolving.  Coordinators must pass their own
                            ``cognitive_arch`` here so the field propagates unchanged
                            through every tier of the agent tree.  When omitted,
                            resolution falls back to ``_resolve_cognitive_arch()``.

    Returns:
        :class:`SpawnChildResult` with all fields needed to fire a Task call.

    Raises:
        :class:`SpawnChildError` if worktree creation or file I/O fails.
    """
    run_id = _make_run_id(scope_type, scope_value)
    branch = _make_branch(scope_type, scope_value)
    batch_id = _make_batch_id(scope_type, scope_value)

    worktree_path = str(Path(settings.worktrees_dir) / run_id)
    host_worktree_path = str(Path(settings.host_worktrees_dir) / run_id)

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

    # Resolve cognitive architecture — forward the parent's arch when provided
    # so the field propagates unchanged through every tier of the agent tree.
    resolved_arch: str
    if cognitive_arch:
        resolved_arch = cognitive_arch
        logger.info(
            "🌳 spawn_child: role=%r tier=%r org_domain=%r scope=%s:%s arch=%r (forwarded from parent)",
            role, tier, org_domain, scope_type, scope_value, resolved_arch,
        )
    else:
        resolved_arch = _resolve_cognitive_arch(
            issue_body,
            role,
            skills_hint=skills_hint,
        )
        logger.info(
            "🌳 spawn_child: role=%r tier=%r org_domain=%r scope=%s:%s arch=%r (resolved)",
            role, tier, org_domain, scope_type, scope_value, resolved_arch,
        )

    # Resolve origin/dev SHA to pin the worktree start point.
    # Using a concrete SHA instead of the main repo's HEAD prevents agents
    # from inheriting local commits not yet on origin/dev and decouples the
    # worktree from whatever branch the main repo currently has checked out.
    sha_proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "origin/dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(settings.repo_dir),
    )
    sha_out, sha_err = await sha_proc.communicate()
    if sha_proc.returncode != 0:
        err = sha_err.decode().strip()
        logger.error("❌ spawn_child: git rev-parse origin/dev failed — %s", err)
        raise SpawnChildError(f"git rev-parse origin/dev failed: {err}")
    dev_sha = sha_out.decode().strip()

    # Create git worktree anchored to the resolved SHA
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", worktree_path, "-b", branch, dev_sha,
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

    # Persist DB record — all task context goes to the DB row; no .agent-task
    # file is written.  Agents read their full briefing from the DB via the
    # ac://runs/{run_id}/context MCP resource and the task/briefing prompt.
    db_issue_number = issue_number if issue_number is not None else (pr_number or 0)
    await persist_agent_run_dispatch(
        run_id=run_id,
        issue_number=db_issue_number,
        role=role,
        branch=branch,
        worktree_path=worktree_path,
        batch_id=batch_id,
        host_worktree_path=host_worktree_path,
        cognitive_arch=resolved_arch,
        tier=tier,
        org_domain=org_domain,
        parent_run_id=parent_run_id,
        gh_repo=gh_repo,
        is_resumed=is_resumed,
        coord_fingerprint=coord_fingerprint,
    )

    # Auto-acknowledge: pending_launch → implementing
    await acknowledge_agent_run(run_id)
    logger.info("✅ spawn_child: run_id=%r acknowledged (implementing)", run_id)

    return SpawnChildResult(
        run_id=run_id,
        host_worktree_path=host_worktree_path,
        worktree_path=worktree_path,
        tier=tier,
        org_domain=org_domain,
        role=role,
        cognitive_arch=resolved_arch,
        scope_type=scope_type,
        scope_value=scope_value,
    )
