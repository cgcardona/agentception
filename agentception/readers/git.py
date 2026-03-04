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
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from agentception.config import settings

logger = logging.getLogger(__name__)

# Matches any branch created by AgentCeption: feat/issue-N or feat/brain-dump-*
_AGENT_BRANCH_RE = re.compile(r"^feat/(issue-\d+|brain-dump-.+)$")
_ISSUE_N_RE = re.compile(r"^feat/issue-(\d+)$")


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
    - ``is_agent_branch`` — True when branch matches ``feat/issue-N`` or ``feat/brain-dump-*``
    - ``issue_number``    — int when branch is ``feat/issue-N``, else None
    - ``locked``          — True when git has locked the worktree from auto-prune
    - ``task_mtime_str``  — relative age of ``.agent-task`` file, or empty string
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
                "task_mtime_str": "",
            }
        elif line.startswith("HEAD "):
            current["head_sha"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):]
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            current["branch"] = branch
            current["is_agent_branch"] = bool(_AGENT_BRANCH_RE.match(branch))
            m = _ISSUE_N_RE.match(branch)
            if m:
                current["issue_number"] = int(m.group(1))
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("locked"):
            current["locked"] = True

    if current:
        worktrees.append(current)

    # Mark the first entry as main worktree (git always lists main first).
    if worktrees:
        worktrees[0]["is_main"] = True

    # Fetch HEAD commit message and task file mtime for each worktree.
    for wt in worktrees:
        sha = str(wt.get("head_sha", ""))
        if sha:
            wt["head_message"] = await _git(["log", "-1", "--format=%s", sha])
        # Report how long ago the agent last updated its task file.
        task_file = Path(str(wt["path"])) / ".agent-task"
        try:
            wt["task_mtime_str"] = _relative_time(task_file.stat().st_mtime)
        except OSError:
            wt["task_mtime_str"] = ""

    return worktrees


async def get_worktree_detail(slug: str) -> dict[str, object]:
    """Fetch on-demand detail for a single worktree.

    Returns a dict with:
    - ``commits``      — list of ``{sha, message}`` for commits on branch not in origin/dev
    - ``diff_stat``    — output of ``git diff --stat origin/dev...{branch}``
    - ``task_content`` — raw text of the ``.agent-task`` file, or empty string
    - ``branch``       — branch name for this worktree
    - ``found``        — False when no worktree with that slug exists
    """
    worktrees = await list_git_worktrees()
    wt = next((w for w in worktrees if str(w.get("slug", "")) == slug), None)
    if wt is None:
        return {"found": False, "commits": [], "diff_stat": "", "task_content": "", "branch": ""}

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

    # Task file — the agent's instructions.
    task_content = ""
    task_file = Path(str(wt["path"])) / ".agent-task"
    try:
        task_content = task_file.read_text(encoding="utf-8")
    except OSError:
        pass

    return {
        "found": True,
        "branch": branch,
        "commits": commits,
        "diff_stat": diff_stat,
        "task_content": task_content,
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
