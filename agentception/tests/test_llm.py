"""Tests for agentception/services/llm.py — public API and provider selection.

Covers:
- completion_with_tools branches on effective_llm_provider (local vs anthropic).
- completion and completion_stream branch on effective_llm_provider.

Run targeted:
    pytest agentception/tests/test_llm.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agentception.config import LLMProviderChoice


@pytest.mark.anyio
async def test_completion_with_tools_uses_local_adapter_when_effective_provider_local() -> None:
    """When effective_llm_provider is local, completion_with_tools calls call_local_with_tools."""
    from agentception.services.llm import completion_with_tools

    tool_response: dict[str, object] = {
        "stop_reason": "stop",
        "content": "ok",
        "tool_calls": [],
    }
    with (
        patch("agentception.services.llm.settings") as mock_settings,
        patch(
            "agentception.services.llm.call_local_with_tools",
            new_callable=AsyncMock,
            return_value=tool_response,
        ) as mock_local,
        patch(
            "agentception.services.llm.call_anthropic_with_tools",
            new_callable=AsyncMock,
        ) as mock_anthropic,
    ):
        mock_settings.effective_llm_provider = LLMProviderChoice.local
        result = await completion_with_tools(
            [{"role": "user", "content": "hi"}],
            system="You are a helper.",
            tools=[],
        )
        assert result == tool_response
        mock_local.assert_called_once()
        mock_anthropic.assert_not_called()


@pytest.mark.anyio
async def test_completion_with_tools_uses_anthropic_adapter_when_effective_provider_anthropic() -> None:
    """When effective_llm_provider is anthropic, completion_with_tools calls call_anthropic_with_tools."""
    from agentception.services.llm import completion_with_tools

    tool_response: dict[str, object] = {
        "stop_reason": "stop",
        "content": "ok",
        "tool_calls": [],
    }
    with (
        patch("agentception.services.llm.settings") as mock_settings,
        patch(
            "agentception.services.llm.call_local_with_tools",
            new_callable=AsyncMock,
        ) as mock_local,
        patch(
            "agentception.services.llm.call_anthropic_with_tools",
            new_callable=AsyncMock,
            return_value=tool_response,
        ) as mock_anthropic,
    ):
        mock_settings.effective_llm_provider = LLMProviderChoice.anthropic
        result = await completion_with_tools(
            [{"role": "user", "content": "hi"}],
            system="You are a helper.",
            tools=[],
        )
        assert result == tool_response
        mock_anthropic.assert_called_once()
        mock_local.assert_not_called()
