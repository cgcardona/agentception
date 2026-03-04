from __future__ import annotations

"""Control-plane routes for the AgentCeption dashboard.

These endpoints perform destructive agent operations (kill, future: pause/spawn).
Each operation is idempotent: calling it on a slug that no longer exists returns
404 rather than erroring, so UI retries are safe.

Why a separate router?
- Destructive operations need clear separation from the read-only API and UI
  routers so they can be reviewed, rate-limited, or gated independently.
- Prefix ``/api/control`` signals to callers that these are write operations,
  not reads.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from agentception.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/control", tags=["control"])


def _parse_issue_number(worktree: Path) -> int | None:
    """Read .agent-task in ``worktree`` and return the ISSUE_NUMBER value, or None.

    Parses only KEY=value lines — never evaluates them. Returns ``None`` when
    the file is absent, unreadable, or contains no ISSUE_NUMBER key.
    """
    task_file = worktree / ".agent-task"
    if not task_file.exists():
        return None
    try:
        for line in task_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("ISSUE_NUMBER="):
                raw = line.split("=", 1)[1].strip()
                return int(raw) if raw.isdigit() else None
    except OSError:
        logger.warning("⚠️ Could not read .agent-task at %s", task_file)
    return None


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Run ``cmd`` as a subprocess and return (returncode, stdout, stderr).

    All I/O is captured; nothing is printed to the container console.
    Raises ``RuntimeError`` only when the process cannot be started at all.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout_bytes.decode(errors="replace"), stderr_bytes.decode(errors="replace")


@router.post("/kill/{slug}")
async def kill_agent(slug: str) -> JSONResponse:
    """Force-remove an agent worktree and clear the ``agent:wip`` GitHub label.

    Slug is the bare directory name under ``settings.worktrees_dir``, e.g.
    ``issue-553`` or ``pr-607``.

    Steps:
    1. Verify the worktree directory exists (404 if not).
    2. ``git worktree remove --force <path>`` — detaches and removes the directory.
    3. Parse ``.agent-task`` for ``ISSUE_NUMBER`` and clear ``agent:wip`` on that
       issue via ``gh issue edit``.
    4. ``git worktree prune`` — cleans up stale refs in the main repo.

    Returns ``{"killed": slug}`` on success. Any individual step failure is
    logged as a warning but does not abort the overall operation — the goal is
    best-effort cleanup rather than strict atomicity.
    """
    worktree = settings.worktrees_dir / slug
    if not worktree.exists():
        raise HTTPException(status_code=404, detail=f"Worktree '{slug}' not found")

    issue_number = _parse_issue_number(worktree)

    # Step 1: force-remove the worktree.
    # git worktree remove only works when the metadata in .git/worktrees/ is
    # intact. If it fails (e.g. metadata was already cleaned from the host
    # side), fall back to a direct rm -rf so the directory is always gone.
    repo_dir = str(settings.repo_dir)
    rc, stdout, stderr = await _run(
        ["git", "-C", repo_dir, "worktree", "remove", "--force", str(worktree)]
    )
    if rc != 0:
        logger.warning("⚠️ git worktree remove exited %d: %s — trying rm -rf fallback", rc, stderr.strip())
        if worktree.exists():
            import shutil as _shutil
            try:
                await asyncio.get_event_loop().run_in_executor(None, _shutil.rmtree, str(worktree))
                logger.info("✅ rm -rf fallback removed %s", worktree)
            except OSError as rm_err:
                logger.warning("⚠️ rm -rf fallback failed for %s: %s", worktree, rm_err)

    # Step 2: clear agent:wip on the related issue (best-effort).
    if issue_number is not None:
        gh_rc, _, gh_err = await _run(
            [
                "gh",
                "issue",
                "edit",
                str(issue_number),
                "--repo",
                settings.gh_repo,
                "--remove-label",
                "agent:wip",
            ]
        )
        if gh_rc != 0:
            logger.warning("⚠️ gh issue edit exited %d: %s", gh_rc, gh_err.strip())
        else:
            logger.info("✅ Cleared agent:wip from issue #%d", issue_number)
    else:
        logger.warning("⚠️ No ISSUE_NUMBER in .agent-task for worktree '%s'", slug)

    # Step 3: prune stale worktree refs.
    prune_rc, _, prune_err = await _run(["git", "-C", repo_dir, "worktree", "prune"])
    if prune_rc != 0:
        logger.warning("⚠️ git worktree prune exited %d: %s", prune_rc, prune_err.strip())

    logger.info("✅ Killed agent worktree '%s'", slug)
    hx_trigger = json.dumps({"toast": {"message": f"Agent {slug} killed", "type": "success"}})
    return JSONResponse(content={"killed": slug}, headers={"HX-Trigger": hx_trigger})
