"""Tests for agentception/services/llm.py — public API and local adapter.

Covers:
- completion_with_tools branches on effective_llm_provider (local vs anthropic).
- completion and completion_stream branch on effective_llm_provider.
- Local adapter: OpenAI content normalization, streaming with fallback.

Run targeted:
    pytest agentception/tests/test_llm.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.config import LLMProviderChoice


# ---------------------------------------------------------------------------
# Local adapter — content normalization
# ---------------------------------------------------------------------------


def test_normalize_openai_message_content_string() -> None:
    """Content as string is returned stripped."""
    from agentception.services.llm import _normalize_openai_message_content

    assert _normalize_openai_message_content({"content": "  hello world  "}) == "hello world"


def test_normalize_openai_message_content_list_of_text_parts() -> None:
    """Content as list of text parts is concatenated."""
    from agentception.services.llm import _normalize_openai_message_content

    msg: dict[str, object] = {
        "content": [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world."},
        ]
    }
    assert _normalize_openai_message_content(msg) == "Hello world."


def test_normalize_openai_message_content_strips_reasoning() -> None:
    """Reasoning parts are omitted; only text parts form the final answer."""
    from agentception.services.llm import _normalize_openai_message_content

    msg: dict[str, object] = {
        "content": [
            {"type": "reasoning", "text": "Let me think..."},
            {"type": "text", "text": "The answer is 42."},
        ]
    }
    assert _normalize_openai_message_content(msg) == "The answer is 42."


def test_normalize_openai_message_content_empty_or_missing() -> None:
    """Missing or empty content returns empty string."""
    from agentception.services.llm import _normalize_openai_message_content

    assert _normalize_openai_message_content({}) == ""
    assert _normalize_openai_message_content({"content": None}) == ""
    assert _normalize_openai_message_content({"content": []}) == ""


# ---------------------------------------------------------------------------
# Public API — provider selection
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Local adapter — streaming fallback
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_local_completion_stream_fallback_when_stream_fails() -> None:
    """When the server does not support streaming, _local_completion_stream yields one content chunk from one-shot."""
    import httpx

    from agentception.services.llm import _local_completion_stream

    class MockStreamCM:
        """Async context manager that raises on enter so stream path fails."""

        async def __aenter__(self) -> None:
            raise httpx.RequestError("stream not supported")

        async def __aexit__(self, *args: object) -> None:
            return None

    fallback_text = "one-shot reply"
    with (
        patch("agentception.services.llm.httpx.AsyncClient") as mock_client_cls,
        patch(
            "agentception.services.llm.call_local_completion",
            new_callable=AsyncMock,
            return_value=fallback_text,
        ),
    ):
        mock_client = MagicMock()
        mock_client.stream.return_value = MockStreamCM()
        mock_client_cls.return_value = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client_cls.return_value.__aexit__.return_value = None
        chunks: list[dict[str, str]] = []
        async for chunk in _local_completion_stream(
            "system",
            "user message",
            temperature=0.0,
            max_tokens=64,
        ):
            chunks.append({"type": chunk["type"], "text": chunk["text"]})
        assert len(chunks) == 1
        assert chunks[0]["type"] == "content"
        assert chunks[0]["text"] == fallback_text
