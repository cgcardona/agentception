from __future__ import annotations

"""Tests for prompt_variant field on AgentTaskSpec and ACAgentRun."""

import pytest

from agentception.models import AgentTaskSpec
from agentception.db.models import ACAgentRun


@pytest.mark.anyio
async def test_prompt_variant_defaults_none() -> None:
    task = AgentTaskSpec()
    assert task.prompt_variant is None


@pytest.mark.anyio
async def test_prompt_variant_stored_on_model() -> None:
    """ACAgentRun accepts prompt_variant and stores it without error."""
    run = ACAgentRun()
    run.prompt_variant = "streamlined"
    assert run.prompt_variant == "streamlined"


@pytest.mark.anyio
async def test_prompt_variant_none_when_not_set() -> None:
    """ACAgentRun prompt_variant is None when not assigned."""
    run = ACAgentRun()
    assert run.prompt_variant is None
