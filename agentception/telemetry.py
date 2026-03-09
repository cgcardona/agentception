from __future__ import annotations

"""Wave aggregation layer for the AgentCeption telemetry pipeline.

Groups all ``.agent-task`` files by their ``BATCH_ID`` prefix and builds
``WaveSummary`` objects from filesystem signals.  File mtimes serve as proxy
timestamps because agents write the task file at worktree creation time and
update it on state changes — no separate log file is required.

When no ``.agent-task`` files exist (all worktrees cleaned up), falls back
to ``ac_agent_runs`` rows from Postgres so the telemetry charts always have
data to display.

Consumed by ``GET /api/telemetry/waves`` and future timeline UI components.
"""

import asyncio
import logging
import os
from pathlib import Path

from pydantic import BaseModel

from agentception.config import settings
from agentception.db.queries import RunContextRow, list_active_runs
from agentception.models import AgentNode, AgentStatus

logger = logging.getLogger(__name__)

# Claude Sonnet 4.6 pricing (per million tokens, as of 2026).
# Exported so tests and the API layer can reference them without duplication.
SONNET_INPUT_PER_M: float = 3.0
SONNET_OUTPUT_PER_M: float = 15.0

# Conservative proxy: transcripts carry no real token counts, so we estimate
# from message volume.  40 % of traffic is treated as input (prompts/context),
# 60 % as output (model completions).
AVG_TOKENS_PER_MSG: int = 800
AVG_INPUT_RATIO: float = 0.4


def estimate_cost(message_count: int) -> tuple[int, float]:
    """Return ``(estimated_tokens, estimated_cost_usd)`` for a given message count.

    Uses ``AVG_TOKENS_PER_MSG`` as a per-message proxy and Claude Sonnet 4.6
    pricing constants.  The input/output split follows ``AVG_INPUT_RATIO``.

    Returns ``(0, 0.0)`` when ``message_count`` is zero so callers can treat
    the zero case uniformly without special-casing.
    """
    tokens = message_count * AVG_TOKENS_PER_MSG
    input_tokens = int(tokens * AVG_INPUT_RATIO)
    output_tokens = tokens - input_tokens
    cost = (
        input_tokens / 1_000_000 * SONNET_INPUT_PER_M
        + output_tokens / 1_000_000 * SONNET_OUTPUT_PER_M
    )
    return tokens, round(cost, 4)


class WaveSummary(BaseModel):
    """Aggregated telemetry for one VP/Engineering batch wave.

    A *wave* is the set of all agents that share the same ``BATCH_ID`` prefix
    (e.g. ``eng-20260301T203044Z-4161``).  ``started_at`` / ``ended_at`` are
    UNIX timestamps derived from ``.agent-task`` file mtimes — they are
    approximations, not wall-clock measurements.

    ``ended_at`` is ``None`` when at least one worktree in the batch is still
    active (i.e. its worktree directory still exists on disk).
    """

    batch_id: str
    started_at: float
    ended_at: float | None
    issues_worked: list[int]
    prs_opened: int
    prs_merged: int
    estimated_tokens: int
    estimated_cost_usd: float
    agents: list[AgentNode]


async def aggregate_waves() -> list[WaveSummary]:
    """Return WaveSummary objects, preferring filesystem data, falling back to DB.

    Primary source: ``.agent-task`` files in live worktrees (filesystem state).
    Fallback: ``ac_agent_runs`` rows from Postgres, used when all worktrees have
    been pruned so that the telemetry charts always show historical data.

    Returns a list sorted by ``started_at`` descending (most recent wave first).
    """
    active_runs = await list_active_runs()
    fs_summaries = _build_wave_summaries(active_runs)
    if fs_summaries:
        return fs_summaries

    # No live worktrees — reconstruct wave summaries from DB records.
    try:
        from agentception.db.queries import get_waves_from_db

        db_waves = await get_waves_from_db(limit=100)
        summaries: list[WaveSummary] = []
        for w in db_waves:
            agents = [
                AgentNode(
                    id=a["id"],
                    role=a["role"],
                    status=AgentStatus(a["status"]) if a["status"] in AgentStatus._value2member_map_ else AgentStatus.FAILED,
                    issue_number=a["issue_number"],
                    pr_number=a["pr_number"],
                    branch=a["branch"],
                    batch_id=a["batch_id"],
                    worktree_path=a["worktree_path"],
                    cognitive_arch=a["cognitive_arch"],
                )
                for a in w["agents"]
            ]
            total_msgs = sum(a.message_count for a in agents)
            tokens, cost = estimate_cost(total_msgs)
            summaries.append(
                WaveSummary(
                    batch_id=w["batch_id"],
                    started_at=w["started_at"],
                    ended_at=w["ended_at"],
                    issues_worked=w["issues_worked"],
                    prs_opened=w["prs_opened"],
                    prs_merged=w["prs_merged"],
                    estimated_tokens=tokens,
                    estimated_cost_usd=cost,
                    agents=agents,
                )
            )
        return summaries
    except Exception as exc:
        logger.warning("⚠️  aggregate_waves DB fallback failed (non-fatal): %s", exc)
        return []


async def compute_wave_timing(runs: list[RunContextRow]) -> tuple[float, float | None]:
    """Return ``(started_at, ended_at)`` from DB ``spawned_at`` timestamps.

    ``started_at`` is the earliest ``spawned_at`` in the group (UNIX timestamp).
    ``ended_at`` is ``None`` if any worktree path still exists (agent active),
    otherwise the latest ``spawned_at`` as a proxy for when the wave ended.

    Returns ``(0.0, None)`` when the list is empty.
    """
    import datetime as _dt

    if not runs:
        return 0.0, None

    timestamps: list[float] = []
    any_still_active = False

    for run in runs:
        try:
            ts = _dt.datetime.fromisoformat(run["spawned_at"]).timestamp()
            timestamps.append(ts)
        except (ValueError, KeyError):
            pass
        worktree = run["worktree_path"]
        if worktree and Path(worktree).exists():
            any_still_active = True

    if not timestamps:
        return 0.0, None

    started_at = min(timestamps)
    ended_at = None if any_still_active else max(timestamps)
    return started_at, ended_at


# ── Private helpers ────────────────────────────────────────────────────────────


def _build_wave_summaries(active_runs: list[RunContextRow]) -> list[WaveSummary]:
    """Group DB run rows by batch_id and produce WaveSummary objects."""
    import datetime as _dt

    groups: dict[str, list[RunContextRow]] = {}
    for run in active_runs:
        bid = run["batch_id"]
        if not bid:
            continue
        groups.setdefault(bid, []).append(run)

    summaries: list[WaveSummary] = []
    for batch_id, members in groups.items():
        timestamps: list[float] = []
        any_still_active = False
        issues_worked: list[int] = []
        prs_opened = 0

        for run in members:
            issue_num = run["issue_number"]
            if issue_num is not None and issue_num not in issues_worked:
                issues_worked.append(issue_num)
            if run["pr_number"] is not None:
                prs_opened += 1
            try:
                ts = _dt.datetime.fromisoformat(run["spawned_at"]).timestamp()
                timestamps.append(ts)
            except (ValueError, KeyError):
                pass
            worktree = run["worktree_path"]
            if worktree and Path(worktree).exists():
                any_still_active = True

        started_at = min(timestamps) if timestamps else 0.0
        ended_at = None if any_still_active else (max(timestamps) if timestamps else None)
        agents = [_run_to_agent_node(run) for run in members]
        total_message_count = sum(a.message_count for a in agents)
        estimated_tokens, estimated_cost_usd = estimate_cost(total_message_count)

        summaries.append(
            WaveSummary(
                batch_id=batch_id,
                started_at=started_at,
                ended_at=ended_at,
                issues_worked=sorted(issues_worked),
                prs_opened=prs_opened,
                prs_merged=0,
                estimated_tokens=estimated_tokens,
                estimated_cost_usd=round(estimated_cost_usd, 4),
                agents=agents,
            )
        )

    summaries.sort(key=lambda s: s.started_at, reverse=True)
    return summaries


def _stat_mtime(path: Path) -> float | None:
    """Return file mtime as a float, or None on OS error."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


async def _get_mtime(path: Path) -> float:
    """Async wrapper around ``os.stat`` for mtime — raises OSError on failure."""
    loop = asyncio.get_running_loop()
    stat_result = await loop.run_in_executor(None, os.stat, path)
    return stat_result.st_mtime


def _run_to_agent_node(run: RunContextRow) -> AgentNode:
    """Convert a DB ``RunContextRow`` to a minimal ``AgentNode`` for wave summaries."""
    worktree = run["worktree_path"]
    agent_id = worktree or f"agent-{run['issue_number'] or 'unknown'}"
    is_active = Path(worktree).exists() if worktree else False
    return AgentNode(
        id=agent_id,
        role=run["role"] or "unknown",
        status=AgentStatus.IMPLEMENTING if is_active else AgentStatus.COMPLETED,
        issue_number=run["issue_number"],
        pr_number=run["pr_number"],
        branch=run["branch"],
        batch_id=run["batch_id"],
        worktree_path=worktree,
        cognitive_arch=run["cognitive_arch"],
    )
