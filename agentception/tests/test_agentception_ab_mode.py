from __future__ import annotations

"""Tests for A/B mode role-file selection (AC-504).

Covers:
- Even BATCH_ID second selects variant A when A/B mode is enabled.
- Odd BATCH_ID second selects variant B when A/B mode is enabled.
- Disabled A/B mode returns the default role file unchanged.
- Unparseable BATCH_ID falls back to default when A/B mode is enabled.
- Missing variant file falls back to default.

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_agentception_ab_mode.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agentception.intelligence.ab_mode import (
    _extract_seconds,
    _is_even_batch,
    select_role_file,
)
from agentception.models import AbModeConfig, PipelineConfig


# ── _extract_seconds ──────────────────────────────────────────────────────────


def test_extract_seconds_canonical_format() -> None:
    """_extract_seconds must parse the canonical eng-YYYYMMDDTHHMMSSz-<hex> format."""
    # Seconds component is 43 in this BATCH_ID.
    assert _extract_seconds("eng-20260302T054843Z-54b3") == 43


def test_extract_seconds_even() -> None:
    """_extract_seconds must return an even number for an even-second BATCH_ID."""
    seconds = _extract_seconds("eng-20260302T120000Z-abcd")
    assert seconds == 0


def test_extract_seconds_odd() -> None:
    """_extract_seconds must return an odd number for an odd-second BATCH_ID."""
    seconds = _extract_seconds("eng-20260302T120001Z-abcd")
    assert seconds == 1


def test_extract_seconds_unparseable_returns_none() -> None:
    """_extract_seconds must return None for a BATCH_ID that has no timestamp."""
    assert _extract_seconds("no-timestamp-here") is None


# ── _is_even_batch ────────────────────────────────────────────────────────────


def test_is_even_batch_even_second() -> None:
    """_is_even_batch returns True when the BATCH_ID second component is even."""
    assert _is_even_batch("eng-20260302T120000Z-0000") is True


def test_is_even_batch_odd_second() -> None:
    """_is_even_batch returns False when the BATCH_ID second component is odd."""
    assert _is_even_batch("eng-20260302T120001Z-0000") is False


def test_is_even_batch_unparseable_returns_none() -> None:
    """_is_even_batch returns None when the BATCH_ID cannot be parsed."""
    assert _is_even_batch("garbage") is None


# ── select_role_file ──────────────────────────────────────────────────────────


def _make_config(enabled: bool, variant_a: str | None = None, variant_b: str | None = None) -> PipelineConfig:
    """Build a PipelineConfig with the specified A/B mode settings."""
    return PipelineConfig(
        max_eng_vps=1,
        max_qa_vps=1,
        pool_size_per_vp=4,
        active_labels_order=[],
        ab_mode=AbModeConfig(
            enabled=enabled,
            target_role="python-developer",
            variant_a_file=variant_a,
            variant_b_file=variant_b,
        ),
    )


@pytest.mark.anyio
async def test_ab_mode_selects_variant_a_for_even_batch() -> None:
    """select_role_file returns variant_a_file when A/B mode is on and batch second is even."""
    config = _make_config(
        enabled=True,
        variant_a=".cursor/roles/python-developer.md",
        variant_b=".cursor/roles/python-developer-v2.md",
    )
    # Seconds = 00 (even) → variant A
    even_batch_id = "eng-20260302T120000Z-abcd"

    with patch(
        "agentception.intelligence.ab_mode.read_pipeline_config",
        new=AsyncMock(return_value=config),
    ):
        result = await select_role_file(even_batch_id, "default.md")

    assert result == ".cursor/roles/python-developer.md"


@pytest.mark.anyio
async def test_ab_mode_selects_variant_b_for_odd_batch() -> None:
    """select_role_file returns variant_b_file when A/B mode is on and batch second is odd."""
    config = _make_config(
        enabled=True,
        variant_a=".cursor/roles/python-developer.md",
        variant_b=".cursor/roles/python-developer-v2.md",
    )
    # Seconds = 01 (odd) → variant B
    odd_batch_id = "eng-20260302T120001Z-abcd"

    with patch(
        "agentception.intelligence.ab_mode.read_pipeline_config",
        new=AsyncMock(return_value=config),
    ):
        result = await select_role_file(odd_batch_id, "default.md")

    assert result == ".cursor/roles/python-developer-v2.md"


@pytest.mark.anyio
async def test_ab_mode_disabled_uses_default_role() -> None:
    """select_role_file returns default_role_file when A/B mode is disabled."""
    config = _make_config(
        enabled=False,
        variant_a=".cursor/roles/python-developer.md",
        variant_b=".cursor/roles/python-developer-v2.md",
    )
    batch_id = "eng-20260302T120001Z-abcd"

    with patch(
        "agentception.intelligence.ab_mode.read_pipeline_config",
        new=AsyncMock(return_value=config),
    ):
        result = await select_role_file(batch_id, "default.md")

    assert result == "default.md"


@pytest.mark.anyio
async def test_ab_mode_unparseable_batch_id_falls_back_to_default() -> None:
    """select_role_file returns default_role_file when the BATCH_ID cannot be parsed."""
    config = _make_config(
        enabled=True,
        variant_a=".cursor/roles/python-developer.md",
        variant_b=".cursor/roles/python-developer-v2.md",
    )

    with patch(
        "agentception.intelligence.ab_mode.read_pipeline_config",
        new=AsyncMock(return_value=config),
    ):
        result = await select_role_file("not-a-valid-batch-id", "default.md")

    assert result == "default.md"


@pytest.mark.anyio
async def test_ab_mode_missing_variant_file_falls_back_to_default() -> None:
    """select_role_file falls back to default when the resolved variant file is None."""
    config = _make_config(enabled=True, variant_a=None, variant_b=None)
    even_batch_id = "eng-20260302T120000Z-abcd"

    with patch(
        "agentception.intelligence.ab_mode.read_pipeline_config",
        new=AsyncMock(return_value=config),
    ):
        result = await select_role_file(even_batch_id, "default.md")

    assert result == "default.md"
