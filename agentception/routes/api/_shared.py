from __future__ import annotations

"""Shared helpers and constants for all JSON API routes.

Contains:
- ``_SENTINEL``: path to the pipeline-pause sentinel file.
- ``ROLE_DEFAULT_FIGURE``: re-exported from ``services.cognitive_arch``.
- ``_derive_skills_from_body``: re-exported from ``services.cognitive_arch``.
- ``_extract_skills_from_body``: re-exported from ``services.cognitive_arch``.
- ``_resolve_cognitive_arch``: re-exported from ``services.cognitive_arch``.
- ``_build_agent_task``: constructs ``.agent-task`` file content for engineer agents.
- ``_build_coordinator_task``: constructs ``.agent-task`` for brain-dump coordinators.
- ``_build_conductor_task``: constructs ``.agent-task`` for conductor/CTO agents.
- ``_issue_is_claimed_api``: checks ``agent:wip`` label presence.
"""

from datetime import datetime, timezone
from pathlib import Path

from agentception.config import settings
from agentception.services.cognitive_arch import (
    ROLE_DEFAULT_FIGURE as ROLE_DEFAULT_FIGURE,
    _derive_skills_from_body as _derive_skills_from_body,
    _extract_skills_from_body as _extract_skills_from_body,
    _resolve_cognitive_arch as _resolve_cognitive_arch,
)

# Path to the sentinel file that pauses the agent pipeline.
# Writing this file tells CTO and coordinator loops to wait rather than spawn agents.
_SENTINEL: Path = settings.ac_dir / ".pipeline-pause"


def _build_agent_task(
    issue_number: int,
    title: str,
    role: str,
    worktree: Path,
    host_worktree: Path,
    branch: str,
    phase_label: str = "",
    depends_on: str = "none",
    cognitive_arch: str = "hopper:python",
    wave_id: str = "manual",
) -> str:
    """Build the raw text content of a ``.agent-task`` file.

    The format mirrors what the ``parallel-issue-to-pr.md`` coordinator
    script generates so that agents spawned via the control plane receive
    the same context as batch-spawned agents.

    ``worktree`` is the container-side path (written to the file for Docker
    commands).  ``host_worktree`` is the host-side path embedded as
    ``HOST_WORKTREE`` so the Cursor Task launcher can use the correct path
    when opening the worktree as a project root.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    # ROLE_FILE is metadata only — the kickoff prompt embeds all role content
    # inline.  The path uses the host repo dir so it is human-readable even
    # though agents are instructed not to read it from disk.
    role_file_display = f"<host-repo>/.agentception/roles/{role}.md"
    return (
        f"WORKFLOW=issue-to-pr\n"
        f"GH_REPO={repo}\n"
        f"ISSUE_NUMBER={issue_number}\n"
        f"ISSUE_TITLE={title}\n"
        f"ISSUE_URL=https://github.com/{repo}/issues/{issue_number}\n"
        f"PHASE_LABEL={phase_label}\n"
        f"DEPENDS_ON={depends_on}\n"
        f"BRANCH={branch}\n"
        f"ROLE={role}\n"
        f"ROLE_FILE={role_file_display}\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"CLOSES_ISSUES={issue_number}\n"
        f"BATCH_ID={wave_id}\n"
        f"WAVE={wave_id}\n"
        f"COGNITIVE_ARCH={cognitive_arch}\n"
        f"CREATED_AT={now}\n"
        f"SPAWN_MODE=chain\n"
        f"LINKED_PR=none\n"
        f"SPAWN_SUB_AGENTS=false\n"
        f"ATTEMPT_N=0\n"
        f"REQUIRED_OUTPUT=pr_url\n"
        f"ON_BLOCK=stop\n"
    )


def _build_coordinator_task(
    slug: str,
    plan_text: str,
    label_prefix: str,
    worktree: Path,
    host_worktree: Path,
    branch: str,
) -> str:
    """Build the ``.agent-task`` content for a plan coordinator worktree.

    The coordinator agent reads ``WORKFLOW=bugs-to-issues`` and follows
    ``parallel-bugs-to-issues.md``: it runs the Phase Planner, creates GitHub
    labels, creates worktrees for each batch, writes sub-agent task files, and
    launches sub-agents.  AgentCeption's only job is to prepare the worktree
    and this file — the Cursor background agent does all LLM work.

    The ``PLAN_DUMP`` section is appended as a freeform block after the
    structured key=value header so the coordinator can read it verbatim.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    prefix_line = f"LABEL_PREFIX={label_prefix}\n" if label_prefix else ""
    return (
        f"WORKFLOW=bugs-to-issues\n"
        f"GH_REPO={repo}\n"
        f"ROLE=coordinator\n"
        f"ROLE_FILE=<host-repo>/.agentception/roles/coordinator.md\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"BATCH_ID={slug}\n"
        f"WAVE={slug}\n"
        f"COGNITIVE_ARCH={ROLE_DEFAULT_FIGURE.get('engineering-coordinator', 'von_neumann')}:python\n"
        f"{prefix_line}"
        f"CREATED_AT={now}\n"
        f"SPAWN_MODE=chain\n"
        f"SPAWN_SUB_AGENTS=true\n"
        f"ATTEMPT_N=0\n"
        f"REQUIRED_OUTPUT=phase_plan\n"
        f"ON_BLOCK=stop\n"
        f"\nPLAN_DUMP:\n{plan_text}\n"
    )


def _build_conductor_task(
    wave_id: str,
    phases: list[str],
    org: str | None,
    worktree: Path,
    host_worktree: Path,
    branch: str,
) -> str:
    """Build the ``.agent-task`` content for a conductor worktree.

    The conductor agent reads ``WORKFLOW=conductor`` and coordinates across the
    listed phases, spawning sub-agents for each unclaimed issue.  AgentCeption
    only prepares the worktree and this file — all LLM work happens inside
    the Cursor background agent that opens the returned ``host_worktree``.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    return (
        f"WORKFLOW=conductor\n"
        f"GH_REPO={repo}\n"
        f"ROLE=conductor\n"
        f"ROLE_FILE=<host-repo>/.agentception/roles/conductor.md\n"
        f"WAVE_ID={wave_id}\n"
        f"PHASES={','.join(phases)}\n"
        f"ORG={org or ''}\n"
        f"BRANCH={branch}\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"BATCH_ID={wave_id}\n"
        f"WAVE={wave_id}\n"
        f"COGNITIVE_ARCH={ROLE_DEFAULT_FIGURE.get('conductor', 'jeff_dean')}:python\n"
        f"CREATED_AT={now}\n"
        f"SPAWN_MODE=chain\n"
        f"SPAWN_SUB_AGENTS=true\n"
        f"ATTEMPT_N=0\n"
        f"REQUIRED_OUTPUT=wave_complete\n"
        f"ON_BLOCK=stop\n"
    )


def _issue_is_claimed_api(iss: dict[str, object]) -> bool:
    """Return True when an issue carries the ``agent:wip`` label."""
    raw = iss.get("labels")
    if not isinstance(raw, list):
        return False
    for lbl in raw:
        if isinstance(lbl, str) and lbl == "agent:wip":
            return True
        if isinstance(lbl, dict) and lbl.get("name") == "agent:wip":
            return True
    return False
