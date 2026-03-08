from __future__ import annotations

"""Async OpenRouter client for AgentCeption's direct LLM calls.

Patterns and implementation standards for the LLM client:
  - Provider routing lock (Anthropic direct, no Bedrock/Vertex fallback)
  - Extended reasoning via payload["reasoning"] — yields thinking deltas
    separately from content deltas so the UI can display them differently
  - Exponential backoff retry on 429/5xx/timeout
  - Persistent httpx.AsyncClient (re-used across requests, not recreated per call)

Three public entry points:

``call_openrouter(user_prompt, ...)``
    Waits for the full completion and returns the text.  No retry for now on
    the non-streaming path (used only for MCP tools where latency matters less).

``call_openrouter_stream(user_prompt, ...)``
    AsyncGenerator that yields dicts as SSE-ready events:
      {"type": "thinking", "text": "..."}  -- reasoning token (chain of thought)
      {"type": "content",  "text": "..."}  -- output token (the actual YAML)
    Callers map these to their own SSE event format.

``call_openrouter_with_tools(messages, ...)``
    Multi-turn tool-use call.  Accepts a message history and a list of OpenAI-
    format tool definitions.  Returns a :class:`ToolResponse` containing the
    model's text output, any tool calls it made, and the stop reason.  The
    caller is responsible for dispatching tool calls and appending results to
    the message list before calling again.  Used by the Cursor-free agent loop.

The key is read from ``settings.openrouter_api_key`` (env var
``OPENROUTER_API_KEY``).  A missing key raises ``RuntimeError``.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Literal, TypedDict

import httpx

from agentception.config import settings

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL = "anthropic/claude-sonnet-4.6"
_DEFAULT_TIMEOUT = 120.0
_MAX_RETRIES = 2

# Both Claude 4.x models support reasoning and caching via Anthropic direct.
_REASONING_MODELS = {"anthropic/claude-sonnet-4.6", "anthropic/claude-opus-4.6"}


class LLMChunk(TypedDict):
    """A single event yielded by ``call_openrouter_stream``."""

    type: Literal["thinking", "content"]
    text: str


# ---------------------------------------------------------------------------
# Tool-use types (used by call_openrouter_with_tools and agent_loop)
# ---------------------------------------------------------------------------


class ToolFunction(TypedDict):
    """Function spec inside an OpenAI-format tool definition."""

    name: str
    description: str
    parameters: dict[str, object]


class ToolDefinition(TypedDict):
    """OpenAI-format tool definition passed to the model."""

    type: Literal["function"]
    function: ToolFunction


class ToolCallFunction(TypedDict):
    """Function call detail inside a tool_call response block."""

    name: str
    arguments: str  # JSON-encoded argument dict


class ToolCall(TypedDict):
    """A single tool invocation returned by the model."""

    id: str
    type: Literal["function"]
    function: ToolCallFunction


class ToolResponse(TypedDict):
    """Return value from ``call_openrouter_with_tools``."""

    stop_reason: str  # "stop" | "tool_calls" | "length"
    content: str  # text output (empty when stop_reason is "tool_calls")
    tool_calls: list[ToolCall]  # empty when stop_reason is "stop"


def _base_headers() -> dict[str, str]:
    """Build the shared HTTP headers for every OpenRouter request."""
    api_key = settings.openrouter_api_key
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not configured -- "
            "set it in .env and restart the agentception service."
        )
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://agentception.local",
        "X-Title": "AgentCeption",
    }


# ---------------------------------------------------------------------------
# Persistent client (re-used across requests for connection pooling)
# ---------------------------------------------------------------------------

_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return the module-level shared client, creating it on first call."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            headers=_base_headers(),
        )
    return _shared_client


# ---------------------------------------------------------------------------
# Non-streaming call (used by MCP tools and validate endpoint)
# ---------------------------------------------------------------------------


async def call_openrouter(
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """Call Claude Sonnet via OpenRouter and return the full text response.

    Args:
        user_prompt: The user-turn message.
        system_prompt: Optional system-turn message.
        temperature: Sampling temperature (0.0--1.0).
        max_tokens: Maximum tokens in the completion.

    Returns:
        The raw text string of the model's first completion choice.

    Raises:
        RuntimeError: When ``OPENROUTER_API_KEY`` is not set.
        httpx.HTTPStatusError: On non-2xx responses after retries.
        httpx.TimeoutException: When the request exceeds ``_DEFAULT_TIMEOUT``.
    """
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    payload: dict[str, object] = {
        "model": _MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        # Lock to direct Anthropic — avoids Bedrock/Vertex variants that may
        # behave differently with caching / reasoning params.
        "provider": {"order": ["anthropic"], "allow_fallbacks": False},
    }

    logger.info("✅ LLM call — model=%s prompt_chars=%d", _MODEL, len(user_prompt))

    client = _get_client()
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            backoff = 2 ** attempt
            logger.warning("⚠️ LLM retry %d/%d after %ds", attempt, _MAX_RETRIES, backoff)
            await asyncio.sleep(backoff)
        try:
            resp = await client.post(_OPENROUTER_URL, json=payload)
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code in (429, 500, 502, 503, 504):
                continue
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            continue
    else:
        raise last_error or RuntimeError("LLM request failed after retries")

    data: object = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected OpenRouter response type: {type(data)}")
    choices: object = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"OpenRouter returned no choices: {data}")
    first: object = choices[0]
    if not isinstance(first, dict):
        raise ValueError(f"Unexpected choice format: {first}")
    message: object = first.get("message")
    if not isinstance(message, dict):
        raise ValueError(f"Unexpected message format: {first}")
    content: object = message.get("content", "")
    if not isinstance(content, str):
        raise ValueError(f"Unexpected content type: {type(content)}")

    logger.info("✅ LLM response — %d chars", len(content))
    return content


# ---------------------------------------------------------------------------
# Streaming call with extended reasoning
# ---------------------------------------------------------------------------


async def call_openrouter_stream(
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    reasoning_fraction: float = 0.35,
) -> AsyncGenerator[LLMChunk, None]:
    """Stream chunks from Claude Sonnet with extended reasoning enabled.

    Yields :class:`LLMChunk` dicts with ``type`` set to:

    ``"thinking"``
        Reasoning token from ``delta.reasoning_details`` (chain of thought).
        Shown in the UI as dim/muted text before the YAML appears.

    ``"content"``
        Output token from ``delta.content`` (the actual YAML being written).
        Shown as bright green code text.

    Provider lock, reasoning budget, and retry logic below.

    Args:
        user_prompt: The user-turn message.
        system_prompt: Optional system-turn message.
        temperature: Sampling temperature.
        max_tokens: Maximum total tokens (reasoning + output).
        reasoning_fraction: Fraction of ``max_tokens`` reserved for reasoning
            (default 0.35 → ~1400 tokens of thinking on a 4096 budget).

    Raises:
        RuntimeError: Missing API key.
        httpx.HTTPStatusError: Non-2xx from OpenRouter after retries.
        httpx.TimeoutException: Request timeout.
    """
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    reasoning_budget = max(int(max_tokens * reasoning_fraction), 1024)

    payload: dict[str, object] = {
        "model": _MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        # Lock to direct Anthropic (same rationale as non-streaming path).
        "provider": {"order": ["anthropic"], "allow_fallbacks": False},
    }

    if _MODEL in _REASONING_MODELS:
        payload["reasoning"] = {"max_tokens": reasoning_budget}
        logger.info("🧠 Reasoning enabled — budget=%d tokens", reasoning_budget)

    logger.info(
        "✅ LLM stream start — model=%s prompt_chars=%d reasoning=%d",
        _MODEL, len(user_prompt), reasoning_budget,
    )

    total_thinking = 0
    total_content = 0

    async with _get_client().stream("POST", _OPENROUTER_URL, json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[6:]
            if raw == "[DONE]":
                break
            try:
                chunk: object = json.loads(raw)
                if not isinstance(chunk, dict):
                    continue
                choices: object = chunk.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice: object = choices[0]
                if not isinstance(choice, dict):
                    continue
                delta: object = choice.get("delta")
                if not isinstance(delta, dict):
                    continue

                # Reasoning tokens — chain of thought from Anthropic via OR.
                # Both delta.reasoning (string) and delta.reasoning_details (array)
                # are present; use only the structured array to avoid double-emit
                # (streaming response pattern).
                for detail in delta.get("reasoning_details") or []:
                    if not isinstance(detail, dict):
                        continue
                    if detail.get("type") == "reasoning.text":
                        text: object = detail.get("text", "")
                        if isinstance(text, str) and text:
                            total_thinking += len(text)
                            yield LLMChunk(type="thinking", text=text)

                # Output content tokens.
                content_text: object = delta.get("content", "")
                if isinstance(content_text, str) and content_text:
                    total_content += len(content_text)
                    yield LLMChunk(type="content", text=content_text)

            except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
                continue

    logger.info(
        "✅ LLM stream done — thinking=%d chars content=%d chars",
        total_thinking, total_content,
    )


# ---------------------------------------------------------------------------
# Multi-turn tool-use call (used by the Cursor-free agent loop)
# ---------------------------------------------------------------------------


async def call_openrouter_with_tools(
    messages: list[dict[str, object]],
    *,
    system: str,
    tools: list[ToolDefinition],
    model: str = _MODEL,
    temperature: float = 0.0,
    max_tokens: int = 8192,
) -> ToolResponse:
    """Call Claude via OpenRouter with tool-use support.

    The caller maintains the full message history and passes it on every turn.
    This function is stateless — it sends one request and returns the result.

    Args:
        messages: Conversation history (user / assistant / tool messages).
            The system prompt is NOT included here; pass it via ``system``.
        system: System prompt prepended to every request.
        tools: OpenAI-format tool definitions the model may call.
        model: OpenRouter model identifier.
        temperature: Sampling temperature.  Defaults to 0 for determinism.
        max_tokens: Maximum tokens the model may emit per turn.

    Returns:
        :class:`ToolResponse` with ``stop_reason``, ``content``, and a
        (possibly empty) list of ``tool_calls`` to dispatch.

    Raises:
        RuntimeError: Missing API key or unrecoverable HTTP failure.
        httpx.HTTPStatusError: Non-2xx after retries.
    """
    full_messages: list[dict[str, object]] = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    payload: dict[str, object] = {
        "model": model,
        "messages": full_messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "provider": {"order": ["anthropic"], "allow_fallbacks": False},
    }

    logger.info(
        "✅ LLM tool-use call — model=%s turns=%d tools=%d",
        model,
        len(messages),
        len(tools),
    )

    client = _get_client()
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            backoff = 2**attempt
            logger.warning("⚠️ LLM retry %d/%d after %ds", attempt, _MAX_RETRIES, backoff)
            await asyncio.sleep(backoff)
        try:
            resp = await client.post(_OPENROUTER_URL, json=payload)
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code in (429, 500, 502, 503, 504):
                continue
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            continue
    else:
        raise last_error or RuntimeError("LLM tool-use request failed after retries")

    data: object = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected OpenRouter response type: {type(data)}")
    choices: object = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"OpenRouter returned no choices: {data}")
    first: object = choices[0]
    if not isinstance(first, dict):
        raise ValueError(f"Unexpected choice format: {first}")

    finish_reason: object = first.get("finish_reason", "stop")
    if not isinstance(finish_reason, str):
        finish_reason = "stop"

    message: object = first.get("message")
    if not isinstance(message, dict):
        raise ValueError(f"Unexpected message format: {first}")

    raw_content: object = message.get("content") or ""
    content = raw_content if isinstance(raw_content, str) else ""

    raw_tool_calls: object = message.get("tool_calls") or []
    if not isinstance(raw_tool_calls, list):
        raw_tool_calls = []

    tool_calls: list[ToolCall] = []
    for tc in raw_tool_calls:
        if not isinstance(tc, dict):
            continue
        tc_id: object = tc.get("id", "")
        tc_fn: object = tc.get("function")
        if not isinstance(tc_id, str) or not isinstance(tc_fn, dict):
            continue
        fn_name: object = tc_fn.get("name", "")
        fn_args: object = tc_fn.get("arguments", "{}")
        if not isinstance(fn_name, str) or not isinstance(fn_args, str):
            continue
        tool_calls.append(
            ToolCall(
                id=tc_id,
                type="function",
                function=ToolCallFunction(name=fn_name, arguments=fn_args),
            )
        )

    logger.info(
        "✅ LLM tool-use done — stop_reason=%s content_chars=%d tool_calls=%d",
        finish_reason,
        len(content),
        len(tool_calls),
    )
    return ToolResponse(
        stop_reason=finish_reason,
        content=content,
        tool_calls=tool_calls,
    )
