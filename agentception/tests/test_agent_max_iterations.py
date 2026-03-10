"""Tests for the agent_max_iterations field on AgentCeptionSettings.

Covers:
- Default value is 100.
- Env var ``AGENT_MAX_ITERATIONS`` is respected.
- Values below 1 are rejected with a clear error message.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentception.config import AgentCeptionSettings


def test_agent_max_iterations_default() -> None:
    """Default value is 100 — generous but bounded."""
    s = AgentCeptionSettings()
    assert s.agent_max_iterations == 100


def test_agent_max_iterations_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENT_MAX_ITERATIONS env var overrides the default."""
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "50")
    s = AgentCeptionSettings()
    assert s.agent_max_iterations == 50


def test_agent_max_iterations_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is rejected — an agent needs at least one iteration."""
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "0")
    with pytest.raises(ValidationError, match="agent_max_iterations must be a positive integer"):
        AgentCeptionSettings()


def test_agent_max_iterations_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative values are rejected with a descriptive error."""
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "-5")
    with pytest.raises(ValidationError, match="agent_max_iterations must be a positive integer"):
        AgentCeptionSettings()


def test_agent_max_iterations_accepts_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """1 is the minimum valid value."""
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "1")
    s = AgentCeptionSettings()
    assert s.agent_max_iterations == 1
