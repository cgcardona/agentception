from __future__ import annotations

"""Shared helpers and constants for all JSON API routes.

Contains:
- ``_SENTINEL``: path to the pipeline-pause sentinel file.
- ``_build_agent_task``: constructs ``.agent-task`` file content for engineer agents.
- ``_build_coordinator_task``: constructs ``.agent-task`` for brain-dump coordinators.
- ``_resolve_cognitive_arch``: derives COGNITIVE_ARCH string from issue body.
- ``_issue_is_claimed_api``: checks ``agent:wip`` label presence.
"""

from datetime import datetime, timezone
from pathlib import Path

from agentception.config import settings

# Path to the sentinel file that pauses the agent pipeline.
# Writing this file tells CTO and Eng VP loops to wait rather than spawn agents.
_SENTINEL: Path = settings.ac_dir / ".pipeline-pause"


def _resolve_cognitive_arch(issue_body: str, role: str) -> str:
    """Derive COGNITIVE_ARCH string from issue body and role.

    Format: ``figure:skill1:skill2``.  Mirrors the logic in
    ``parallel-issue-to-pr.md`` so agents spawned via the control plane
    receive the same architectural context as batch-spawned agents.
    """
    body = issue_body.lower()

    if any(k in body for k in ("d3.js", "force-directed", "d3.force", "d3.select")):
        skills = "d3:javascript"
    elif any(k in body for k in ("monaco", "vs/loader", "editor.*cdn")):
        skills = "monaco"
    elif any(k in body for k in ("htmx", "hx-", "sse-connect", "hx-ext")):
        skills = "htmx"
        if any(k in body for k in ("jinja2", ".html", "templateresponse", "extends.*html")):
            skills += ":jinja2"
        if any(k in body for k in ("alpine", "x-data", "x-show")):
            skills += ":alpine"
    elif any(k in body for k in ("jinja2", "templateresponse", "extends.*html")):
        skills = "jinja2"
    elif any(k in body for k in ("postgres", "alembic", "migration", "sqlalchemy")):
        skills = "postgresql:python"
    elif any(k in body for k in ("dockerfile", "from python", "compose.*service")):
        skills = "devops"
    elif any(k in body for k in ("midi", "muse", "variation", "beat")):
        skills = "midi:python"
    elif any(k in body for k in ("llm", "embedding", "rag", "openrouter", "claude")):
        skills = "llm:python"
    elif any(k in body for k in ("apirouter", "fastapi", "depends", "response_model")):
        skills = "fastapi:python"
    else:
        skills = "python"

    if any(k in body for k in ("migration", "alembic", "schema", "db.model", "postgres")):
        figure = "dijkstra"
    elif any(k in body for k in ("sse", "broadcast", "async", "asyncio", "fanout")):
        figure = "shannon"
    elif any(k in body for k in ("overview", "dashboard", "pipeline", "tree")):
        figure = "lovelace"
    elif any(k in body for k in ("api", "endpoint", "route", "contract")):
        figure = "turing"
    else:
        figure = "hopper"

    return f"{figure}:{skills}"


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
    role_file_display = f"<host-repo>/.cursor/roles/{role}.md"
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
        f"ROLE_FILE=<host-repo>/.cursor/roles/coordinator.md\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"BATCH_ID={slug}\n"
        f"WAVE={slug}\n"
        f"COGNITIVE_ARCH=coordinator\n"
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
        f"ROLE_FILE=<host-repo>/.cursor/roles/conductor.md\n"
        f"WAVE_ID={wave_id}\n"
        f"PHASES={','.join(phases)}\n"
        f"ORG={org or ''}\n"
        f"BRANCH={branch}\n"
        f"WORKTREE={worktree}\n"
        f"HOST_WORKTREE={host_worktree}\n"
        f"BASE=dev\n"
        f"BATCH_ID={wave_id}\n"
        f"WAVE={wave_id}\n"
        f"COGNITIVE_ARCH=conductor\n"
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
