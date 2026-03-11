"""Cost report for AgentCeption runs.

Queries the database for real token usage (input, output, cache-write,
cache-read) accumulated during agent runs and computes the exact USD cost
using Anthropic's published pricing for claude-sonnet-4-6.

Usage (from inside the container):
    python tools/cost.py                    # last 20 completed runs
    python tools/cost.py --last 50          # last N runs
    python tools/cost.py --run-id issue-537 # one specific run
    python tools/cost.py --all              # every run in the DB

Pricing reference (claude-sonnet-4-6, as of 2026-03):
    Input tokens:       $3.00 / MTok
    Output tokens:     $15.00 / MTok
    Cache write:        $3.75 / MTok  (Turn 1 — written to Anthropic cache)
    Cache read:         $0.30 / MTok  (Turns 2-N — read from cache)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from agentception.db.engine import get_session, init_db
from agentception.db.models import ACAgentRun

# ---------------------------------------------------------------------------
# Pricing — claude-sonnet-4-6 (USD per million tokens)
# ---------------------------------------------------------------------------

_INPUT_PER_M: float = 3.00
_OUTPUT_PER_M: float = 15.00
_CACHE_WRITE_PER_M: float = 3.75
_CACHE_READ_PER_M: float = 0.30


def _cost(
    input_tokens: int,
    output_tokens: int,
    cache_write: int,
    cache_read: int,
) -> float:
    """Return total USD cost for a single run's accumulated token counts."""
    return (
        input_tokens / 1_000_000 * _INPUT_PER_M
        + output_tokens / 1_000_000 * _OUTPUT_PER_M
        + cache_write / 1_000_000 * _CACHE_WRITE_PER_M
        + cache_read / 1_000_000 * _CACHE_READ_PER_M
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_cost(usd: float) -> str:
    if usd < 0.001:
        return f"${usd*1000:.3f}m"  # millicents-ish
    return f"${usd:.4f}"


async def _report(run_id: str | None, last: int, all_runs: bool) -> None:
    await init_db()

    async with get_session() as session:
        stmt = select(ACAgentRun).order_by(ACAgentRun.spawned_at.desc())
        if run_id:
            stmt = stmt.where(ACAgentRun.id == run_id)
        elif not all_runs:
            stmt = stmt.limit(last)

        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        print("No runs found.")
        return

    # Header
    col_widths = (28, 12, 8, 8, 8, 8, 10, 8)
    headers = ("run_id", "role", "input", "output", "cw", "cr", "total_tok", "cost")
    sep = "  ".join("-" * w for w in col_widths)
    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)

    print()
    print(f"  claude-sonnet-4-6 pricing: input ${_INPUT_PER_M}/M  output ${_OUTPUT_PER_M}/M  "
          f"cache-write ${_CACHE_WRITE_PER_M}/M  cache-read ${_CACHE_READ_PER_M}/M")
    print()
    print(fmt.format(*headers))
    print(sep)

    total_input = total_output = total_cw = total_cr = 0
    total_usd = 0.0

    for run in reversed(rows):
        inp = run.total_input_tokens
        out = run.total_output_tokens
        cw = run.total_cache_write_tokens
        cr = run.total_cache_read_tokens
        total_tok = inp + out
        usd = _cost(inp, out, cw, cr)

        total_input += inp
        total_output += out
        total_cw += cw
        total_cr += cr
        total_usd += usd

        print(fmt.format(
            run.id[:28],
            (run.role or "")[:12],
            _fmt_tokens(inp),
            _fmt_tokens(out),
            _fmt_tokens(cw),
            _fmt_tokens(cr),
            _fmt_tokens(total_tok),
            _fmt_cost(usd),
        ))

    print(sep)
    grand_total = total_input + total_output
    print(fmt.format(
        f"TOTAL ({len(rows)} runs)",
        "",
        _fmt_tokens(total_input),
        _fmt_tokens(total_output),
        _fmt_tokens(total_cw),
        _fmt_tokens(total_cr),
        _fmt_tokens(grand_total),
        _fmt_cost(total_usd),
    ))

    # Cache efficiency summary
    if total_input > 0:
        cache_hit_pct = total_cr / total_input * 100
        cache_savings = total_cr / 1_000_000 * (_INPUT_PER_M - _CACHE_READ_PER_M)
        print()
        print(f"  Cache hit rate : {cache_hit_pct:.1f}% of input tokens read from cache")
        print(f"  Cache savings  : {_fmt_cost(cache_savings)} vs full input pricing")
        print(f"  Effective rate : {_fmt_cost(total_usd / max(grand_total, 1) * 1_000_000)}/MTok blended")
    print()

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"  Report generated: {now_utc}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", help="Show cost for a specific run ID")
    parser.add_argument("--last", type=int, default=20, help="Show last N runs (default: 20)")
    parser.add_argument("--all", dest="all_runs", action="store_true", help="Show all runs")
    args = parser.parse_args()

    asyncio.run(_report(run_id=args.run_id, last=args.last, all_runs=args.all_runs))


if __name__ == "__main__":
    main()
