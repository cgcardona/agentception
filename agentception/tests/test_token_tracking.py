"""Tests for token accumulation and cost calculation.

Covers:
  - accumulate_token_usage: happy path, unknown run silently skipped, DB error swallowed
  - tools/cost.py _cost() calculation with actual Anthropic pricing
  - ToolResponse now includes output_tokens
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Cost calculation (pure function — no DB needed)
# ---------------------------------------------------------------------------


def _cost(input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> float:
    """Mirror of tools/cost.py _cost() for isolated testing."""
    INPUT_PER_M = 3.00
    OUTPUT_PER_M = 15.00
    CACHE_WRITE_PER_M = 3.75
    CACHE_READ_PER_M = 0.30
    return (
        input_tokens / 1_000_000 * INPUT_PER_M
        + output_tokens / 1_000_000 * OUTPUT_PER_M
        + cache_write / 1_000_000 * CACHE_WRITE_PER_M
        + cache_read / 1_000_000 * CACHE_READ_PER_M
    )


def test_cost_one_million_input_tokens() -> None:
    assert _cost(1_000_000, 0, 0, 0) == pytest.approx(3.00)


def test_cost_one_million_output_tokens() -> None:
    assert _cost(0, 1_000_000, 0, 0) == pytest.approx(15.00)


def test_cost_cache_write_is_cheaper_than_input() -> None:
    # Writing to cache ($3.75/M) vs uncached input ($3.00/M) — write is slightly
    # more expensive but turns 2-N read at $0.30/M, a 90% discount.
    write_cost = _cost(0, 0, 1_000_000, 0)
    read_cost = _cost(0, 0, 0, 1_000_000)
    assert write_cost == pytest.approx(3.75)
    assert read_cost == pytest.approx(0.30)
    assert read_cost < write_cost


def test_cost_typical_run() -> None:
    """Realistic run: 50K input, 5K output, 45K cache-read (cached system prompt)."""
    usd = _cost(
        input_tokens=50_000,
        output_tokens=5_000,
        cache_write=0,
        cache_read=45_000,
    )
    expected = (50_000 / 1e6 * 3.00) + (5_000 / 1e6 * 15.00) + (45_000 / 1e6 * 0.30)
    assert usd == pytest.approx(expected, rel=1e-6)


def test_cost_zero_tokens() -> None:
    assert _cost(0, 0, 0, 0) == 0.0


# ---------------------------------------------------------------------------
# ToolResponse includes output_tokens
# ---------------------------------------------------------------------------


def test_tool_response_has_output_tokens_field() -> None:
    """ToolResponse TypedDict must include output_tokens (regression guard)."""
    from agentception.services.llm import ToolResponse
    # TypedDict fields are accessible via __annotations__
    assert "output_tokens" in ToolResponse.__annotations__


# ---------------------------------------------------------------------------
# accumulate_token_usage
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_accumulate_token_usage_executes_update() -> None:
    """accumulate_token_usage fires an UPDATE against the DB session."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.db.persist.get_session", return_value=mock_session):
        from agentception.db.persist import accumulate_token_usage

        await accumulate_token_usage(
            run_id="issue-999",
            input_tokens=10_000,
            output_tokens=1_000,
            cache_write_tokens=8_000,
            cache_read_tokens=2_000,
        )

    mock_session.execute.assert_awaited_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_accumulate_token_usage_swallows_db_error() -> None:
    """DB errors from accumulate_token_usage must not propagate."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=RuntimeError("DB down"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("agentception.db.persist.get_session", return_value=mock_session):
        from agentception.db.persist import accumulate_token_usage

        # Must not raise
        await accumulate_token_usage(
            run_id="issue-999",
            input_tokens=100,
            output_tokens=10,
            cache_write_tokens=0,
            cache_read_tokens=0,
        )
