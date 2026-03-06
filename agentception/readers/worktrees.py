from __future__ import annotations

"""Worktree reader for AgentCeption.

Scans ``~/.agentception/worktrees/agentception/`` for active git worktrees and parses
the ``.agent-task`` file in each one to derive live agent metadata.

This is the primary filesystem signal for the poller — it tells the dashboard
which agents are actively working and what they are working on. Combine with
transcript data for richer status information.
"""

import asyncio
import logging
import tomllib
from pathlib import Path

from agentception.config import settings
from agentception.models import IssueSub, PRSub, TaskFile

logger = logging.getLogger(__name__)


async def list_active_worktrees() -> list[TaskFile]:
    """Scan the worktrees directory and return one TaskFile per active checkout.

    A worktree is considered active if it contains a ``.agent-task`` file.
    Directories without that file are silently skipped (they may be stale
    or manually created). Returns an empty list when the worktrees directory
    does not exist.
    """
    worktrees_dir: Path = settings.worktrees_dir
    if not worktrees_dir.exists():
        logger.debug("⚠️  Worktrees dir does not exist: %s", worktrees_dir)
        return []

    results: list[TaskFile] = []
    try:
        entries = list(worktrees_dir.iterdir())
    except OSError as exc:
        logger.warning("⚠️  Cannot read worktrees dir %s: %s", worktrees_dir, exc)
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        task = await parse_agent_task(entry)
        if task is not None:
            results.append(task)

    logger.debug("✅ Found %d active worktree(s)", len(results))
    return results


async def parse_agent_task(worktree_path: Path) -> TaskFile | None:
    """Parse a TOML v2 ``.agent-task`` file into a TaskFile model.

    Returns ``None`` when the file is absent, unreadable, or malformed TOML.
    Uses ``tomllib.loads()`` exclusively — the legacy KEY=VALUE format is no
    longer supported.
    """
    task_file_path = worktree_path / ".agent-task"
    if not task_file_path.exists():
        return None

    try:
        content = await asyncio.get_running_loop().run_in_executor(
            None, task_file_path.read_text, "utf-8"
        )
    except OSError as exc:
        logger.warning("⚠️  Cannot read %s: %s", task_file_path, exc)
        return None

    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("⚠️  Malformed TOML in %s: %s", task_file_path, exc)
        return None

    try:
        return _build_task_file_from_toml(data, worktree_path)
    except Exception as exc:
        logger.warning("⚠️  Failed to build TaskFile from %s: %s", task_file_path, exc)
        return None


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


# ── Private helpers ────────────────────────────────────────────────────────────


def _section(data: dict[str, object], key: str) -> dict[str, object]:
    """Extract a TOML section by key, returning an empty dict if absent or wrong type.

    After ``tomllib.loads()``, all section values are dicts with string keys.
    We rebuild explicitly to give mypy a concrete ``dict[str, object]`` type.
    """
    val = data.get(key)
    if not isinstance(val, dict):
        return {}
    out: dict[str, object] = {}
    for k, v in val.items():
        if isinstance(k, str):
            out[k] = v
    return out


def _opt_str(val: object) -> str | None:
    """Return the value as str if it is a string, otherwise None."""
    return val if isinstance(val, str) else None


def _opt_int(val: object) -> int | None:
    """Return the value as int if it is an int (but not bool), otherwise None."""
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return None


def _int_list(val: object) -> list[int]:
    """Return a list of ints extracted from a TOML array, skipping non-ints."""
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, int) and not isinstance(item, bool)]


def _str_list(val: object) -> list[str]:
    """Return a list of strings extracted from a TOML array, skipping non-strings."""
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, str)]


def _build_task_file_from_toml(data: dict[str, object], worktree_path: Path) -> TaskFile:
    """Map a parsed TOML v2 .agent-task dict to a TaskFile model.

    Each TOML section is extracted via ``_section()`` and individual fields
    are narrowed with type-safe helpers.  Unknown keys are silently ignored;
    missing optional sections produce empty dicts.  The ``worktree.path``
    field takes precedence over the filesystem path passed in when present.
    """
    task = _section(data, "task")
    agent = _section(data, "agent")
    repo = _section(data, "repo")
    pipeline = _section(data, "pipeline")
    spawn = _section(data, "spawn")
    target = _section(data, "target")
    worktree_sec = _section(data, "worktree")
    output = _section(data, "output")
    domain = _section(data, "domain")

    # [[issue_queue]] and [[pr_queue]] are arrays of inline tables.
    raw_issue_queue = data.get("issue_queue", [])
    raw_pr_queue = data.get("pr_queue", [])

    issue_queue: list[IssueSub] = []
    if isinstance(raw_issue_queue, list):
        for item in raw_issue_queue:
            if isinstance(item, dict):
                item_dict: dict[str, object] = {}
                for k, v in item.items():
                    if isinstance(k, str):
                        item_dict[k] = v
                try:
                    issue_queue.append(IssueSub.model_validate(item_dict))
                except Exception as exc:
                    logger.debug("⚠️  Skipping malformed issue_queue entry: %s", exc)

    pr_queue: list[PRSub] = []
    if isinstance(raw_pr_queue, list):
        for item in raw_pr_queue:
            if isinstance(item, dict):
                item_dict2: dict[str, object] = {}
                for k, v in item.items():
                    if isinstance(k, str):
                        item_dict2[k] = v
                try:
                    pr_queue.append(PRSub.model_validate(item_dict2))
                except Exception as exc:
                    logger.debug("⚠️  Skipping malformed pr_queue entry: %s", exc)

    # [worktree].path overrides the filesystem path when present.
    worktree_path_val = worktree_sec.get("path")
    worktree_str = (
        worktree_path_val
        if isinstance(worktree_path_val, str)
        else str(worktree_path)
    )

    # linked_pr is 0 or a PR number written back by the agent.
    linked_pr_val = worktree_sec.get("linked_pr")
    linked_pr: int | None = _opt_int(linked_pr_val)

    spawn_sub_agents_val = spawn.get("sub_agents")
    spawn_sub_agents = (
        bool(spawn_sub_agents_val) if isinstance(spawn_sub_agents_val, bool) else False
    )

    attempt_n_val = task.get("attempt_n")
    attempt_n = _opt_int(attempt_n_val) or 0

    return TaskFile(
        task=_opt_str(task.get("workflow")),
        id=_opt_str(task.get("id")),
        attempt_n=attempt_n,
        required_output=_opt_str(task.get("required_output")),
        on_block=_opt_str(task.get("on_block")),
        role=_opt_str(agent.get("role")),
        tier=_opt_str(agent.get("tier")),
        org_domain=_opt_str(agent.get("org_domain")),
        cognitive_arch=_opt_str(agent.get("cognitive_arch")),
        session_id=_opt_str(agent.get("session_id")),
        gh_repo=_opt_str(repo.get("gh_repo")),
        base=_opt_str(repo.get("base")),
        batch_id=_opt_str(pipeline.get("batch_id")),
        parent_run_id=_opt_str(pipeline.get("parent_run_id")),
        wave=_opt_str(pipeline.get("wave")),
        spawn_sub_agents=spawn_sub_agents,
        spawn_mode=_opt_str(spawn.get("mode")),
        issue_number=_opt_int(target.get("issue_number")),
        pr_number=_opt_int(target.get("pr_number")),
        depends_on=_int_list(target.get("depends_on")),
        closes_issues=_int_list(target.get("closes")),
        file_ownership=_str_list(target.get("file_ownership")),
        worktree=worktree_str,
        branch=_opt_str(worktree_sec.get("branch")),
        linked_pr=linked_pr,
        draft_id=_opt_str(output.get("draft_id")),
        output_path=_opt_str(output.get("path")),
        domain=_opt_str(domain.get("name")),
        issue_queue=issue_queue,
        pr_queue=pr_queue,
    )
