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

import datetime
import difflib
import json
import logging
from pathlib import Path
from typing import TypedDict

from agentception.models import FileEditEvent

logger = logging.getLogger(__name__)

_MEMORY_FILENAME = ".agentception/memory.json"
_DIFF_LINE_CAP = 120


class FileEditEventJSON(TypedDict):
    """JSON-safe representation of a :class:`~agentception.models.FileEditEvent`.

    Produced by ``FileEditEvent.model_dump(mode="json")``: all fields are
    plain Python scalars so the dict can be passed directly to ``json.dumps``.
    """

    timestamp: str
    path: str
    diff: str
    lines_omitted: int


class WorkingMemoryJSON(TypedDict, total=False):
    """JSON-serialisable mirror of :class:`WorkingMemory`.

    Identical shape to :class:`WorkingMemory` except ``files_written`` holds
    :class:`FileEditEventJSON` dicts instead of live :class:`FileEditEvent`
    Pydantic objects.  Returned by :func:`_memory_to_json_safe`.
    """

    plan: str
    files_written: list[FileEditEventJSON]
    files_examined: list[str]
    findings: dict[str, str]
    decisions: list[str]
    next_steps: list[str]
    blockers: list[str]


class WorkingMemory(TypedDict, total=False):
    """Structured scratch-pad the agent maintains across iterations.

    All fields are optional so the agent can update only what changed.
    """

    plan: str
    """High-level implementation plan for this session."""

    files_written: list[FileEditEvent]
    """File-edit events recorded this session — each carries a unified diff."""

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


def _deserialize_file_edit_event(raw: object) -> FileEditEvent | None:
    """Attempt to deserialize a raw dict into a :class:`FileEditEvent`.

    Returns ``None`` when the value is not a valid dict or is missing required
    fields, so callers can silently skip malformed entries.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return FileEditEvent.model_validate(raw)
    except Exception:
        return None


def _auto_track_file_write(path: str, before: str, after: str) -> FileEditEvent:
    """Compute a unified diff between *before* and *after* and return a :class:`FileEditEvent`.

    The diff is capped at ``_DIFF_LINE_CAP`` (120) visible lines so that large
    rewrites do not bloat the memory JSON.  ``lines_omitted`` carries the count
    of hidden lines so the UI can surface a "N lines omitted" notice without
    re-computing the diff.

    Pass ``before=""`` (empty string) when the file is being created for the
    first time — this produces a creation-style diff where every line appears
    as an addition (``+``).  Pass the previous file content as *before* for
    edits so the diff faithfully represents what changed.
    """
    raw_lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    visible = raw_lines[:_DIFF_LINE_CAP]
    omitted = max(0, len(raw_lines) - _DIFF_LINE_CAP)
    diff_str = "".join(visible)
    return FileEditEvent(
        timestamp=datetime.datetime.utcnow(),
        path=path,
        diff=diff_str,
        lines_omitted=omitted,
    )


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
        files_written = raw.get("files_written")
        if isinstance(files_written, list):
            events = [_deserialize_file_edit_event(f) for f in files_written]
            valid_events = [e for e in events if e is not None]
            if valid_events:
                result["files_written"] = valid_events
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


def _memory_to_json_safe(memory: WorkingMemory) -> WorkingMemoryJSON:
    """Convert a :class:`WorkingMemory` to a JSON-serialisable dict.

    ``FileEditEvent`` objects in ``files_written`` are serialised via
    ``model_dump(mode="json")`` so that ``datetime`` fields become ISO-8601
    strings rather than Python objects that ``json.dumps`` cannot handle.
    """
    result: WorkingMemoryJSON = {}
    if "plan" in memory:
        result["plan"] = memory["plan"]
    if "files_written" in memory:
        result["files_written"] = [
            FileEditEventJSON(
                timestamp=e.timestamp.isoformat(),
                path=e.path,
                diff=e.diff,
                lines_omitted=e.lines_omitted,
            )
            for e in memory["files_written"]
        ]
    if "files_examined" in memory:
        result["files_examined"] = memory["files_examined"]
    if "findings" in memory:
        result["findings"] = memory["findings"]
    if "decisions" in memory:
        result["decisions"] = memory["decisions"]
    if "next_steps" in memory:
        result["next_steps"] = memory["next_steps"]
    if "blockers" in memory:
        result["blockers"] = memory["blockers"]
    return result


def write_memory(worktree_path: Path, memory: WorkingMemory) -> None:
    """Persist *memory* to the worktree's ``.agentception/memory.json`` file.

    Creates ``.agentception/`` if it does not exist yet.  Never raises — write
    failures are logged and ignored so the agent loop is not interrupted.
    """
    path = _memory_path(worktree_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_memory_to_json_safe(memory), indent=2), encoding="utf-8"
        )
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
        if "files_written" in existing:
            result["files_written"] = existing["files_written"]
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
    if "files_written" in update:
        result["files_written"] = update["files_written"]
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

    files_written = memory.get("files_written")
    if files_written:
        files_str = ", ".join(f"`{e.path}`" for e in files_written)
        lines.append(f"**Already written this session (do NOT re-implement):** {files_str}")

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
