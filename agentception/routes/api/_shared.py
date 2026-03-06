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

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

from agentception.config import settings
from agentception.services.cognitive_arch import (
    ROLE_DEFAULT_FIGURE as ROLE_DEFAULT_FIGURE,
    _derive_skills_from_body as _derive_skills_from_body,
    _extract_skills_from_body as _extract_skills_from_body,
    _resolve_cognitive_arch as _resolve_cognitive_arch,
)
from agentception.services.toml_task import TomlValue, render_toml_str, toml_val

# Path to the sentinel file that pauses the agent pipeline.
# Writing this file tells CTO and coordinator loops to wait rather than spawn agents.
_SENTINEL: Path = settings.ac_dir / ".pipeline-pause"

# Internal aliases kept for callers within this module.
_TomlValue: TypeAlias = TomlValue
_toml_val = toml_val
_render_toml_str = render_toml_str


def _build_agent_task(
    issue_number: int,
    title: str,
    role: str,
    worktree: Path,
    host_worktree: Path,
    branch: str,
    phase_label: str = "",
    depends_on: list[int] | None = None,
    cognitive_arch: str = "hopper:python",
    wave_id: str = "manual",
    file_ownership: list[str] | None = None,
) -> str:
    """Build the TOML v2 content of a ``.agent-task`` file for an engineer agent.

    Emits a fully-structured TOML document following the v2.0 spec in
    ``.agentception/agent-task-spec.md``.  The file is consumed by both the
    AgentCeption dashboard (via ``parse_agent_task()`` / ``tomllib``) and the
    Cursor LLM (raw text as context), so every field must be valid TOML.

    ``worktree`` is the container-side path (retained for backward compat).
    ``host_worktree`` is the host-side path written to ``[worktree].path`` so
    the Cursor Task launcher opens the correct directory.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    dep_list: list[int] = depends_on if depends_on is not None else []
    ownership: list[str] = file_ownership if file_ownership is not None else []

    sections: dict[str, dict[str, _TomlValue]] = {
        "task": {
            "version": "2.0",
            "workflow": "issue-to-pr",
            "id": str(uuid.uuid4()),
            "created_at": now,
            "attempt_n": 0,
            "required_output": "pr_url",
            "on_block": "stop",
        },
        "agent": {
            "role": role,
            "tier": "engineer",
            "org_domain": "engineering",
            "cognitive_arch": cognitive_arch,
        },
        "repo": {
            "gh_repo": repo,
            "base": "dev",
        },
        "pipeline": {
            "batch_id": wave_id,
            "wave": wave_id,
        },
        "spawn": {
            "mode": "chain",
            "sub_agents": False,
        },
        "target": {
            "issue_number": issue_number,
            "issue_title": title,
            "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
            "phase_label": phase_label,
            "depends_on": dep_list,
            "closes": [issue_number],
            "file_ownership": ownership,
        },
        "worktree": {
            "path": str(host_worktree),
            "branch": branch,
            "linked_pr": 0,
        },
    }
    return _render_toml_str(sections)


def _build_coordinator_task(
    slug: str,
    plan_text: str,
    label_prefix: str,
    worktree: Path,
    host_worktree: Path,
    branch: str,
) -> str:
    """Build the TOML v2 ``.agent-task`` content for a plan coordinator worktree.

    The coordinator agent reads ``task.workflow = "bugs-to-issues"`` and runs
    the Phase Planner, creates GitHub labels, creates worktrees for each batch,
    and launches sub-agents.  AgentCeption only prepares the worktree and this
    file — the Cursor background agent does all LLM work.

    The raw brain dump is stored in ``[plan_draft].dump`` as a TOML multiline
    basic string so it is available verbatim to the coordinator agent.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    coord_arch = (
        f"{ROLE_DEFAULT_FIGURE.get('engineering-coordinator', 'von_neumann')}:python"
    )

    plan_draft_fields: dict[str, _TomlValue] = {"dump": plan_text}
    if label_prefix:
        plan_draft_fields["label_prefix"] = label_prefix

    sections: dict[str, dict[str, _TomlValue]] = {
        "task": {
            "version": "2.0",
            "workflow": "bugs-to-issues",
            "id": str(uuid.uuid4()),
            "created_at": now,
            "attempt_n": 0,
            "required_output": "phase_plan",
            "on_block": "stop",
        },
        "agent": {
            "role": "coordinator",
            "tier": "coordinator",
            "cognitive_arch": coord_arch,
        },
        "repo": {
            "gh_repo": repo,
            "base": "dev",
        },
        "pipeline": {
            "batch_id": slug,
            "wave": slug,
        },
        "spawn": {
            "mode": "chain",
            "sub_agents": True,
        },
        "worktree": {
            "path": str(host_worktree),
            "branch": branch,
        },
        "plan_draft": plan_draft_fields,
    }
    return _render_toml_str(sections)


def _build_conductor_task(
    wave_id: str,
    phases: list[str],
    org: str | None,
    worktree: Path,
    host_worktree: Path,
    branch: str,
) -> str:
    """Build the TOML v2 ``.agent-task`` content for a conductor worktree.

    The conductor agent reads ``task.workflow = "conductor"`` and coordinates
    across the listed phases, spawning sub-agents for each unclaimed issue.
    AgentCeption only prepares the worktree and this file — all LLM work
    happens inside the Cursor background agent that opens the returned
    ``host_worktree``.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = settings.gh_repo
    conductor_arch = (
        f"{ROLE_DEFAULT_FIGURE.get('conductor', 'jeff_dean')}:python"
    )

    target_fields: dict[str, _TomlValue] = {"phases": phases}
    if org:
        target_fields["org"] = org

    sections: dict[str, dict[str, _TomlValue]] = {
        "task": {
            "version": "2.0",
            "workflow": "conductor",
            "id": str(uuid.uuid4()),
            "created_at": now,
            "attempt_n": 0,
            "required_output": "wave_complete",
            "on_block": "stop",
        },
        "agent": {
            "role": "conductor",
            "tier": "executive",
            "cognitive_arch": conductor_arch,
        },
        "repo": {
            "gh_repo": repo,
            "base": "dev",
        },
        "pipeline": {
            "batch_id": wave_id,
            "wave": wave_id,
        },
        "spawn": {
            "mode": "chain",
            "sub_agents": True,
        },
        "target": target_fields,
        "worktree": {
            "path": str(host_worktree),
            "branch": branch,
        },
    }
    return _render_toml_str(sections)


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
