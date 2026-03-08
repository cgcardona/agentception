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
    """Parse a TOML ``.agent-task`` file into a TaskFile model.

    Returns ``None`` when the file is absent, unreadable, or so malformed
    that no valid TaskFile can be constructed. Malformed TOML is logged as
    a warning and returns ``None`` — it never propagates an exception.
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
        data: dict[str, object] = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("⚠️  TOML parse error in %s: %s", task_file_path, exc)
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


def _sec(data: dict[str, object], key: str) -> dict[str, object]:
    """Safely extract a TOML table (section) by key.

    Returns an empty dict when the key is absent or its value is not a table.
    """
    val = data.get(key)
    if isinstance(val, dict):
        return val
    return {}


def _str_val(sec: dict[str, object], key: str) -> str | None:
    """Return a string field from a TOML section, or None if absent/wrong type."""
    val = sec.get(key)
    return val if isinstance(val, str) else None


def _int_val(sec: dict[str, object], key: str) -> int | None:
    """Return an integer field from a TOML section, or None if absent/wrong type.

    Explicitly excludes booleans since ``bool`` is a subclass of ``int`` in Python.
    """
    val = sec.get(key)
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return None


def _bool_val(sec: dict[str, object], key: str, default: bool = False) -> bool:
    """Return a boolean field from a TOML section, falling back to ``default``."""
    val = sec.get(key)
    if isinstance(val, bool):
        return val
    return default


def _int_list_val(sec: dict[str, object], key: str) -> list[int]:
    """Return a list-of-int field from a TOML section, skipping non-int items."""
    val = sec.get(key)
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, int) and not isinstance(item, bool)]


def _str_list_val(sec: dict[str, object], key: str) -> list[str]:
    """Return a list-of-str field from a TOML section, skipping non-string items."""
    val = sec.get(key)
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, str)]


def _build_task_file_from_toml(data: dict[str, object], worktree_path: Path) -> TaskFile:
    """Map TOML ``.agent-task`` sections to ``TaskFile`` fields.

    Section → field mapping follows ``.agentception/agent-task-spec.md`` v2.0.
    Unknown fields are silently ignored; all TaskFile fields are optional so
    partially-populated files yield a valid model.
    """
    task_sec = _sec(data, "task")
    agent_sec = _sec(data, "agent")
    repo_sec = _sec(data, "repo")
    pipeline_sec = _sec(data, "pipeline")
    spawn_sec = _sec(data, "spawn")
    target_sec = _sec(data, "target")
    worktree_sec = _sec(data, "worktree")
    output_sec = _sec(data, "output")
    domain_sec = _sec(data, "domain")

    issue_queue: list[IssueSub] = []
    raw_issue_queue = data.get("issue_queue")
    if isinstance(raw_issue_queue, list):
        for item in raw_issue_queue:
            if isinstance(item, dict):
                try:
                    issue_queue.append(IssueSub.model_validate(item))
                except Exception:
                    pass

    pr_queue: list[PRSub] = []
    raw_pr_queue = data.get("pr_queue")
    if isinstance(raw_pr_queue, list):
        for item in raw_pr_queue:
            if isinstance(item, dict):
                try:
                    pr_queue.append(PRSub.model_validate(item))
                except Exception:
                    pass

    return TaskFile(
        task=_str_val(task_sec, "workflow"),
        id=_str_val(task_sec, "id"),
        attempt_n=_int_val(task_sec, "attempt_n") or 0,
        is_resumed=_bool_val(task_sec, "is_resumed", default=False),
        required_output=_str_val(task_sec, "required_output"),
        on_block=_str_val(task_sec, "on_block"),
        role=_str_val(agent_sec, "role"),
        tier=_str_val(agent_sec, "tier"),
        org_domain=_str_val(agent_sec, "org_domain"),
        cognitive_arch=_str_val(agent_sec, "cognitive_arch"),
        session_id=_str_val(agent_sec, "session_id"),
        gh_repo=_str_val(repo_sec, "gh_repo"),
        base=_str_val(repo_sec, "base"),
        batch_id=_str_val(pipeline_sec, "batch_id"),
        parent_run_id=_str_val(pipeline_sec, "parent_run_id"),
        wave=_str_val(pipeline_sec, "wave"),
        vp_fingerprint=_str_val(pipeline_sec, "vp_fingerprint"),
        spawn_sub_agents=_bool_val(spawn_sec, "sub_agents"),
        spawn_mode=_str_val(spawn_sec, "mode"),
        issue_number=_int_val(target_sec, "issue_number"),
        pr_number=_int_val(target_sec, "pr_number"),
        depends_on=_int_list_val(target_sec, "depends_on"),
        closes_issues=_int_list_val(target_sec, "closes"),
        file_ownership=_str_list_val(target_sec, "file_ownership"),
        worktree=_str_val(worktree_sec, "path") or str(worktree_path),
        branch=_str_val(worktree_sec, "branch"),
        linked_pr=_int_val(worktree_sec, "linked_pr"),
        draft_id=_str_val(output_sec, "draft_id"),
        output_path=_str_val(output_sec, "path"),
        domain=_str_val(domain_sec, "name"),
        issue_queue=issue_queue,
        pr_queue=pr_queue,
    )
