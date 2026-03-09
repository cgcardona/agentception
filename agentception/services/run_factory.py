"""Shared factory for creating and launching ad-hoc agent runs.

Both the REST route (``POST /api/runs/adhoc``) and the MCP tool
(``spawn_adhoc_child``) use this module so the creation logic lives in exactly
one place.  The HTTP route raises ``HTTPException`` on failure; the MCP tool
catches ``RunCreationError`` and returns a structured error dict instead.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from pathlib import Path

from agentception.config import settings
from agentception.db.engine import get_session
from agentception.db.models import ACAgentRun
from agentception.services.cognitive_arch import ROLE_DEFAULT_FIGURE, _resolve_cognitive_arch

logger = logging.getLogger(__name__)


class RunCreationError(Exception):
    """Raised when worktree creation or DB insertion fails."""


async def create_and_launch_run(
    *,
    role: str,
    task_description: str,
    figure: str | None = None,
    base_branch: str = "origin/dev",
    parent_run_id: str | None = None,
    tier: str = "worker",
    org_domain: str = "engineering",
    launch: bool = True,
) -> dict[str, str]:
    """Create a worktree, insert a DB row, optionally fire the agent loop.

    This is the single authoritative implementation for launching an ad-hoc
    agent run.  Both the HTTP route and the ``spawn_adhoc_child`` MCP tool
    delegate here so the behaviour is always identical.

    Args:
        role: Role slug (e.g. ``"developer"``).
        task_description: Plain-language task injected as the agent's first message.
        figure: Cognitive figure override (e.g. ``"guido_van_rossum"``).
            When ``None`` the default for the role is used.
        base_branch: Git ref to branch the worktree from.  Defaults to
            ``"origin/dev"``.
        parent_run_id: ``run_id`` of the calling agent, if spawned by a
            coordinator.  ``None`` for top-level ad-hoc runs.
        tier: DB tier label — ``"worker"`` for engineers,
            ``"coordinator"`` for coordinators.
        org_domain: DB org slot for the UI hierarchy.
        launch: When ``True`` (default) the agent loop is started as a
            background task.  Pass ``False`` to create the run and worktree
            without starting the loop — useful for the debug script, which
            drives the loop itself turn-by-turn.

    Returns:
        ``{"run_id": str, "worktree_path": str, "cognitive_arch": str}``

    Raises:
        RunCreationError: When git worktree creation or DB insertion fails.
    """
    run_id = f"adhoc-{uuid.uuid4().hex[:12]}"
    worktree_path = settings.worktrees_dir / run_id
    branch_name = f"adhoc/{run_id}"

    resolved_figure = figure or ROLE_DEFAULT_FIGURE.get(role, "hopper")
    cognitive_arch = _resolve_cognitive_arch(
        issue_body="",
        role=role,
        figure_override=resolved_figure,
    )

    await _create_worktree(worktree_path, branch_name, base_branch, run_id)
    await _insert_run(
        run_id=run_id,
        role=role,
        cognitive_arch=cognitive_arch,
        worktree_path=worktree_path,
        branch=branch_name,
        task_description=task_description,
        parent_run_id=parent_run_id,
        tier=tier,
        org_domain=org_domain,
    )

    if launch:
        # Import here to avoid a circular import at module load time.
        from agentception.services.agent_loop import run_agent_loop  # noqa: PLC0415

        asyncio.create_task(run_agent_loop(run_id))
        logger.info(
            "✅ run_factory: launched run_id=%s role=%s arch=%s parent=%s",
            run_id, role, cognitive_arch, parent_run_id or "none",
        )
    else:
        logger.info(
            "✅ run_factory: created (no-launch) run_id=%s role=%s arch=%s",
            run_id, role, cognitive_arch,
        )

    return {
        "run_id": run_id,
        "worktree_path": str(worktree_path),
        "cognitive_arch": cognitive_arch,
    }


async def _create_worktree(
    worktree_path: Path,
    branch_name: str,
    base_ref: str,
    run_id: str,
) -> None:
    """Create a git worktree at *worktree_path* branching off *base_ref*.

    After creation the worktree's remote URL is rewritten to embed the
    ``GITHUB_TOKEN`` so that ``git push`` works inside the container without
    a separate credential helper.  The token lives only in the worktree's
    local git config — it is not committed.
    """
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", "-b", branch_name, str(worktree_path), base_ref,
        cwd=str(settings.repo_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.error("❌ _create_worktree failed for run_id=%s: %s", run_id, err)
        raise RunCreationError(f"git worktree add failed: {err}")

    await _configure_worktree_auth(worktree_path, run_id)
    logger.info("✅ worktree created — %s", worktree_path)


async def _configure_worktree_auth(worktree_path: Path, run_id: str) -> None:
    """Embed GITHUB_TOKEN in the worktree remote URL so git push works natively.

    Transforms ``https://github.com/owner/repo.git`` into
    ``https://x-access-token:<token>@github.com/owner/repo.git`` and writes
    it to the worktree's local git config only.  If ``GITHUB_TOKEN`` is not
    set, logs a warning and leaves the remote unchanged.
    """
    import os  # noqa: PLC0415

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        logger.warning("⚠️ GITHUB_TOKEN not set — git push will require manual auth in worktrees")
        return

    # Read the current remote URL.
    url_proc = await asyncio.create_subprocess_exec(
        "git", "remote", "get-url", "origin",
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    url_out, _ = await url_proc.communicate()
    remote_url = url_out.decode().strip()

    if not remote_url.startswith("https://"):
        # SSH remotes already handle auth via ssh-agent — nothing to do.
        return

    # Inject token into URL, avoiding double-injection if already present.
    if "@" not in remote_url.split("://", 1)[-1]:
        authed_url = remote_url.replace(
            "https://", f"https://x-access-token:{token}@", 1
        )
    else:
        authed_url = remote_url

    set_proc = await asyncio.create_subprocess_exec(
        "git", "remote", "set-url", "origin", authed_url,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, set_err = await set_proc.communicate()
    if set_proc.returncode != 0:
        logger.warning(
            "⚠️ _configure_worktree_auth — could not set remote URL for run_id=%s: %s",
            run_id, set_err.decode().strip(),
        )
        return

    logger.info("✅ worktree auth configured — run_id=%s", run_id)


async def _insert_run(
    *,
    run_id: str,
    role: str,
    cognitive_arch: str,
    worktree_path: Path,
    branch: str,
    task_description: str,
    parent_run_id: str | None,
    tier: str,
    org_domain: str,
) -> None:
    """Insert an ``ACAgentRun`` row directly into ``implementing`` state."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with get_session() as session:
        run = ACAgentRun(
            id=run_id,
            wave_id=None,
            issue_number=None,
            pr_number=None,
            branch=branch,
            worktree_path=str(worktree_path),
            role=role,
            status="implementing",
            attempt_number=0,
            spawn_mode=None,
            batch_id=None,
            cognitive_arch=cognitive_arch,
            tier=tier,
            org_domain=org_domain,
            parent_run_id=parent_run_id,
            spawned_at=now,
            last_activity_at=now,
            completed_at=None,
            task_description=task_description,
        )
        session.add(run)
        await session.commit()
    logger.info("✅ ACAgentRun inserted — run_id=%s status=implementing", run_id)
