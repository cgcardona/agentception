from __future__ import annotations

"""Git repository data reader for AgentCeption.

Reads live git state (branches, worktrees, stash) from the mounted repo
at ``settings.repo_dir``.  All reads are subprocess calls; results are NOT
cached because git state changes frequently and the data is only fetched
on-demand (never every tick).

Public API:
    list_git_worktrees()       → all linked worktrees with branch + HEAD info
    list_git_branches()        → local branches with ahead/behind status
    list_git_stash()           → stash entries
    get_worktree_detail(slug)  → on-demand detail: commits, diff-stat, task file
"""

import asyncio
import logging
import re
import time
from pathlib import Path

from agentception.config import settings

logger = logging.getLogger(__name__)

# Matches any branch created by AgentCeption:
#   agent/*  — dispatcher-created top-level worktree branches
#   ac/*     — pipeline branches (engineer, coordinator, reviewer)
_AGENT_BRANCH_RE = re.compile(r"^(agent/.+|ac/.+)$")


async def _git(args: list[str]) -> str:
    """Run ``git -C <repo_dir> <args>`` and return stdout as a string.

    Returns an empty string on non-zero exit rather than raising, so callers
    can treat missing data as empty rather than an error.
    """
    repo = str(settings.repo_dir)
    cmd = ["git", "-C", repo] + args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.debug("⚠️  git command failed: %s — %s", " ".join(cmd), stderr.decode().strip())
        return ""
    return stdout.decode().strip()


def _relative_time(mtime: float) -> str:
    """Convert a unix timestamp to a human-readable relative age string."""
    delta = int(time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


async def list_git_worktrees() -> list[dict[str, object]]:
    """Return all git worktrees (linked + main).

    Each dict has:
    - ``path``            — container-side filesystem path
    - ``slug``            — basename of the worktree directory (e.g. ``issue-732``)
    - ``branch``          — git branch name (normalised, no refs/heads/ prefix)
    - ``head_sha``        — full SHA of HEAD commit
    - ``head_message``    — subject line of HEAD commit
    - ``is_main``         — True for the primary (repo-root) worktree
    - ``is_agent_branch`` — True when branch matches agent/* or ac/* patterns
    - ``issue_number``    — int when branch is ``feat/issue-N``, else None
    - ``locked``          — True when git has locked the worktree from auto-prune
    """
    raw = await _git(["worktree", "list", "--porcelain"])
    worktrees: list[dict[str, object]] = []

    current: dict[str, object] = {}
    for line in raw.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            path = line[len("worktree "):]
            current = {
                "path": path,
                "slug": Path(path).name,
                "is_main": False,
                "is_agent_branch": False,
                "issue_number": None,
                "locked": False,
            }
        elif line.startswith("HEAD "):
            current["head_sha"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):]
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            current["branch"] = branch
            current["is_agent_branch"] = bool(_AGENT_BRANCH_RE.match(branch))
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("locked"):
            current["locked"] = True

    if current:
        worktrees.append(current)

    # Mark the first entry as main worktree (git always lists main first).
    if worktrees:
        worktrees[0]["is_main"] = True

    # Fetch HEAD commit message for each worktree.
    for wt in worktrees:
        sha = str(wt.get("head_sha", ""))
        if sha:
            wt["head_message"] = await _git(["log", "-1", "--format=%s", sha])

    return worktrees


async def get_worktree_detail(slug: str) -> dict[str, object]:
    """Fetch on-demand detail for a single worktree.

    Returns a dict with:
    - ``commits``   — list of ``{sha, message}`` for commits on branch not in origin/dev
    - ``diff_stat`` — output of ``git diff --stat origin/dev...{branch}``
    - ``branch``    — branch name for this worktree
    - ``found``     — False when no worktree with that slug exists
    """
    worktrees = await list_git_worktrees()
    wt = next((w for w in worktrees if str(w.get("slug", "")) == slug), None)
    if wt is None:
        return {"found": False, "commits": [], "diff_stat": "", "branch": ""}

    branch = str(wt.get("branch", ""))

    # Commits on this branch not yet in origin/dev.
    commits_raw = await _git(["log", "--oneline", f"origin/dev..{branch}"])
    commits: list[dict[str, str]] = []
    for line in commits_raw.splitlines():
        parts = line.split(" ", 1)
        commits.append({
            "sha": parts[0],
            "message": parts[1] if len(parts) > 1 else "",
        })

    # Diff stat vs the merge-base with origin/dev (triple-dot = symmetric difference).
    diff_stat = await _git(["diff", "--stat", f"origin/dev...{branch}"])

    return {
        "found": True,
        "branch": branch,
        "commits": commits,
        "diff_stat": diff_stat,
    }


async def list_git_branches() -> list[dict[str, object]]:
    """Return local branches with ahead/behind counts relative to origin.

    Each dict has: ``name``, ``head_sha``, ``head_message``, ``ahead``,
    ``behind``, ``is_agent_branch`` (bool), ``is_current`` (bool).
    """
    # --format=%(refname:short) %(objectname:short) %(upstream:trackshort) %(HEAD)
    raw = await _git([
        "branch", "-v", "--format",
        "%(HEAD)|%(refname:short)|%(objectname:short)|%(subject)|%(upstream:trackshort)",
    ])

    branches: list[dict[str, object]] = []
    for line in raw.splitlines():
        parts = line.split("|", 4)
        if len(parts) < 5:
            continue
        is_current_marker, name, sha, subject, track = parts
        is_current = is_current_marker.strip() == "*"

        # Parse ahead/behind from track shorthand like "[ahead 2]", "[behind 1]", "[gone]"
        ahead = 0
        behind = 0
        if "[ahead" in track:
            m = re.search(r"ahead\s+(\d+)", track)
            if m:
                ahead = int(m.group(1))
        if "behind" in track:
            m = re.search(r"behind\s+(\d+)", track)
            if m:
                behind = int(m.group(1))

        branches.append({
            "name": name.strip(),
            "head_sha": sha.strip(),
            "head_message": subject.strip(),
            "ahead": ahead,
            "behind": behind,
            "is_agent_branch": bool(_AGENT_BRANCH_RE.match(name.strip())),
            "is_current": is_current,
        })

    return branches


async def worktree_last_commit_time(worktree_path: Path) -> float:
    """Return the UNIX timestamp of the most recent commit in the worktree.

    Used for stuck-agent detection: if this value has not advanced in a
    configurable number of minutes, the agent is likely hung and should be
    flagged in the dashboard. Returns 0.0 when the worktree has no commits
    or git is unavailable.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "-1",
            "--format=%ct",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout.strip():
            return 0.0
        return float(stdout.strip())
    except (OSError, ValueError) as exc:
        logger.debug("⚠️  worktree_last_commit_time(%s) error: %s", worktree_path, exc)
        return 0.0


async def list_git_stash() -> list[dict[str, object]]:
    """Return stash entries.

    Each dict has: ``ref`` (stash@{N}), ``branch``, ``message``.
    """
    raw = await _git(["stash", "list", "--format=%gd|%gs"])
    entries: list[dict[str, object]] = []
    for line in raw.splitlines():
        parts = line.split("|", 1)
        ref = parts[0].strip() if parts else ""
        description = parts[1].strip() if len(parts) > 1 else ""

        # "WIP on <branch>: <hash> <msg>" or "On <branch>: <msg>"
        branch = ""
        m = re.match(r"(?:WIP on|On)\s+([^:]+):", description)
        if m:
            branch = m.group(1).strip()

        entries.append({"ref": ref, "branch": branch, "message": description})

    return entries


async def ensure_branch(branch: str, base: str = "origin/dev") -> bool:
    """Create a git branch only if it does not already exist.

    Parameters
    ----------
    branch:
        Branch name to create (e.g. ``"feat/issue-123"``).
    base:
        Base ref to branch from (default: ``"origin/dev"``).

    Returns
    -------
    bool
        ``True`` if the branch was created, ``False`` if it already existed.

    Raises
    ------
    RuntimeError
        If ``git branch`` fails for any reason other than the branch already
        existing.
    """
    # Check if branch already exists
    existing = await _git(["branch", "--list", branch])
    if existing.strip():
        logger.debug("Branch %r already exists — skipping creation", branch)
        return False

    # Create the branch
    repo = str(settings.repo_dir)
    cmd = ["git", "-C", repo, "branch", branch, base]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip()
        raise RuntimeError(f"git branch {branch} {base} failed: {err}")

    logger.info("✅ Created branch %r from %s", branch, base)
    return True


async def ensure_worktree(worktree_path: Path, branch: str, base: str = "origin/dev") -> bool:
    """Create git worktree only if it does not already exist.

    Handles three cases:
    1. Both worktree dir and branch exist — return ``False`` (no-op, fully idempotent).
    2. Branch exists but worktree dir does not (e.g. after bad teardown) — use
       ``git worktree add <path> <branch>`` (no ``-b`` flag). Return ``True``.
    3. Neither exists — use ``git worktree add -b <branch> <path> <base>``. Return ``True``.

    Parameters
    ----------
    worktree_path:
        Absolute path where the worktree should be created.
    branch:
        Branch name for the worktree (e.g. ``"feat/issue-123"``).
    base:
        Base ref to branch from when creating a new branch (default: ``"origin/dev"``).

    Returns
    -------
    bool
        ``True`` if the worktree was created, ``False`` if it already existed.

    Raises
    ------
    RuntimeError
        If ``git worktree add`` fails for any reason other than the worktree
        already existing.

    Notes
    -----
    This function does NOT configure worktree auth. Callers must still call
    ``_configure_worktree_auth()`` separately after this returns ``True``.
    """
    # Case 1: Worktree directory already exists — assume fully configured
    if worktree_path.exists():
        logger.debug("Worktree %s already exists — skipping creation", worktree_path)
        return False

    # Check if branch exists
    existing_branch = await _git(["branch", "--list", branch])
    branch_exists = bool(existing_branch.strip())

    repo = str(settings.repo_dir)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    if branch_exists:
        # Case 2: Branch exists but worktree dir does not — add without -b
        cmd = ["git", "-C", repo, "worktree", "add", str(worktree_path), branch]
    else:
        # Case 3: Neither exists — create branch and worktree together
        cmd = ["git", "-C", repo, "worktree", "add", "-b", branch, str(worktree_path), base]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip()
        # If the error is "already exists", treat as idempotent success
        if "already exists" in err.lower():
            logger.debug("Worktree %s already exists (detected via error) — skipping", worktree_path)
            return False
        raise RuntimeError(f"git worktree add failed: {err}")

    logger.info("✅ Created worktree %s on branch %s", worktree_path, branch)
    return True

