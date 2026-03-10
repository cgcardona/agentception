"""Factory for creating and launching ad-hoc agent runs from coordinator agents.

The ``build_spawn_adhoc_child`` MCP tool delegates here so coordinator agents
can spawn child runs that are not tied to a specific GitHub issue.  The MCP
tool catches ``RunCreationError`` and returns a structured error dict.

This module also owns the shared worktree utilities (``_configure_worktree_auth``,
``_index_worktree``, ``_WORKTREE_COLLECTION_PREFIX``) that are imported by
``spawn_child.py`` so every worktree — whether spawned via coordinator MCP tool
or via the official dispatch pipeline — gets worktree isolation and Qdrant indexing.

Git push authentication is handled by the container-wide askpass helper baked
into the image (``/usr/local/bin/github-askpass``).  No token is ever written
to any git config file.
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

# Qdrant collection name used for per-run worktree indexes.
# Follows the pattern "worktree-<run_id>" so stale collections are easy to
# identify and the main "code" collection is never polluted.
_WORKTREE_COLLECTION_PREFIX = "worktree-"


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

    from agentception.readers.git import ensure_worktree  # noqa: PLC0415

    await ensure_worktree(worktree_path, branch_name, base_branch)
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

    # Index the worktree in the background so agents can search it with
    # search_codebase.  The worktree starts from origin/dev so its content is
    # identical to the main repo at spawn time — the worktree-specific index
    # becomes more valuable as the agent writes new or modified files.
    # Non-blocking: indexing failure never prevents the run from launching.
    asyncio.create_task(_index_worktree(worktree_path, run_id))

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

    After creation ``_configure_worktree_auth`` enables ``extensions.worktreeConfig``
    for config isolation.  Authentication for ``git push`` is handled automatically
    by the container-wide askpass helper (``/usr/local/bin/github-askpass``).
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


async def _index_worktree(worktree_path: Path, run_id: str) -> None:
    """Index the worktree into a per-run Qdrant collection — non-blocking.

    The collection is named ``worktree-<run_id>`` so each run gets its own
    semantic search scope.  The agent can pass this collection name to
    ``search_codebase`` to search only the files it is working with.

    Errors are logged and swallowed — indexing failure never prevents the run
    from starting.  The main ``code`` collection (the full repo index) is
    always available as a fallback.
    """
    from agentception.services.code_indexer import index_codebase  # noqa: PLC0415

    collection = f"{_WORKTREE_COLLECTION_PREFIX}{run_id}"
    try:
        stats = await index_codebase(repo_path=worktree_path, collection=collection)
        logger.info(
            "✅ run_factory: worktree indexed — run_id=%s collection=%s files=%s chunks=%s",
            run_id,
            collection,
            stats.get("files_indexed", "?"),
            stats.get("chunks_indexed", "?"),
        )
    except Exception as exc:
        logger.warning(
            "⚠️ run_factory: worktree indexing failed — run_id=%s: %s",
            run_id,
            exc,
        )


async def _configure_worktree_auth(worktree_path: Path, run_id: str) -> None:
    """Enable per-worktree git config isolation for the newly created worktree.

    Git push authentication is handled entirely by the container-wide askpass
    helper (``/usr/local/bin/github-askpass``, configured via
    ``git config --system core.askPass`` in the Dockerfile).  The helper
    returns ``x-access-token`` / ``$GITHUB_TOKEN`` as Basic credentials, which
    is the only auth scheme that GitHub's git-receive-pack endpoint accepts.
    Bearer tokens (``Authorization: Bearer …``) are rejected by the git
    protocol even though they work for the REST API.

    This function's only remaining job is to enable ``extensions.worktreeConfig``
    so that future worktree-specific git config keys (e.g. user.email overrides
    or per-agent fetch refspecs) can be written to the isolated
    ``.git/worktrees/<name>/config.worktree`` file rather than the shared
    ``.git/config``.  This prevents any per-worktree setting from leaking into
    the main repo config.

    Tokens must never appear in ``remote.origin.url`` — GitHub's secret
    scanning auto-revokes any PAT it detects in a pushed config blob.
    """
    # Enable per-worktree config in the shared .git/config (idempotent).
    # Required for --worktree flag to write to the worktree-specific file
    # rather than the shared config.
    ext_proc = await asyncio.create_subprocess_exec(
        "git", "config", "--local", "extensions.worktreeConfig", "true",
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, ext_err = await ext_proc.communicate()
    if ext_proc.returncode != 0:
        logger.warning(
            "⚠️ _configure_worktree_auth — could not enable worktreeConfig for run_id=%s: %s",
            run_id, ext_err.decode().strip(),
        )
        return

    logger.info("✅ worktree auth configured (askpass + worktreeConfig) — run_id=%s", run_id)


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
