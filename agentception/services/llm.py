from __future__ import annotations

"""Async Anthropic client for AgentCeption's direct LLM calls.

All three public entry points target the Anthropic Messages API directly
(https://api.anthropic.com/v1/messages).  Prompt caching (cache_control:
ephemeral on the system prompt) is active for claude-sonnet-4-6 and later,
giving ~90% input-token discount on turns 2-N of every agent run.

Three public entry points:

``call_anthropic(user_prompt, ...)``
    Waits for the full completion and returns the text.  Used by the Phase
    Planner and MCP tools where a single-turn, non-streaming response suffices.

``call_anthropic_stream(user_prompt, ...)``
    AsyncGenerator that yields :class:`LLMChunk` dicts as SSE-ready events:
      {"type": "thinking", "text": "..."}  -- extended-thinking token
      {"type": "content",  "text": "..."}  -- output token (the actual YAML)
    Callers map these to their own SSE event format.

``call_anthropic_with_tools(messages, ...)``
    Multi-turn tool-use call.  Accepts an OpenAI-format message history and
    a list of OpenAI-format tool definitions; converts both to Anthropic wire
    format internally so the caller (agent_loop) does not need to change.
    Returns a :class:`ToolResponse` with the model's text, any tool calls,
    and the stop reason.

The key is read from ``settings.anthropic_api_key`` (env var
``ANTHROPIC_API_KEY``).  A missing key raises ``RuntimeError``.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Literal, NotRequired, TypedDict

import httpx

from agentception.config import settings

logger = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-sonnet-4-6"
_OPUS_MODEL = "claude-opus-4-6"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT = 120.0
_MAX_RETRIES = 2


class LLMChunk(TypedDict):
    """A single event yielded by ``call_anthropic_stream``."""

    type: Literal["thinking", "content"]
    text: str


# ---------------------------------------------------------------------------
# Tool-use types — public interface unchanged; internal wire format differs
# ---------------------------------------------------------------------------


class ToolFunction(TypedDict):
    """Function spec inside an OpenAI-format tool definition."""

    name: str
    description: str
    parameters: dict[str, object]


class ToolDefinition(TypedDict):
    """OpenAI-format tool definition passed in by callers."""

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
    """Return value from ``call_anthropic_with_tools``.

    ``input_tokens`` covers all tokens billed this turn (including cached
    reads at their discounted rate).  ``cache_creation_input_tokens`` and
    ``cache_read_input_tokens`` are non-zero only when prompt caching is
    active; they are used by the debug script and telemetry to confirm cache
    hits and quantify the per-turn discount.
    """

    stop_reason: str  # "stop" | "tool_calls" | "length"
    content: str  # text output (empty when stop_reason is "tool_calls")
    tool_calls: list[ToolCall]  # empty when stop_reason is "stop"
    input_tokens: NotRequired[int]  # total input tokens consumed this turn
    cache_creation_input_tokens: NotRequired[int]  # tokens written to cache (Turn 1)
    cache_read_input_tokens: NotRequired[int]  # tokens read from cache (Turns 2-N)


# ---------------------------------------------------------------------------
# HTTP client helpers
# ---------------------------------------------------------------------------


def _api_key() -> str:
    key = settings.anthropic_api_key
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not configured — "
            "set it in .env and restart the agentception service.  "
            "Obtain a key at https://console.anthropic.com → API Keys."
        )
    return key


def _base_headers() -> dict[str, str]:
    """Build the HTTP headers required by every Anthropic API request."""
    return {
        "x-api-key": _api_key(),
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return the module-level shared client, creating it on first call."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    return _shared_client


# ---------------------------------------------------------------------------
# Format converters — OpenAI ↔ Anthropic
# ---------------------------------------------------------------------------


def _tools_to_anthropic(tools: list[ToolDefinition]) -> list[dict[str, object]]:
    """Convert OpenAI-format tool definitions to Anthropic's input_schema format."""
    result: list[dict[str, object]] = []
    for tool in tools:
        fn = tool["function"]
        result.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn["parameters"],
            }
        )
    return result


def _messages_to_anthropic(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Convert OpenAI-format message history to Anthropic Messages format.

    Key differences handled here:
    - ``role: "tool"`` messages (tool results) become ``role: "user"`` messages
      containing ``tool_result`` content blocks.  Consecutive tool-result
      messages are collapsed into a single user turn (Anthropic requires it).
    - ``role: "assistant"`` messages with ``tool_calls`` become assistant
      messages whose ``content`` is an array of ``tool_use`` blocks.
    """
    out: list[dict[str, object]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role: object = msg.get("role", "")

        if role == "user":
            raw = msg.get("content", "")
            out.append({"role": "user", "content": raw})
            i += 1

        elif role == "assistant":
            blocks: list[dict[str, object]] = []
            text = msg.get("content") or ""
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})

            raw_calls = msg.get("tool_calls") or []
            if isinstance(raw_calls, list):
                for tc in raw_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        continue
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args: object = (
                            json.loads(args_raw)
                            if isinstance(args_raw, str)
                            else args_raw
                        )
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": args,
                        }
                    )
            # Anthropic requires non-empty content; send empty string if no blocks.
            out.append(
                {"role": "assistant", "content": blocks if blocks else ""}
            )
            i += 1

        elif role == "tool":
            # Collapse consecutive tool-result messages into one user turn.
            results: list[dict[str, object]] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tr = messages[i]
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.get("tool_call_id", ""),
                        "content": str(tr.get("content", "")),
                    }
                )
                i += 1
            out.append({"role": "user", "content": results})

        else:
            i += 1

    return out


# ---------------------------------------------------------------------------
# Non-streaming call — simple prompt → text completion
# ---------------------------------------------------------------------------


async def call_anthropic(
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """Call Claude via the Anthropic API and return the full text response.

    Args:
        user_prompt: The user-turn message.
        system_prompt: Optional system-turn message.
        temperature: Sampling temperature (0.0--1.0).
        max_tokens: Maximum tokens in the completion.

    Returns:
        The raw text string of the model's response.

    Raises:
        RuntimeError: When ``ANTHROPIC_API_KEY`` is not set.
        httpx.HTTPStatusError: On non-2xx responses after retries.
        httpx.TimeoutException: When the request exceeds ``_DEFAULT_TIMEOUT``.
    """
    payload: dict[str, object] = {
        "model": _MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if system_prompt:
        payload["system"] = system_prompt

    logger.info("✅ LLM call — model=%s prompt_chars=%d", _MODEL, len(user_prompt))

    client = _get_client()
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            backoff = 2**attempt
            logger.warning("⚠️ LLM retry %d/%d after %ds", attempt, _MAX_RETRIES, backoff)
            await asyncio.sleep(backoff)
        try:
            resp = await client.post(_ANTHROPIC_URL, json=payload, headers=_base_headers())
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
        raise ValueError(f"Unexpected Anthropic response type: {type(data)}")

    content_blocks: object = data.get("content", [])
    if not isinstance(content_blocks, list):
        raise ValueError(f"Anthropic returned unexpected content: {data}")

    text_parts: list[str] = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                text_parts.append(text)

    result = "".join(text_parts)
    logger.info("✅ LLM response — %d chars", len(result))
    return result


# ---------------------------------------------------------------------------
# Streaming call with extended thinking
# ---------------------------------------------------------------------------


async def call_anthropic_stream(
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 1.0,
    max_tokens: int = 16000,
    reasoning_fraction: float = 0.35,
) -> AsyncGenerator[LLMChunk, None]:
    """Stream chunks from Claude with extended thinking enabled.

    Yields :class:`LLMChunk` dicts with ``type`` set to:

    ``"thinking"``
        Extended-thinking token (chain of thought before the answer).
        Shown in the UI as dim/muted text.

    ``"content"``
        Output token (the actual YAML being written).
        Shown as bright green code text.

    Note: Anthropic requires ``temperature=1`` when extended thinking is
    enabled.  The ``temperature`` parameter is accepted for API compatibility
    but overridden to 1.0 internally when a thinking budget is active.

    Args:
        user_prompt: The user-turn message.
        system_prompt: Optional system-turn message.
        temperature: Ignored when thinking is enabled (must be 1.0 per Anthropic).
        max_tokens: Maximum total tokens (thinking + output).
        reasoning_fraction: Fraction of ``max_tokens`` reserved for thinking.

    Raises:
        RuntimeError: Missing API key.
        httpx.HTTPStatusError: Non-2xx after retries.
        httpx.TimeoutException: Request timeout.
    """
    thinking_budget = max(int(max_tokens * reasoning_fraction), 1024)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": user_prompt}
    ]

    payload: dict[str, object] = {
        "model": _MODEL,
        "max_tokens": max_tokens,
        # temperature must be 1 when extended thinking is active (Anthropic requirement).
        "temperature": 1.0,
        "thinking": {"type": "enabled", "budget_tokens": thinking_budget},
        "messages": messages,
        "stream": True,
    }
    if system_prompt:
        payload["system"] = system_prompt

    logger.info(
        "✅ LLM stream start — model=%s prompt_chars=%d thinking_budget=%d",
        _MODEL,
        len(user_prompt),
        thinking_budget,
    )

    total_thinking = 0
    total_content = 0

    async with _get_client().stream(
        "POST", _ANTHROPIC_URL, json=payload, headers=_base_headers()
    ) as resp:
        resp.raise_for_status()
        # Anthropic SSE: each line is "data: {json}" or an event header.
        # content_block_delta carries {"type": "thinking_delta"|"text_delta", ...}
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[6:]
            if raw == "[DONE]":
                break
            try:
                event: object = json.loads(raw)
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "content_block_delta":
                    continue
                delta: object = event.get("delta")
                if not isinstance(delta, dict):
                    continue

                delta_type: object = delta.get("type")

                if delta_type == "thinking_delta":
                    thinking: object = delta.get("thinking", "")
                    if isinstance(thinking, str) and thinking:
                        total_thinking += len(thinking)
                        yield LLMChunk(type="thinking", text=thinking)

                elif delta_type == "text_delta":
                    text: object = delta.get("text", "")
                    if isinstance(text, str) and text:
                        total_content += len(text)
                        yield LLMChunk(type="content", text=text)

            except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
                continue

    logger.info(
        "✅ LLM stream done — thinking=%d chars content=%d chars",
        total_thinking,
        total_content,
    )


# ---------------------------------------------------------------------------
# Multi-turn tool-use call — used by the agent loop
# ---------------------------------------------------------------------------


async def call_anthropic_with_tools(
    messages: list[dict[str, object]],
    *,
    system: str,
    tools: list[ToolDefinition],
    model: str = _MODEL,
    temperature: float = 0.0,
    max_tokens: int = 8192,
) -> ToolResponse:
    """Call Claude via the Anthropic API with tool-use support.

    Accepts and returns OpenAI-format data structures so the caller
    (agent_loop) does not need to change.  The conversion to Anthropic's
    wire format — content-block arrays, tool_use/tool_result blocks,
    input_schema instead of parameters — happens internally.

    Prompt caching is applied to the system prompt (cache_control: ephemeral).
    Turn 1 writes the cache; turns 2-N read it at ~10% of normal input cost.

    Args:
        messages: OpenAI-format conversation history (user/assistant/tool).
        system: System prompt prepended to every request (cached).
        tools: OpenAI-format tool definitions the model may call.
        model: Anthropic model ID.
        temperature: Sampling temperature.  Defaults to 0 for determinism.
        max_tokens: Maximum tokens the model may emit per turn.

    Returns:
        :class:`ToolResponse` with ``stop_reason``, ``content``, and a
        (possibly empty) list of ``tool_calls`` to dispatch.

    Raises:
        RuntimeError: Missing API key or unrecoverable HTTP failure.
        httpx.HTTPStatusError: Non-2xx after retries.
    """
    anthropic_tools = _tools_to_anthropic(tools)
    anthropic_messages = _messages_to_anthropic(messages)

    # System prompt as a cacheable content-block array.
    # cache_control: ephemeral → 5-minute TTL; charged at ~10% on cache hits.
    system_block: list[dict[str, object]] = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    payload: dict[str, object] = {
        "model": model,
        "system": system_block,
        "messages": anthropic_messages,
        "tools": anthropic_tools,
        "tool_choice": {"type": "auto"},
        "temperature": temperature,
        "max_tokens": max_tokens,
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
            resp = await client.post(
                _ANTHROPIC_URL, json=payload, headers=_base_headers()
            )
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
        raise ValueError(f"Unexpected Anthropic response type: {type(data)}")

    # Map Anthropic stop reasons to the internal convention the agent loop expects.
    # Anthropic: "tool_use" | "end_turn" | "max_tokens"
    # Internal:  "tool_calls" | "stop"   | "length"
    raw_stop: object = data.get("stop_reason", "end_turn")
    if raw_stop == "tool_use":
        stop_reason = "tool_calls"
    elif raw_stop == "max_tokens":
        stop_reason = "length"
    else:
        stop_reason = "stop"

    content_blocks: object = data.get("content", [])
    if not isinstance(content_blocks, list):
        content_blocks = []

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text", "")
            if isinstance(t, str):
                text_parts.append(t)
        elif btype == "tool_use":
            tool_input: object = block.get("input", {})
            # Convert input dict back to JSON string (OpenAI ToolCallFunction expects it).
            args_str = (
                json.dumps(tool_input)
                if isinstance(tool_input, dict)
                else "{}"
            )
            tool_calls.append(
                ToolCall(
                    id=str(block.get("id", "")),
                    type="function",
                    function=ToolCallFunction(
                        name=str(block.get("name", "")),
                        arguments=args_str,
                    ),
                )
            )

    # Extract token counts — used for rate-limit accounting in the agent loop.
    usage: object = data.get("usage", {})
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    if isinstance(usage, dict):
        raw_input = usage.get("input_tokens", 0)
        input_tokens = int(raw_input) if isinstance(raw_input, int) else 0
        raw_write = usage.get("cache_creation_input_tokens", 0)
        cache_creation_tokens = int(raw_write) if isinstance(raw_write, int) else 0
        raw_read = usage.get("cache_read_input_tokens", 0)
        cache_read_tokens = int(raw_read) if isinstance(raw_read, int) else 0
        logger.info(
            "✅ LLM usage — input=%d cache_written=%d cache_read=%d",
            input_tokens,
            cache_creation_tokens,
            cache_read_tokens,
        )

    content = "".join(text_parts)
    logger.info(
        "✅ LLM tool-use done — stop_reason=%s content_chars=%d tool_calls=%d",
        stop_reason,
        len(content),
        len(tool_calls),
    )
    return ToolResponse(
        stop_reason=stop_reason,
        content=content,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        cache_creation_input_tokens=cache_creation_tokens,
        cache_read_input_tokens=cache_read_tokens,
    )
