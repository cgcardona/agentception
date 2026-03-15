"""Tests for agentception/services/llm.py — public API and local adapter.

Covers:
- completion_with_tools branches on effective_llm_provider (local vs anthropic).
- completion and completion_stream branch on effective_llm_provider.
- Local adapter: OpenAI content normalization, streaming with fallback.
- _normalize_think_tags: <think> tag splitting for model-agnostic thinking/content.

Run targeted:
    pytest agentception/tests/test_llm.py -v
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentception.config import LLMProviderChoice
from agentception.services.llm import LLMChunk, _normalize_think_tags
from agentception.types import JsonValue


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

    msg: dict[str, JsonValue] = {
        "content": [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world."},
        ]
    }
    assert _normalize_openai_message_content(msg) == "Hello world."


def test_normalize_openai_message_content_strips_reasoning() -> None:
    """Reasoning parts are omitted; only text parts form the final answer."""
    from agentception.services.llm import _normalize_openai_message_content

    msg: dict[str, JsonValue] = {
        "content": [
            {"type": "reasoning", "text": "Let me think..."},
            {"type": "text", "text": "The answer is 42."},
        ]
    }
    assert _normalize_openai_message_content(msg) == "The answer is 42."


def test_local_cap_max_tokens_respects_ceiling() -> None:
    """Adapter clamps requested max_tokens to the configured ceiling."""
    from agentception.services.llm import _local_cap_max_tokens

    with patch("agentception.services.llm.settings") as mock_settings:
        mock_settings.local_llm_completion_token_ceiling = 4096
        assert _local_cap_max_tokens(8192) == 4096
        assert _local_cap_max_tokens(100) == 100
        mock_settings.local_llm_completion_token_ceiling = 2048
        assert _local_cap_max_tokens(8192) == 2048


def test_local_completion_payload_caps_max_tokens() -> None:
    """Plan 1A asks for 8192; local payload must not exceed server ceiling."""
    from agentception.services.llm import _local_completion_payload

    with patch("agentception.services.llm.settings") as mock_settings:
        mock_settings.local_llm_completion_token_ceiling = 4096
        mock_settings.local_llm_model = ""
        payload = _local_completion_payload(
            "sys", "user", temperature=0.2, max_tokens=8192, stream=True
        )
        assert payload["max_tokens"] == 4096


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

    tool_response: dict[str, JsonValue] = {
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

    tool_response: dict[str, JsonValue] = {
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

        async def __aexit__(self, *args: str | int | bool | float | None) -> None:
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


# ---------------------------------------------------------------------------
# _normalize_think_tags — model-agnostic <think> tag splitting
# ---------------------------------------------------------------------------


async def _chunks_from(items: list[LLMChunk]) -> AsyncGenerator[LLMChunk, None]:
    """Helper: yield LLMChunks from a list."""
    for item in items:
        yield item


async def _collect(stream: AsyncGenerator[LLMChunk, None]) -> list[LLMChunk]:
    """Collect all chunks from an async generator."""
    return [c async for c in stream]


@pytest.mark.anyio
async def test_think_tags_split_thinking_and_content() -> None:
    """<think>...</think> content is reclassified as thinking; rest is content."""
    raw = [LLMChunk(type="content", text="<think>reasoning</think>YAML output")]
    result = await _collect(_normalize_think_tags(_chunks_from(raw)))
    types = [c["type"] for c in result]
    assert types == ["thinking", "content"]
    assert result[0]["text"] == "reasoning"
    assert result[1]["text"] == "YAML output"


@pytest.mark.anyio
async def test_think_tags_no_tags_passthrough() -> None:
    """Content without <think> tags passes through unchanged."""
    raw = [LLMChunk(type="content", text="initiative: foo\nphases: []")]
    result = await _collect(_normalize_think_tags(_chunks_from(raw)))
    assert len(result) == 1
    assert result[0]["type"] == "content"
    assert result[0]["text"] == "initiative: foo\nphases: []"


@pytest.mark.anyio
async def test_think_tags_preserves_thinking_type_chunks() -> None:
    """Chunks already typed as thinking pass through without double-processing."""
    raw = [
        LLMChunk(type="thinking", text="already classified"),
        LLMChunk(type="content", text="YAML here"),
    ]
    result = await _collect(_normalize_think_tags(_chunks_from(raw)))
    assert result[0] == LLMChunk(type="thinking", text="already classified")
    assert result[1] == LLMChunk(type="content", text="YAML here")


@pytest.mark.anyio
async def test_think_tags_split_across_chunks() -> None:
    """<think> tag split across two content chunks is handled correctly."""
    raw = [
        LLMChunk(type="content", text="<think>start of thinking"),
        LLMChunk(type="content", text=" more thinking</think>real content"),
    ]
    result = await _collect(_normalize_think_tags(_chunks_from(raw)))
    thinking_text = "".join(c["text"] for c in result if c["type"] == "thinking")
    content_text = "".join(c["text"] for c in result if c["type"] == "content")
    assert "start of thinking" in thinking_text
    assert "more thinking" in thinking_text
    assert content_text == "real content"


@pytest.mark.anyio
async def test_think_tags_multiline_qwen_style() -> None:
    """Typical Qwen3 output: <think>\\n...\\n</think>\\n\\nYAML."""
    raw = [
        LLMChunk(
            type="content",
            text=(
                "<think>\nLet me plan this carefully.\n"
                "I need 2 phases.\n</think>\n\n"
                "initiative: my-plan\nphases: []"
            ),
        ),
    ]
    result = await _collect(_normalize_think_tags(_chunks_from(raw)))
    types = [c["type"] for c in result]
    assert "thinking" in types
    assert "content" in types
    content_text = "".join(c["text"] for c in result if c["type"] == "content")
    assert "initiative: my-plan" in content_text
    thinking_text = "".join(c["text"] for c in result if c["type"] == "thinking")
    assert "Let me plan" in thinking_text
