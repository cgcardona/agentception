from __future__ import annotations

"""MCP Build Command tools — explicit run lifecycle state transitions.

Every function in this module represents a state transition in the run state
machine.  Functions are grouped by audience:

Dispatcher
----------
- ``build_claim_run``       — pending_launch → implementing (was: build_acknowledge_run)

Engineers
---------
- ``build_complete_run``    — implementing → completed (was: split from build_report_done)
- ``build_teardown_worktree`` — clean up worktree after completion

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
from agentception.db.queries import get_agent_run_role, get_agent_run_teardown

from agentception.services.auto_redispatch import auto_redispatch_after_rejection
from agentception.services.auto_reviewer import auto_dispatch_reviewer
from agentception.services.teardown import release_worktree, teardown_agent_worktree

logger = logging.getLogger(__name__)


async def _rebase_and_push_worktree(wt_path: str, agent_run_id: str | None) -> dict[str, object] | None:
    """Rebase the worktree branch onto origin/dev and force-push.

    Returns ``None`` on success, or a structured error dict on rebase conflict
    that the caller should return immediately to the agent.

    Extracted into its own function so tests can mock it cleanly without
    spawning real git subprocesses against non-existent paths.

    If the worktree directory no longer exists (e.g. the agent called
    build_complete_run explicitly as a tool and the loop's stop-path
    calls it again as a safety net) the function skips the rebase and
    returns ``None`` — the branch was already pushed by the first call.
    """
    if not Path(wt_path).exists():
        logger.info(
            "ℹ️  _rebase_and_push_worktree: worktree %r already removed — skipping rebase",
            wt_path,
        )
        return None

    proc = await asyncio.create_subprocess_exec(
        "git", "fetch", "origin", "dev",
        cwd=wt_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    proc = await asyncio.create_subprocess_exec(
        "git", "rebase", "origin/dev",
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
            "❌ _rebase_and_push_worktree: rebase onto origin/dev failed for run_id=%r — %s",
            agent_run_id,
            stderr.decode(errors="replace").strip(),
        )
        return {
            "status": "error",
            "reason": "rebase_conflict",
            "message": (
                "Rebase onto origin/dev failed. "
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


async def build_claim_run(run_id: str) -> dict[str, object]:
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
) -> dict[str, object]:
    """Record that the agent has finished work and transition to completed.

    Persists the ``done`` event (linking the PR and updating workflow state),
    then transitions the run to ``completed``.  Does NOT tear down the worktree
    — call ``build_teardown_worktree`` explicitly after this if cleanup is needed.

    Was: part of ``build_report_done``.  Teardown is now a separate explicit
    command so orchestration layers can control when cleanup happens.

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
    _done_payload: dict[str, object] = {"pr_url": pr_url, "summary": summary}
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
    else:
        # Non-reviewer (implementer) completed: release worktree and dispatch reviewer.
        # Release the developer's worktree before dispatching the reviewer.
        # Git forbids the same branch in two worktrees simultaneously; if the
        # developer's worktree still holds the branch the reviewer dispatch will
        # fail with "already used by worktree at …".  We only remove the
        # worktree directory and prune refs — branches are left intact because
        # the open PR still references the remote branch.
        if agent_run_id:
            teardown_info = await get_agent_run_teardown(agent_run_id)
            wt_path = teardown_info.get("worktree_path") if teardown_info else None
            if wt_path is not None:
                # Rebase onto dev and force-push before dispatching reviewer.
                rebase_error = await _rebase_and_push_worktree(wt_path, agent_run_id)
                if rebase_error is not None:
                    return rebase_error

                await release_worktree(
                    worktree_path=wt_path,
                    repo_dir=str(settings.repo_dir),
                )

        # Fire-and-forget: reviewer failure never affects the implementer's result.
        asyncio.create_task(
            auto_dispatch_reviewer(issue_number=issue_number, pr_url=pr_url),
            name=f"auto-reviewer-{issue_number}",
        )

    return {"ok": True, "event": "done", "status": "completed"}


async def build_teardown_worktree(agent_run_id: str) -> dict[str, object]:
    """Clean up the git worktree for a completed or stopped run.

    Fires ``teardown_agent_worktree`` as a background task so the caller
    receives an immediate ack while the actual cleanup runs asynchronously.
    Teardown removes the git worktree, prunes refs, deletes the remote branch,
    and deletes the local branch.

    Call this after ``build_complete_run``.  The Dispatcher or orchestration
    layer is responsible for deciding when teardown happens — engineers should
    not call this directly.

    Was: the teardown side-effect hidden inside ``build_report_done``.

    Args:
        agent_run_id: The run ID of the completed agent (must have a worktree).

    Returns:
        ``{"ok": True, "run_id": agent_run_id, "teardown": "queued"}``
    """
    if not agent_run_id:
        return {"ok": False, "reason": "agent_run_id is required"}

    asyncio.create_task(
        teardown_agent_worktree(agent_run_id),
        name=f"teardown-{agent_run_id}",
    )
    logger.info("🧹 build_teardown_worktree: teardown queued for run_id=%r", agent_run_id)
    return {"ok": True, "run_id": agent_run_id, "teardown": "queued"}


async def build_block_run(run_id: str) -> dict[str, object]:
    """Transition an ``implementing`` run to ``blocked``.

    Call when the agent cannot proceed without external input (a human decision,
    a dependency resolving, or a required resource becoming available).  The run
    stays in ``blocked`` until ``build_resume_run`` is called.

    Only valid from ``implementing`` state.

    Args:
        run_id: The run ID to block.

    Returns:
        ``{"ok": True, "run_id": run_id, "status": "blocked"}`` on success,
        ``{"ok": False, "reason": "..."}`` if not in implementing state.
    """
    ok = await block_agent_run(run_id)
    if not ok:
        logger.warning("⚠️ build_block_run: %r not in implementing state", run_id)
        return {
            "ok": False,
            "reason": f"Run {run_id!r} not found or not in implementing state",
        }
    logger.info("✅ build_block_run: %r → blocked", run_id)
    return {"ok": True, "run_id": run_id, "status": "blocked"}


async def build_resume_run(run_id: str, agent_run_id: str) -> dict[str, object]:
    """Transition a ``blocked`` or ``stopped`` run back to ``implementing``.

    Idempotent: if the run is already ``implementing`` and ``agent_run_id``
    matches the run id, the call succeeds without a state change (restart-safe
    — an agent can call this on startup without worrying about duplicate workers).

    Valid from ``blocked`` or ``stopped`` states only.

    Args:
        run_id: The run ID to resume.
        agent_run_id: The caller's own run ID (used for idempotency check).

    Returns:
        ``{"ok": True, "run_id": run_id, "status": "implementing"}`` on success,
        ``{"ok": False, "reason": "..."}`` if not in a resumable state.
    """
    ok = await resume_agent_run(run_id, agent_run_id)
    if not ok:
        logger.warning(
            "⚠️ build_resume_run: %r not in resumable state (or agent_run_id mismatch)", run_id
        )
        return {
            "ok": False,
            "reason": (
                f"Run {run_id!r} not found, not in a resumable state (blocked/stopped), "
                "or agent_run_id does not match"
            ),
        }
    logger.info("✅ build_resume_run: %r → implementing", run_id)
    return {"ok": True, "run_id": run_id, "status": "implementing"}


async def build_cancel_run(run_id: str) -> dict[str, object]:
    """Transition any active run to ``cancelled``.

    ``cancelled`` is a terminal state — the run cannot be resumed.  Use
    ``build_stop_run`` if you want to pause and later resume.

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


async def build_stop_run(run_id: str) -> dict[str, object]:
    """Transition any active run to ``stopped``.

    Unlike ``build_cancel_run``, a stopped run can be resumed via
    ``build_resume_run``.  Use this when you want to pause a run for inspection
    without permanently closing it.

    Valid from any non-terminal state.

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
) -> dict[str, object]:
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
