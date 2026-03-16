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
import shutil
import time
from pathlib import Path

from agentception.config import settings
from agentception.types import JsonValue

logger = logging.getLogger(__name__)

# Semaphore that limits concurrent ``git worktree add`` calls.
# git serialises writes to .git/config via a lockfile; racing more than
# one add at a time causes "could not lock config file" failures.
# 5 concurrent slots give ~2-3 new worktrees/second — enough for
# hundreds of dispatches per minute.
_WORKTREE_ADD_SEM: asyncio.Semaphore = asyncio.Semaphore(5)

# Matches any branch created by AgentCeption — all branches use the agent/ prefix.
_AGENT_BRANCH_RE = re.compile(r"^agent/.+$")

# Slug for plan branch names: alphanumeric and single hyphens only, max 32 chars.
_PLAN_SLUG_RE = re.compile(r"[^a-z0-9]+")


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


async def list_git_worktrees() -> list[dict[str, JsonValue]]:
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
    worktrees: list[dict[str, JsonValue]] = []

    current: dict[str, JsonValue] = {}
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


async def get_worktree_detail(slug: str) -> dict[str, JsonValue]:
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
    commits: list[JsonValue] = []
    for line in commits_raw.splitlines():
        parts = line.split(" ", 1)
        commits.append({
            "sha": parts[0],
            "message": parts[1] if len(parts) > 1 else "",
        })

    diff_stat = await _git(["diff", "--stat", f"origin/dev...{branch}"])

    return {
        "found": True,
        "branch": branch,
        "commits": commits,
        "diff_stat": diff_stat,
    }


async def list_git_branches() -> list[dict[str, JsonValue]]:
    """Return local branches with ahead/behind counts relative to origin.

    Each dict has: ``name``, ``head_sha``, ``head_message``, ``ahead``,
    ``behind``, ``is_agent_branch`` (bool), ``is_current`` (bool).
    """
    # --format=%(refname:short) %(objectname:short) %(upstream:trackshort) %(HEAD)
    raw = await _git([
        "branch", "-v", "--format",
        "%(HEAD)|%(refname:short)|%(objectname:short)|%(subject)|%(upstream:trackshort)",
    ])

    branches: list[dict[str, JsonValue]] = []
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


async def list_git_stash() -> list[dict[str, JsonValue]]:
    """Return stash entries.

    Each dict has: ``ref`` (stash@{N}), ``branch``, ``message``.
    """
    raw = await _git(["stash", "list", "--format=%gd|%gs"])
    entries: list[dict[str, JsonValue]] = []
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


async def ensure_worktree(
    worktree_path: Path,
    branch: str,
    base: str = "origin/dev",
    reset: bool = False,
    main_repo_dir: Path | None = None,
) -> bool:
    """Create git worktree, optionally resetting any stale state first.

    Handles four cases:

    1. Both worktree dir and branch exist, ``reset=False`` — no-op, fully
       idempotent.  Returns ``False``.
    2. Both worktree dir and branch exist, ``reset=True`` — tear down the
       existing worktree and branch, then recreate fresh from *base*.  Returns
       ``True``.  Use this for re-dispatches so the executor always starts from
       a clean ``origin/dev`` and never duplicates prior commits.
    3. Branch exists but worktree dir does not, ``reset=False`` — reattach
       without ``-b``.  Returns ``True``.
    4. Branch exists but worktree dir does not, ``reset=True`` — delete the
       stale branch first, then create fresh from *base*.  Returns ``True``.
    5. Neither exists — ``git worktree add -b <branch> <path> <base>``.
       Returns ``True``.

    Parameters
    ----------
    worktree_path:
        Absolute path where the worktree should be created.
    branch:
        Branch name for the worktree (e.g. ``"feat/issue-123"``).
    base:
        Base ref to branch from when creating a new branch (default:
        ``"origin/dev"``).
    reset:
        When ``True``, any existing worktree directory, local branch, and
        remote branch are torn down before (re)creating from *base*.  Use for
        re-dispatches so the executor always starts from a clean ``origin/dev``
        and never picks up commits from a prior run.  When ``False`` (default),
        the function is fully idempotent.

    Returns
    -------
    bool
        ``True`` if the worktree was created (or recreated), ``False`` if it
        already existed and ``reset=False``.

    Raises
    ------
    RuntimeError
        If ``git worktree add`` fails for any reason other than the worktree
        already existing.

    Notes
    -----
    This function does NOT configure worktree auth. Callers must still call
    ``_configure_worktree_auth()`` separately after this returns ``True``.

    main_repo_dir
        When set, git commands run in this directory instead of
        ``settings.repo_dir``.  Use for multi-repo dispatch so the worktree
        is created in the repo that owns the branch (e.g. GeodesicDomeDesigner).
    """
    repo = str(main_repo_dir) if main_repo_dir is not None else str(settings.repo_dir)

    # Fast path: directory exists and no reset requested — fully idempotent.
    # Check outside the semaphore to avoid unnecessary contention.
    if worktree_path.exists() and not reset:
        logger.debug("Worktree %s already exists — skipping creation", worktree_path)
        return False

    async with _WORKTREE_ADD_SEM:
        dir_exists = worktree_path.exists()

        # Re-check inside the lock — another coroutine may have created it.
        if dir_exists and not reset:
            logger.debug("Worktree %s already exists — skipping creation", worktree_path)
            return False

        existing_branch = await _git(["branch", "--list", branch])
        branch_exists = bool(existing_branch.strip())

        # -----------------------------------------------------------------------
        # Reset path: tear down whatever exists so we start clean from base.
        # -----------------------------------------------------------------------
        if reset and (dir_exists or branch_exists):
            if dir_exists:
                # Remove the worktree linkage and the directory.
                rm_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", repo, "worktree", "remove", "--force", str(worktree_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await rm_proc.communicate()
                # If git worktree remove left the dir, wipe it.
                if worktree_path.exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)
                logger.info("✅ ensure_worktree: removed stale worktree %s for reset", worktree_path)

            if branch_exists:
                del_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", repo, "branch", "-D", branch,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await del_proc.communicate()
                logger.info("✅ ensure_worktree: deleted stale branch %s for reset", branch)

            # Delete the remote branch so the next push starts from a clean slate.
            # Silently ignores failure — the remote branch may not exist.
            remote_del = await asyncio.create_subprocess_exec(
                "git", "-C", repo, "push", "origin", "--delete", branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await remote_del.communicate()
            logger.info("✅ ensure_worktree: deleted remote branch %s (if existed)", branch)

            # Prune stale worktree refs.
            prune_proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo, "worktree", "prune",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await prune_proc.communicate()

            dir_exists = False
            branch_exists = False

        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        if branch_exists:
            # Branch exists but worktree dir does not — reattach without -b.
            cmd = ["git", "-C", repo, "worktree", "add", str(worktree_path), branch]
        else:
            # Neither exists — create branch and worktree together from base.
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
    _symlink_frontend_resources(worktree_path)
    return True


def _symlink_frontend_resources(worktree_path: Path) -> None:
    """Symlink frontend build resources from the main repo into the new worktree.

    Agents run npm commands (type-check, test, build:js) from within the
    worktree.  node_modules, package.json, tsconfig.json, and vitest.config.ts
    live in the main repo root and are gitignored, so they are never present in
    a freshly-created worktree.  Without these symlinks agents waste iterations
    discovering they must cd to the main repo before running npm commands.

    Uses settings.repo_dir (not a hardcoded /app) so the path is correct in
    every environment, including tests that override REPO_DIR.

    Symlinks are created only when the target in the main repo actually exists,
    and are skipped when the destination already exists (real file, real
    directory, or existing symlink) to avoid clobbering and circular links.
    """
    repo_root = settings.repo_dir.resolve()

    # Guard: never symlink if the worktree IS the main repo — that would create
    # self-referential links (e.g. /app/node_modules → /app/node_modules).
    if worktree_path.resolve() == repo_root:
        logger.warning("⚠️ _symlink_frontend_resources: worktree_path == repo_root (%s) — skipping", repo_root)
        return

    resources = ["node_modules", "package.json", "package-lock.json", "tsconfig.json", "vitest.config.ts"]
    for name in resources:
        src = repo_root / name
        dst = worktree_path / name
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            continue
        try:
            dst.symlink_to(src)
            logger.debug("🔗 Symlinked %s → %s", dst, src)
        except OSError as exc:
            logger.warning("⚠️ Could not symlink %s → %s: %s", dst, src, exc)


async def get_or_create_plan_branch(plan_id: str, repo: str) -> str:
    """Return the integration branch name for a plan, creating it from origin/dev if needed.

    Used when dispatching the first issue of a plan. Creates ``feat/plan-{slug}``
    from ``origin/dev``, pushes it to origin, and records it in ``plan_branches``.
    Subsequent dispatches for the same plan reuse the existing branch.

    Returns:
        The branch name (e.g. ``feat/plan-readme-rules``).

    Raises:
        RuntimeError: If branch creation or push fails.
    """
    from agentception.db.persist import persist_plan_branch
    from agentception.db.queries.runs import get_plan_branch

    existing = await get_plan_branch(plan_id, repo)
    if existing:
        return existing

    slug = _PLAN_SLUG_RE.sub("-", plan_id.lower()).strip("-")[:32] or "plan"
    branch_name = f"agent/plan-{slug}"

    # Ensure origin/dev is up to date so the plan branch is created from latest.
    repo_str = str(settings.repo_dir)
    fetch_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_str, "fetch", "origin", "dev",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await fetch_proc.communicate()
    if fetch_proc.returncode != 0:
        raise RuntimeError("git fetch origin dev failed")

    await ensure_branch(branch_name, "origin/dev")

    push_proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_str, "push", "-u", "origin", branch_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await push_proc.communicate()
    if push_proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git push origin {branch_name} failed: {err}")

    await persist_plan_branch(plan_id=plan_id, repo=repo, branch_name=branch_name)
    logger.info("✅ get_or_create_plan_branch: plan_id=%s branch=%s", plan_id, branch_name)
    return branch_name

