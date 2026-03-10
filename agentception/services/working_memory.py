"""Persistent working memory for agent runs.

A lightweight JSON file stored at ``.agentception/memory.json`` inside the
worktree — alongside the repo's role files and prompt templates, so all
AgentCeption runtime state is co-located under the project's canonical config
directory rather than a one-off ``.ac/`` prefix.

The agent loop reads it at the start of every iteration and injects a compact
rendering as a secondary system block so the model always has its current state
— without touching the prunable conversation history.

Agents update their memory via the ``update_working_memory`` tool.  Supplied
fields are merged into the stored JSON; omitted fields are preserved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

_MEMORY_FILENAME = ".agentception/memory.json"


class WorkingMemory(TypedDict, total=False):
    """Structured scratch-pad the agent maintains across iterations.

    All fields are optional so the agent can update only what changed.
    """

    plan: str
    """High-level implementation plan for this session."""

    files_examined: list[str]
    """Paths of files already read or inspected — skip re-reading these."""

    findings: dict[str, str]
    """Per-file or per-topic findings: key = file/topic, value = short note."""

    decisions: list[str]
    """Architecture or approach decisions already locked in."""

    next_steps: list[str]
    """Ordered queue of remaining work items."""

    blockers: list[str]
    """Anything blocking progress (unclear spec, awaiting result, etc.)."""


def _memory_path(worktree_path: Path) -> Path:
    return worktree_path / _MEMORY_FILENAME


def read_memory(worktree_path: Path) -> WorkingMemory | None:
    """Load the agent's working memory file.

    Returns ``None`` when the file does not exist or is corrupt.
    Never raises — memory failure degrades gracefully to an empty context.
    """
    path = _memory_path(worktree_path)
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        result = WorkingMemory()
        plan = raw.get("plan")
        if isinstance(plan, str):
            result["plan"] = plan
        files_examined = raw.get("files_examined")
        if (
            isinstance(files_examined, list)
            and all(isinstance(f, str) for f in files_examined)
        ):
            result["files_examined"] = list(files_examined)
        findings = raw.get("findings")
        if isinstance(findings, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in findings.items()
        ):
            result["findings"] = dict(findings)
        decisions = raw.get("decisions")
        if isinstance(decisions, list) and all(isinstance(d, str) for d in decisions):
            result["decisions"] = list(decisions)
        next_steps = raw.get("next_steps")
        if isinstance(next_steps, list) and all(isinstance(s, str) for s in next_steps):
            result["next_steps"] = list(next_steps)
        blockers = raw.get("blockers")
        if isinstance(blockers, list) and all(isinstance(b, str) for b in blockers):
            result["blockers"] = list(blockers)
        return result or None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("⚠️ working_memory.read — error: %s", exc)
        return None


def write_memory(worktree_path: Path, memory: WorkingMemory) -> None:
    """Persist *memory* to the worktree's ``.ac/memory.json`` file.

    Creates ``.ac/`` if it does not exist yet.  Never raises — write failures
    are logged and ignored so the agent loop is not interrupted.
    """
    path = _memory_path(worktree_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(memory, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("⚠️ working_memory.write — error: %s", exc)


def merge_memory(existing: WorkingMemory | None, update: WorkingMemory) -> WorkingMemory:
    """Merge *update* fields into *existing*, returning a new :class:`WorkingMemory`.

    Only fields present in *update* are changed; absent fields from *existing*
    are preserved.  ``findings`` dicts are union-merged so individual keys can
    be added or updated without resending the entire dict.
    """
    result = WorkingMemory()

    if existing is not None:
        if "plan" in existing:
            result["plan"] = existing["plan"]
        if "files_examined" in existing:
            result["files_examined"] = existing["files_examined"]
        if "findings" in existing:
            result["findings"] = existing["findings"]
        if "decisions" in existing:
            result["decisions"] = existing["decisions"]
        if "next_steps" in existing:
            result["next_steps"] = existing["next_steps"]
        if "blockers" in existing:
            result["blockers"] = existing["blockers"]

    if "plan" in update:
        result["plan"] = update["plan"]
    if "files_examined" in update:
        result["files_examined"] = update["files_examined"]
    if "findings" in update:
        prior: dict[str, str] = result.get("findings", {})
        result["findings"] = {**prior, **update["findings"]}
    if "decisions" in update:
        result["decisions"] = update["decisions"]
    if "next_steps" in update:
        result["next_steps"] = update["next_steps"]
    if "blockers" in update:
        result["blockers"] = update["blockers"]

    return result


def render_memory(memory: WorkingMemory) -> str:
    """Render *memory* as compact Markdown for injection into the system prompt.

    The rendering is intentionally terse — one line per item — to minimise the
    token cost while keeping the model oriented.

    Order: findings first (type signatures + existing tests are facts the agent
    needs before writing any code), then plan, then files examined, then
    decisions/next_steps/blockers.  Putting facts before the task description
    means the agent reads the constraints before it starts planning, which
    eliminates the mypy-fix loop and test-collision discovery turns.
    """
    lines: list[str] = ["## Working Memory"]

    findings = memory.get("findings")
    if findings:
        lines.append("**Findings (read before writing any code):**")
        for key, note in findings.items():
            lines.append(f"- `{key}`: {note}")

    plan = memory.get("plan")
    if plan:
        lines.append(f"**Plan:** {plan}")

    files_examined = memory.get("files_examined")
    if files_examined:
        files_str = ", ".join(f"`{f}`" for f in files_examined)
        lines.append(f"**Files read (skip re-reading):** {files_str}")

    decisions = memory.get("decisions")
    if decisions:
        lines.append("**Decisions:**")
        for d in decisions:
            lines.append(f"- {d}")

    next_steps = memory.get("next_steps")
    if next_steps:
        lines.append("**Next steps:**")
        for i, s in enumerate(next_steps, 1):
            lines.append(f"{i}. {s}")

    blockers = memory.get("blockers")
    if blockers:
        lines.append("**Blockers:**")
        for b in blockers:
            lines.append(f"- ⚠️ {b}")

    return "\n".join(lines)
