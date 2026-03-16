from __future__ import annotations

"""MCP Build Command tools — explicit run lifecycle state transitions.

Every function in this module represents a state transition in the run state
machine.  Functions are grouped by audience:

Dispatcher
----------
- ``build_claim_run``       — pending_launch → implementing

Engineers
---------
- ``build_complete_run``    — implementing → completed

Coordinators
------------
- ``build_spawn_adhoc_child`` — spawn a child run from a coordinator

Lifecycle
---------
- ``build_cancel_run``      — any active → cancelled (terminal)

Rules
-----
- These tools change run state.  They never append telemetry events.
- All state transitions are validated server-side (see db/persist.py).
- Each tool returns a structured dict — never prose.
"""

import asyncio
import logging
import re
from pathlib import Path

import httpx

from agentception.types import JsonValue
from agentception.db.persist import (
    acknowledge_agent_run,
    block_agent_run,
    cancel_agent_run,
    complete_agent_run,
    persist_agent_event,
    resume_agent_run,
    stop_agent_run,
)
from agentception.config import settings
from agentception.db.queries import (
    all_plan_issues_merged_into_plan_branch,
    get_agent_run_role,
    get_agent_run_teardown,
    get_plan_branch,
    get_plan_id_for_issue,
)

from agentception.services.auto_redispatch import auto_redispatch_after_rejection
from agentception.services.auto_reviewer import auto_dispatch_reviewer
from agentception.services.teardown import release_worktree, teardown_agent_worktree

logger = logging.getLogger(__name__)


async def _rebase_and_push_worktree(
    wt_path: str,
    agent_run_id: str | None,
    base: str = "origin/dev",
) -> dict[str, JsonValue] | None:
    """Rebase the worktree branch onto *base* and force-push.

    Returns ``None`` on success, or a structured error dict on rebase conflict
    that the caller should return immediately to the agent.

    When the run is plan-scoped, *base* is the plan branch (e.g. origin/feat/plan-xyz)
    so the implementer's branch is rebased onto the plan branch before the reviewer
    is dispatched. Otherwise *base* is origin/dev.
    """
    if not Path(wt_path).exists():
        logger.info(
            "ℹ️  _rebase_and_push_worktree: worktree %r already removed — skipping rebase",
            wt_path,
        )
        return None

    base_branch_name = base.replace("origin/", "") if base.startswith("origin/") else base
    proc = await asyncio.create_subprocess_exec(
        "git", "fetch", "origin", base_branch_name,
        cwd=wt_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    proc = await asyncio.create_subprocess_exec(
        "git", "rebase", base,
        cwd=wt_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        abort_proc = await asyncio.create_subprocess_exec(
            "git", "rebase", "--abort",
            cwd=wt_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await abort_proc.communicate()
        logger.error(
            "❌ _rebase_and_push_worktree: rebase onto %s failed for run_id=%r — %s",
            base,
            agent_run_id,
            stderr.decode(errors="replace").strip(),
        )
        return {
            "status": "error",
            "reason": "rebase_conflict",
            "message": (
                f"Rebase onto {base} failed. "
                "Resolve the conflicts manually and call build_complete_run again."
            ),
        }

    branch_proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "--abbrev-ref", "HEAD",
        cwd=wt_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    branch_stdout, _ = await branch_proc.communicate()
    branch_name = branch_stdout.decode().strip()

    proc = await asyncio.create_subprocess_exec(
        "git", "push", "--force-with-lease", "origin", branch_name,
        cwd=wt_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return None


async def _maybe_trigger_plan_merge(plan_id: str) -> None:
    """If all plan issues are merged into the plan branch, rebase onto dev, open PR, dispatch reviewer.

    Called as a background task when a reviewer merges an issue PR (grade A/B).
    If this was the last issue in the plan, we rebase the plan branch onto dev,
    open the plan→dev PR, and dispatch a reviewer to merge it. Never raises.
    """
    repo = settings.gh_repo
    plan_branch = await get_plan_branch(plan_id, repo)
    if not plan_branch:
        logger.info("ℹ️ _maybe_trigger_plan_merge: plan_id=%s has no branch — skipping", plan_id)
        return
    if not await all_plan_issues_merged_into_plan_branch(plan_id, repo, plan_branch):
        logger.info(
            "ℹ️ _maybe_trigger_plan_merge: plan_id=%s not all issues merged yet — skipping",
            plan_id,
        )
        return
    repo_dir = str(settings.repo_dir)
    try:
        # Rebase plan branch onto origin/dev in the main repo.
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "fetch", "origin", "dev",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "checkout", plan_branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            logger.warning("⚠️ _maybe_trigger_plan_merge: checkout %s failed — skipping", plan_branch)
            return
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "rebase", "origin/dev",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "⚠️ _maybe_trigger_plan_merge: rebase plan %s onto dev failed — skipping",
                plan_branch,
            )
            return
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_dir, "push", "--force-with-lease", "origin", plan_branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            logger.warning("⚠️ _maybe_trigger_plan_merge: push %s failed — skipping", plan_branch)
            return
    except Exception as exc:
        logger.warning("⚠️ _maybe_trigger_plan_merge: %s — skipping", exc)
        return

    from agentception.readers.github import ensure_pull_request
    pr_number, _ = await ensure_pull_request(
        head=plan_branch,
        base="dev",
        title=f"Merge plan into dev ({plan_id})",
        body="Plan integration branch — all issue PRs merged. Review and merge.",
    )
    pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    from agentception.services.auto_reviewer import auto_dispatch_reviewer
    await auto_dispatch_reviewer(
        issue_number=0,
        pr_url=pr_url,
        pr_branch=plan_branch,
    )
    logger.info(
        "✅ _maybe_trigger_plan_merge: plan_id=%s plan→dev PR #%d opened, reviewer dispatched",
        plan_id,
        pr_number,
    )


async def build_claim_run(run_id: str) -> dict[str, JsonValue]:
    """Atomically claim a pending run before spawning its Task agent.

    Transitions the run from ``pending_launch`` → ``implementing``.  Call this
    immediately before firing the Task so the run cannot be double-claimed by a
    concurrent Dispatcher.

    Was: ``build_acknowledge_run``.

    Args:
        run_id: The ``run_id`` returned by ``query_pending_runs``
                (e.g. ``"label-cognitive-arch-propagation-7352b9"``).

    Returns:
        ``{"ok": True, "run_id": run_id, "previous_state": "pending_launch"}`` on
        success, or ``{"ok": False, "reason": "..."}`` when the run is not found
        or was already claimed by another Dispatcher.
    """
    ok = await acknowledge_agent_run(run_id)
    if not ok:
        logger.warning(
            "⚠️ build_claim_run: %r not found or already claimed — skipping",
            run_id,
        )
        return {
            "ok": False,
            "reason": f"Run {run_id!r} not found or not in pending_launch state",
        }
    logger.info("✅ build_claim_run: %r claimed", run_id)
    return {"ok": True, "run_id": run_id, "previous_state": "pending_launch"}


# Matches https://github.com/<owner>/<repo>/pull/<number>
# Used to validate the pr_url argument before accepting build_complete_run.
_GITHUB_PR_URL_RE: re.Pattern[str] = re.compile(
    r"https://github\.com/[^/]+/[^/]+/pull/\d+"
)


async def _is_pr_merged(pr_url: str) -> bool:
    """Return True if the GitHub PR at *pr_url* has been merged.

    Parses the owner, repo, and PR number from the URL and calls the GitHub
    REST API.  Returns False on any network or auth error so the caller can
    decide whether to treat the failure as a hard block or a soft warning.
    """
    m = re.search(
        r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)",
        pr_url,
    )
    if not m:
        return False
    owner, repo, number = m.group("owner"), m.group("repo"), m.group("number")
    token = settings.github_token
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/merge",
                headers=headers,
            )
        # 204 = merged; 404 = not merged; anything else = uncertain
        return resp.status_code == 204
    except Exception:
        logger.warning("⚠️ _is_pr_merged: GitHub API call failed for %r — assuming not merged", pr_url)
        return False


def _is_valid_pr_url(pr_url: str) -> bool:
    """Return True if pr_url looks like a valid GitHub pull-request URL.

    Validates the *argument* passed to build_complete_run rather than doing a
    DB lookup.  The PR number is only written to ACAgentRun after this function
    succeeds, so any DB-based check would always return False — a deadlock.
    """
    return bool(_GITHUB_PR_URL_RE.search(pr_url))


async def build_complete_run(
    issue_number: int,
    pr_url: str,
    summary: str = "",
    agent_run_id: str | None = None,
    grade: str = "",
    reviewer_feedback: str = "",
) -> dict[str, JsonValue]:
    """Record that the agent has finished work and transition to completed.

    Persists the ``done`` event (linking the PR and updating workflow state),
    then transitions the run to ``completed``.  Worktree teardown is handled
    automatically by the worktree reaper.

    When called by a reviewer with a failing grade (C/D/F), a new developer
    run is automatically dispatched with the reviewer's feedback injected into
    the briefing — up to 3 attempts before the loop is abandoned.

    Args:
        issue_number: GitHub issue number the agent worked on.
        pr_url: Full URL of the opened (or rejected) pull request.
        summary: Optional one-sentence description of what was done.
        agent_run_id: Run ID used to transition the run state.
        grade: Grade assigned by the reviewer (e.g. "A", "B", "C", "D", "F").
            Empty string when called by a non-reviewer agent.
        reviewer_feedback: Full defect list from the reviewer.  Injected
            verbatim into the re-dispatched developer briefing.  Empty string
            when called by a non-reviewer agent.

    Returns:
        ``{"ok": True, "event": "done", "status": "completed"}``
    """
    # --- Pre-flight guard: pr_url must be a valid GitHub pull-request URL ---
    # We validate the argument directly rather than doing a DB lookup.
    # The PR number is written to ACAgentRun.pr_number by persist_agent_event
    # (called later in this function), so any DB check would always return False
    # for a developer that just opened a PR — a deadlock that caused agents to
    # loop to 100 iterations trying to satisfy a guard they could never pass.
    if not _is_valid_pr_url(pr_url):
        return {
            "ok": False,
            "error": (
                "build_complete_run refused: pr_url must be a valid GitHub pull-request URL "
                f"(e.g. https://github.com/owner/repo/pull/123). Got: {pr_url!r}. "
                "Call create_pull_request first, then pass the returned URL here."
            ),
        }
    # -------------------------

    # ── Determine caller role before persisting so the done-event payload
    # can include grade/feedback for reviewer runs.
    caller_role = await get_agent_run_role(agent_run_id) if agent_run_id else None
    _done_payload: dict[str, JsonValue] = {"pr_url": pr_url, "summary": summary}
    if caller_role == "reviewer":
        _done_payload["grade"] = grade.strip().upper()
        _done_payload["reviewer_feedback"] = reviewer_feedback

    await persist_agent_event(
        issue_number=issue_number,
        event_type="done",
        payload=_done_payload,
        agent_run_id=agent_run_id,
    )

    if agent_run_id:
        ok = await complete_agent_run(agent_run_id)
        if not ok:
            logger.warning(
                "⚠️ build_complete_run: complete_agent_run returned False for run_id=%r "
                "(run may not be in implementing state — event still recorded)",
                agent_run_id,
            )

    logger.info(
        "✅ build_complete_run: issue=%d pr_url=%r run_id=%r",
        issue_number, pr_url, agent_run_id,
    )

    # Reviewer path: grade determines whether to merge (handled by reviewer) or
    # redispatch a corrected developer run.
    if caller_role == "reviewer":
        VALID_REVIEWER_GRADES: frozenset[str] = frozenset({"A", "B", "C", "D", "F"})
        _FAILING_GRADES: frozenset[str] = frozenset({"C", "D", "F"})
        normalised_grade = str(_done_payload["grade"])
        # Reviewer must commit to a valid grade before merge/redispatch logic runs.
        if normalised_grade not in VALID_REVIEWER_GRADES:
            return {
                "error": (
                    f"Invalid grade {normalised_grade!r}. "
                    "Reviewer must supply one of: A, B, C, D, F. "
                    "Call build_complete_run again with a valid grade."
                )
            }
        if normalised_grade in _FAILING_GRADES:
            logger.info(
                "ℹ️ build_complete_run: reviewer rejected (grade=%r) — "
                "releasing worktree and scheduling redispatch for issue #%d run_id=%r",
                grade,
                issue_number,
                agent_run_id,
            )
            # Resolve the PR branch name and worktree path from DB before releasing.
            pr_branch: str | None = None
            if agent_run_id:
                teardown_info = await get_agent_run_teardown(agent_run_id)
                wt_path = teardown_info.get("worktree_path") if teardown_info else None
                pr_branch = teardown_info.get("branch") if teardown_info else None
                if wt_path:
                    # Release the reviewer's worktree synchronously BEFORE scheduling
                    # redispatch.  release_worktree removes the worktree directory and
                    # prunes refs but does NOT delete the branch — the developer
                    # continuation dispatch needs the branch to be alive.
                    # This sequencing guarantees git will never see the branch checked
                    # out in two worktrees simultaneously.
                    await release_worktree(wt_path, str(settings.repo_dir))
                    logger.info(
                        "🧹 build_complete_run: reviewer worktree released (branch kept) for run_id=%r",
                        agent_run_id,
                    )

            asyncio.create_task(
                auto_redispatch_after_rejection(
                    issue_number=issue_number,
                    pr_url=pr_url,
                    reviewer_feedback=reviewer_feedback,
                    grade=normalised_grade,
                    pr_branch=pr_branch,
                ),
                name=f"auto-redispatch-{issue_number}",
            )
        else:
            # Grade A or B: reviewer should have already merged the PR.  Verify
            # before scheduling teardown — teardown deletes the remote branch,
            # which causes GitHub to auto-close any open PR pointing to it.  If
            # the PR is not yet merged, refusing here forces the reviewer to
            # actually call merge_pull_request before completion is recorded.
            pr_is_merged = await _is_pr_merged(pr_url)
            if not pr_is_merged:
                logger.warning(
                    "⚠️ build_complete_run: reviewer called with grade=%r but PR %r "
                    "is not merged — refusing completion to prevent branch deletion",
                    grade,
                    pr_url,
                )
                return {
                    "ok": False,
                    "error": (
                        f"PR {pr_url!r} has not been merged. "
                        "Call merge_pull_request first, then call build_complete_run again. "
                        "Completing without a merge would delete the branch and close the PR."
                    ),
                }
            logger.info(
                "ℹ️ build_complete_run: reviewer approved (grade=%r) — "
                "scheduling full worktree teardown for run_id=%r",
                grade,
                agent_run_id,
            )
            if agent_run_id:
                asyncio.create_task(
                    teardown_agent_worktree(agent_run_id),
                    name=f"teardown-{agent_run_id}",
                )
                logger.info(
                    "🧹 build_complete_run: reviewer worktree teardown queued for run_id=%r",
                    agent_run_id,
                )
            # If this issue belongs to a plan, check whether all plan issues are merged;
            # if so, rebase plan branch onto dev, open plan→dev PR, and dispatch reviewer.
            plan_id = await get_plan_id_for_issue(issue_number, settings.gh_repo)
            if plan_id:
                asyncio.create_task(
                    _maybe_trigger_plan_merge(plan_id),
                    name=f"plan-merge-{plan_id}",
                )
    else:
        # Non-reviewer (implementer) completed: release worktree and dispatch reviewer.
        # Release the developer's worktree before dispatching the reviewer.
        # Git forbids the same branch in two worktrees simultaneously; if the
        # developer's worktree still holds the branch the reviewer dispatch will
        # fail with "already used by worktree at …".  We only remove the
        # worktree directory and prune refs — branches are left intact because
        # the open PR still references the remote branch.
        #
        # When the DB has no worktree_path (e.g. null or run not found), derive
        # the conventional path worktrees_dir/run_id so we still attempt release —
        # otherwise the reviewer dispatch fails with "branch already used by worktree".
        worktree_released = True
        if agent_run_id:
            teardown_info = await get_agent_run_teardown(agent_run_id)
            wt_path = (
                (teardown_info.get("worktree_path") if teardown_info else None)
                or str(Path(settings.worktrees_dir) / agent_run_id)
            )
            if wt_path and Path(wt_path).exists() and teardown_info:
                # Rebase onto plan branch (if plan-scoped) or dev, then force-push.
                rebase_base = "origin/dev"
                if teardown_info.get("plan_branch"):
                    rebase_base = f"origin/{teardown_info['plan_branch']}"
                rebase_error = await _rebase_and_push_worktree(
                    wt_path, agent_run_id, base=rebase_base
                )
                if rebase_error is not None:
                    return rebase_error

            if wt_path:
                worktree_released = await release_worktree(
                    worktree_path=wt_path,
                    repo_dir=str(settings.repo_dir),
                )
                if not worktree_released:
                    logger.warning(
                        "⚠️ build_complete_run: worktree release failed for run_id=%r — "
                        "reviewer not dispatched (would fail with branch already in use)",
                        agent_run_id,
                    )
                    return {
                        "ok": False,
                        "error": (
                            "Worktree release failed; reviewer was not dispatched. "
                            "The PR is open. Retry build_complete_run after the worktree is released, "
                            "or an operator can run release_worktree and dispatch the reviewer manually."
                        ),
                    }

        if worktree_released:
            asyncio.create_task(
                auto_dispatch_reviewer(issue_number=issue_number, pr_url=pr_url),
                name=f"auto-reviewer-{issue_number}",
            )

    return {"ok": True, "event": "done", "status": "completed"}


async def build_cancel_run(run_id: str) -> dict[str, JsonValue]:
    """Transition any active run to ``cancelled``.

    ``cancelled`` is a terminal state — the run cannot be resumed.

    Valid from any non-terminal state (pending_launch, implementing, blocked,
    reviewing).

    Args:
        run_id: The run ID to cancel.

    Returns:
        ``{"ok": True, "run_id": run_id, "status": "cancelled"}`` on success,
        ``{"ok": False, "reason": "..."}`` if already terminal.
    """
    ok = await cancel_agent_run(run_id)
    if not ok:
        logger.warning("⚠️ build_cancel_run: %r not found or already terminal", run_id)
        return {
            "ok": False,
            "reason": f"Run {run_id!r} not found or already in a terminal state",
        }
    logger.info("✅ build_cancel_run: %r → cancelled", run_id)
    return {"ok": True, "run_id": run_id, "status": "cancelled"}


async def build_block_run(run_id: str) -> dict[str, JsonValue]:
    """Transition an ``implementing`` run to ``blocked``.

    Use this when the agent cannot proceed until a dependency resolves or a
    human intervenes.  A blocked run can be resumed later via
    :func:`build_resume_run`.  Only succeeds from ``implementing`` state.

    Args:
        run_id: The run ID to block.

    Returns:
        ``{"ok": True, "run_id": run_id, "status": "blocked"}`` on success,
        ``{"ok": False, "reason": "..."}`` if the run is not implementing.
    """
    ok = await block_agent_run(run_id)
    if not ok:
        logger.warning("⚠️ build_block_run: %r not found or not in implementing state", run_id)
        return {
            "ok": False,
            "reason": f"Run {run_id!r} not found or not in implementing state",
        }
    logger.info("✅ build_block_run: %r → blocked", run_id)
    return {"ok": True, "run_id": run_id, "status": "blocked"}


async def build_resume_run(run_id: str, agent_run_id: str) -> dict[str, JsonValue]:
    """Transition a ``blocked`` or ``stopped`` run back to ``implementing``.

    Idempotent: if the run is already ``implementing`` and the caller's
    ``agent_run_id`` matches, the call succeeds so a crashed-and-restarted
    agent can safely call this on startup.

    Args:
        run_id:       The run ID to resume.
        agent_run_id: The caller's own run ID (used for idempotency check).

    Returns:
        ``{"ok": True, "run_id": run_id, "status": "implementing"}`` on success,
        ``{"ok": False, "reason": "..."}`` if the run is not resumable.
    """
    ok = await resume_agent_run(run_id, agent_run_id)
    if not ok:
        logger.warning("⚠️ build_resume_run: %r not resumable", run_id)
        return {
            "ok": False,
            "reason": f"Run {run_id!r} not found or not in a resumable state",
        }
    logger.info("✅ build_resume_run: %r → implementing", run_id)
    return {"ok": True, "run_id": run_id, "status": "implementing"}


async def build_stop_run(run_id: str) -> dict[str, JsonValue]:
    """Transition any active run to ``stopped``.

    Unlike ``build_cancel_run``, a stopped run can be resumed later via
    :func:`build_resume_run`.

    Args:
        run_id: The run ID to stop.

    Returns:
        ``{"ok": True, "run_id": run_id, "status": "stopped"}`` on success,
        ``{"ok": False, "reason": "..."}`` if already terminal.
    """
    ok = await stop_agent_run(run_id)
    if not ok:
        logger.warning("⚠️ build_stop_run: %r not found or already terminal", run_id)
        return {
            "ok": False,
            "reason": f"Run {run_id!r} not found or already in a terminal state",
        }
    logger.info("✅ build_stop_run: %r → stopped", run_id)
    return {"ok": True, "run_id": run_id, "status": "stopped"}


async def build_spawn_adhoc_child(
    parent_run_id: str,
    role: str,
    task_description: str,
    figure: str = "",
    base_branch: str = "origin/dev",
) -> dict[str, JsonValue]:
    """Spawn a child agent run from within another agent's tool loop.

    This is the MCP-native way for a coordinator to dispatch engineer agents.
    It is equivalent to ``POST /api/runs/adhoc`` but callable directly by an
    agent without touching the REST API.

    The child run gets its own git worktree, a DB row with
    ``parent_run_id`` linking it to this coordinator, and the agent loop
    fires immediately as an asyncio task.

    Args:
        parent_run_id: ``run_id`` of the calling coordinator — used to link
            the child in the DB for hierarchy tracking.
        role: Role slug for the child agent (e.g. ``"developer"``).
        task_description: Plain-language description of the child's task.
            Be specific: files to touch, expected output, constraints.
        figure: Cognitive figure slug override (e.g. ``"guido_van_rossum"``).
            When empty the default for the role is used.
        base_branch: Git ref to branch the worktree from.

    Returns:
        ``{"ok": True, "child_run_id": str, "worktree_path": str,
           "cognitive_arch": str}`` on success.
        ``{"ok": False, "error": str}`` if worktree or DB creation fails.
    """
    from agentception.services.run_factory import RunCreationError, create_and_launch_run  # noqa: PLC0415

    try:
        result = await create_and_launch_run(
            role=role,
            task_description=task_description,
            figure=figure or None,
            base_branch=base_branch,
            parent_run_id=parent_run_id,
        )
    except RunCreationError as exc:
        logger.error(
            "❌ build_spawn_adhoc_child: failed for parent=%s role=%s — %s",
            parent_run_id, role, exc,
        )
        return {"ok": False, "error": str(exc)}

    logger.info(
        "✅ build_spawn_adhoc_child: parent=%s spawned child=%s role=%s",
        parent_run_id, result["run_id"], role,
    )
    return {
        "ok": True,
        "child_run_id": result["run_id"],
        "worktree_path": result["worktree_path"],
        "cognitive_arch": result["cognitive_arch"],
    }
