"""Retrofit missing / truncated activity events for historical agent runs.

Applies three back-fills in a single transaction:

1. **dir_listed** — Runs that called ``list_directory`` before the
   ``dir_listed`` activity-event type was introduced get synthetic events
   reconstructed from the raw tool results stored in ``agent_messages``.

2. **llm_reply text_preview** — Events whose ``text_preview`` was stored
   with the old 200-character limit are extended to 1 500 characters using
   the full assistant text in ``agent_messages``.

Run (idempotent — safe to re-run):

    docker compose exec agentception python3 /app/scripts/retrofit_activity_events.py

Pass ``--dry-run`` to preview counts without writing anything.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
from typing import TypedDict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.db.engine import init_db, get_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

DRY_RUN = "--dry-run" in sys.argv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ToolInvoked(TypedDict):
    event_id: int
    run_id: str
    recorded_at: datetime.datetime


class _ToolResult(TypedDict):
    seq: int
    content: str
    recorded_at: datetime.datetime


class _LlmReply(TypedDict):
    event_id: int
    run_id: str
    chars: int
    current_preview: str
    recorded_at: datetime.datetime


# ---------------------------------------------------------------------------
# 1. dir_listed back-fill
# ---------------------------------------------------------------------------

async def _backfill_dir_listed(session: AsyncSession) -> int:
    """Insert dir_listed events from stored tool results.

    Returns the number of events inserted.
    """
    # Runs that have list_directory invocations but no dir_listed events.
    needs_backfill: list[str] = [
        r[0]
        for r in (
            await session.execute(
                text("""
                    SELECT DISTINCT agent_run_id
                    FROM agent_events
                    WHERE event_type = 'activity'
                      AND (payload::jsonb)->>'subtype' = 'tool_invoked'
                      AND (payload::jsonb)->>'tool_name' = 'list_directory'
                      AND agent_run_id NOT IN (
                          SELECT DISTINCT agent_run_id
                          FROM agent_events
                          WHERE (payload::jsonb)->>'subtype' = 'dir_listed'
                      )
                    ORDER BY 1
                """)
            )
        ).fetchall()
    ]

    if not needs_backfill:
        log.info("dir_listed  — nothing to backfill")
        return 0

    log.info("dir_listed  — runs needing backfill: %s", needs_backfill)
    total_inserted = 0

    for run_id in needs_backfill:
        # tool_invoked events for list_directory, ordered by time.
        invocations: list[_ToolInvoked] = [
            {"event_id": r[0], "run_id": run_id, "recorded_at": r[1]}
            for r in (
                await session.execute(
                    text("""
                        SELECT id, recorded_at
                        FROM agent_events
                        WHERE agent_run_id = :run_id
                          AND event_type = 'activity'
                          AND (payload::jsonb)->>'subtype' = 'tool_invoked'
                          AND (payload::jsonb)->>'tool_name' = 'list_directory'
                        ORDER BY recorded_at
                    """),
                    {"run_id": run_id},
                )
            ).fetchall()
        ]

        # Tool results that contain an entries list, ordered by sequence.
        results: list[_ToolResult] = [
            {"seq": r[0], "content": r[1], "recorded_at": r[2]}
            for r in (
                await session.execute(
                    text("""
                        SELECT sequence_index, content, recorded_at
                        FROM agent_messages
                        WHERE agent_run_id = :run_id
                          AND role = 'tool'
                          AND content LIKE '%"entries"%'
                        ORDER BY sequence_index
                    """),
                    {"run_id": run_id},
                )
            ).fetchall()
        ]

        # Match invocations to results 1-to-1 in chronological order.
        pairs = list(zip(invocations, results))
        if len(invocations) != len(results):
            log.warning(
                "dir_listed  — run %s: %d invocations vs %d results — pairing what we can",
                run_id, len(invocations), len(results),
            )

        inserted = 0
        for invocation, result in pairs:
            try:
                parsed = json.loads(result["content"])
            except json.JSONDecodeError:
                log.warning("dir_listed  — run %s: unparseable result, skipping", run_id)
                continue

            if not parsed.get("ok"):
                continue

            raw_entries: object = parsed.get("entries", [])
            str_entries: list[str] = (
                [e for e in raw_entries if isinstance(e, str)]
                if isinstance(raw_entries, list)
                else []
            )

            # Use the invocation arg_preview to recover the path.
            inv_row = (
                await session.execute(
                    text("""
                        SELECT (payload::jsonb)->>'arg_preview'
                        FROM agent_events WHERE id = :eid
                    """),
                    {"eid": invocation["event_id"]},
                )
            ).fetchone()
            arg_preview = inv_row[0] if inv_row else "{}"
            try:
                import ast
                args = ast.literal_eval(arg_preview) if arg_preview else {}
                path = args.get("path", ".") if isinstance(args, dict) else "."
            except Exception:
                path = "."

            # Emit slightly after the invocation timestamp so ordering is correct.
            emit_at = result["recorded_at"]

            payload = json.dumps({
                "subtype": "dir_listed",
                "path": str(path),
                "entry_count": len(str_entries),
                "entries": "\n".join(str_entries),
            })

            if not DRY_RUN:
                await session.execute(
                    text("""
                        INSERT INTO agent_events
                            (agent_run_id, issue_number, event_type, payload, recorded_at)
                        VALUES
                            (:run_id, NULL, 'activity', :payload, :recorded_at)
                    """),
                    {"run_id": run_id, "payload": payload, "recorded_at": emit_at},
                )
            log.info(
                "dir_listed  — %s run=%s path=%s entries=%d%s",
                "[DRY]" if DRY_RUN else "[INSERT]",
                run_id, path, len(str_entries),
                "" if not DRY_RUN else " (dry-run, not written)",
            )
            inserted += 1

        total_inserted += inserted

    return total_inserted


# ---------------------------------------------------------------------------
# 2. llm_reply text_preview extension
# ---------------------------------------------------------------------------

async def _extend_llm_reply_previews(session: AsyncSession) -> int:
    """Extend llm_reply text_preview from 200 → 1500 chars where possible.

    Returns the number of events updated.
    """
    truncated: list[_LlmReply] = [
        {
            "event_id": r[0],
            "run_id": r[1],
            "chars": int(r[2]),
            "current_preview": r[3],
            "recorded_at": r[4],
        }
        for r in (
            await session.execute(
                text("""
                    SELECT id, agent_run_id,
                           (payload::jsonb)->>'chars',
                           (payload::jsonb)->>'text_preview',
                           recorded_at
                    FROM agent_events
                    WHERE event_type = 'activity'
                      AND (payload::jsonb)->>'subtype' = 'llm_reply'
                      AND ((payload::jsonb)->>'chars')::int > 200
                      AND LENGTH((payload::jsonb)->>'text_preview') <= 200
                    ORDER BY id
                """)
            )
        ).fetchall()
    ]

    if not truncated:
        log.info("llm_reply   — no truncated previews to extend")
        return 0

    updated = 0
    # Tolerance window: llm_reply is emitted right after the assistant text
    # is complete.  The agent_message is written during the same LLM call,
    # so we look ±10 seconds.
    window = datetime.timedelta(seconds=10)

    for event in truncated:
        # Find the nearest assistant text message for this run around the event time.
        row = (
            await session.execute(
                text("""
                    SELECT content
                    FROM agent_messages
                    WHERE agent_run_id = :run_id
                      AND role = 'assistant'
                      AND content IS NOT NULL
                      AND LENGTH(content) > 0
                      AND recorded_at BETWEEN :t_lo AND :t_hi
                    ORDER BY ABS(EXTRACT(EPOCH FROM (recorded_at - :t_mid)))
                    LIMIT 1
                """),
                {
                    "run_id": event["run_id"],
                    "t_lo": event["recorded_at"] - window,
                    "t_hi": event["recorded_at"] + window,
                    "t_mid": event["recorded_at"],
                },
            )
        ).fetchone()

        if row is None or not row[0]:
            log.warning(
                "llm_reply   — event %d run=%s: no matching assistant message found",
                event["event_id"], event["run_id"],
            )
            continue

        full_text: str = row[0]
        new_preview = full_text[:1500]

        # Skip if we don't actually gain anything.
        if new_preview == event["current_preview"]:
            continue

        new_payload = json.dumps({
            "subtype": "llm_reply",
            "chars": event["chars"],
            "text_preview": new_preview,
        })

        if not DRY_RUN:
            await session.execute(
                text("UPDATE agent_events SET payload = :p WHERE id = :eid"),
                {"p": new_payload, "eid": event["event_id"]},
            )
        log.info(
            "llm_reply   — %s event=%d run=%s preview %d→%d chars%s",
            "[DRY]" if DRY_RUN else "[UPDATE]",
            event["event_id"], event["run_id"],
            len(event["current_preview"]), len(new_preview),
            "" if not DRY_RUN else " (dry-run, not written)",
        )
        updated += 1

    return updated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    await init_db()
    async with get_session() as session:
        log.info("=== Activity-event retrofit  (dry_run=%s) ===", DRY_RUN)

        dir_listed_count = await _backfill_dir_listed(session)
        llm_reply_count  = await _extend_llm_reply_previews(session)

        if not DRY_RUN:
            await session.commit()
            log.info(
                "=== Done — dir_listed inserted: %d  llm_reply updated: %d ===",
                dir_listed_count, llm_reply_count,
            )
        else:
            log.info(
                "=== Dry-run complete — would insert: %d dir_listed  update: %d llm_reply ===",
                dir_listed_count, llm_reply_count,
            )


if __name__ == "__main__":
    asyncio.run(main())
