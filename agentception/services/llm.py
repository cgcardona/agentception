from __future__ import annotations

"""Async Anthropic client for AgentCeption's direct LLM calls.

All three public entry points target the Anthropic Messages API directly
(https://api.anthropic.com/v1/messages).  Prompt caching (cache_control:
ephemeral on the system prompt) is active; confirmed working on
claude-sonnet-4-6, giving ~90% input-token discount on turns 2-N.

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
import socket
import ssl
from collections.abc import AsyncGenerator
from typing import Literal, NotRequired, TypedDict

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from agentception.config import LLMProviderChoice, settings
from agentception.db.activity_events import persist_activity_event

logger = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-sonnet-4-6"
_OPUS_MODEL = "claude-opus-4-6"
_HAIKU_MODEL = "claude-haiku-4-5"
_ANTHROPIC_VERSION = "2023-06-01"
# Per-phase timeouts for Anthropic API calls.
# connect/write: generous but bounded — API should accept the request quickly.
# read: 300s (5 minutes).  With large agent contexts (50–80k+ tokens across
#   many iterations), Anthropic can take well over 90 seconds before sending
#   the first byte of a response — the server processes the full prompt before
#   beginning to stream.  90s was too aggressive and caused repeated
#   ReadTimeout → retries → run cancellation on large generations.  300s gives
#   ample headroom while still bounding truly hung connections.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
_MAX_RETRIES = 4
# Timeout for the DNS pre-flight check run before every HTTP attempt.
# This guards against the pathological case where the OS-level getaddrinfo()
# call blocks the thread-pool executor indefinitely (observed as
# [Errno -3] Temporary failure in name resolution on Docker when the embedded
# DNS resolver hangs).  httpx's own connect=10.0 timeout cannot cancel a
# blocking getaddrinfo() thread that the OS refuses to interrupt.
#
# We do NOT wrap client.post() itself in asyncio.wait_for() because the read
# phase can legitimately take up to 300 s (large Anthropic generations) — a
# short hard cap would cancel valid responses.  Instead we pre-check DNS in an
# asyncio-cancellable thread-pool call, fail fast if DNS is hung, and then let
# the actual HTTP call use the full httpx timeout budget.
_DNS_PREFLIGHT_TIMEOUT_SECS: float = 15.0
_DNS_PREFLIGHT_HOST = "api.anthropic.com"
_DNS_PREFLIGHT_PORT = 443
# Minimum seconds to wait after a 429 before retrying.  Anthropic's rolling
# TPM window does not clear in 2–4s, so the standard exponential backoff used
# for transient errors is wrong here — it just adds more calls to the burst.
# We read the Retry-After header when present; otherwise this is the floor.
_RATE_LIMIT_BACKOFF_SECS: float = 20.0


def _log_http_error(exc: httpx.HTTPStatusError) -> None:
    """Log a non-retryable Anthropic HTTP error with an actionable message.

    Detects billing errors (credit balance exhausted) and surfaces them with
    a direct remediation hint so operators immediately know what to fix without
    having to parse the raw JSON body.  All other 4xx/5xx errors fall through
    to the generic body dump.
    """
    status = exc.response.status_code
    try:
        body: object = exc.response.json()
    except Exception:
        body = exc.response.text

    if isinstance(body, dict):
        error_block: object = body.get("error", {})
        if isinstance(error_block, dict):
            msg: object = error_block.get("message", "")
            if isinstance(msg, str) and "credit balance" in msg.lower():
                logger.error(
                    "❌ Anthropic billing error (HTTP %d): credit balance exhausted — "
                    "add funds at https://console.anthropic.com/settings/billing",
                    status,
                )
                return

    logger.error(
        "❌ Anthropic API %d — body: %s",
        status,
        exc.response.text,
    )


async def _rate_limit_sleep(response: httpx.Response, attempt: int) -> None:
    """Sleep the appropriate amount after a 429 response.

    Reads the ``Retry-After`` header from the response when present; otherwise
    uses an exponentially growing backoff starting at ``_RATE_LIMIT_BACKOFF_SECS``.
    """
    retry_after_raw = response.headers.get("retry-after", "")
    try:
        wait = max(float(retry_after_raw), _RATE_LIMIT_BACKOFF_SECS)
    except (ValueError, TypeError):
        wait = _RATE_LIMIT_BACKOFF_SECS * (2.0**attempt)
    logger.warning(
        "⚠️ LLM rate-limited (429) retry %d/%d — sleeping %.0fs (Retry-After=%r)",
        attempt + 1,
        _MAX_RETRIES,
        wait,
        retry_after_raw or "not set",
    )
    await asyncio.sleep(wait)


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
    output_tokens: NotRequired[int]  # tokens generated by the model this turn
    cache_creation_input_tokens: NotRequired[int]  # tokens written to cache (Turn 1)
    cache_read_input_tokens: NotRequired[int]  # tokens read from cache (Turns 2-N)


# ---------------------------------------------------------------------------
# Public API (provider-agnostic contract)
# ---------------------------------------------------------------------------
#
# Callers (plan_ui, llm_phase_planner, agent_loop) should use these three
# entry points only. Provider selection (Anthropic vs local) is done here;
# no provider-specific logic in callers.
# ---------------------------------------------------------------------------


async def completion(
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    json_schema: dict[str, object] | None = None,
) -> str:
    """Single-turn completion; returns final answer text (thinking stripped by adapter).

    Provider-agnostic. Branches on ``settings.effective_llm_provider``; local
    adapter uses single-turn no-tools call; Anthropic uses full completion API.
    """
    if settings.effective_llm_provider == LLMProviderChoice.local:
        return await call_local_completion(
            system_prompt or "",
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    return await call_anthropic(
        user_prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        json_schema=json_schema,
    )


async def completion_stream(
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 1.0,
    max_tokens: int = 16000,
    reasoning_fraction: float = 0.35,
) -> AsyncGenerator[LLMChunk, None]:
    """Stream completion chunks; each chunk has type \"thinking\" or \"content\".

    Provider-agnostic. Branches on ``settings.effective_llm_provider``. Local
    adapter does a single completion and yields one content chunk; Anthropic
    streams thinking + content.
    """
    if settings.effective_llm_provider == LLMProviderChoice.local:
        async for chunk in _local_completion_stream(
            system_prompt or "",
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            yield chunk
        return
    async for chunk in call_anthropic_stream(
        user_prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_fraction=reasoning_fraction,
    ):
        yield chunk


async def completion_with_tools(
    messages: list[dict[str, object]],
    *,
    system: str,
    tools: list[ToolDefinition],
    model: str = _MODEL,
    temperature: float = 0.0,
    max_tokens: int = 32000,
    extra_system_blocks: list[dict[str, object]] | None = None,
    session: AsyncSession | None = None,
    run_id: str | None = None,
    iteration: int = 0,
) -> ToolResponse:
    """Multi-turn tool-use completion; returns ToolResponse (content + tool_calls).

    Provider-agnostic. Branches on ``settings.effective_llm_provider``; local
    uses OpenAI-compatible server; otherwise Anthropic.
    """
    if settings.effective_llm_provider == LLMProviderChoice.local:
        return await call_local_with_tools(
            messages,
            system=system,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_system_blocks=extra_system_blocks,
            session=session,
            run_id=run_id,
            iteration=iteration,
        )
    return await call_anthropic_with_tools(
        messages,
        system=system,
        tools=tools,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_system_blocks=extra_system_blocks,
        session=session,
        run_id=run_id,
        iteration=iteration,
    )


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
    """Build the HTTP headers required by every Anthropic API request.

    ``anthropic-beta: prompt-caching-2024-07-31`` is required to enable
    ``cache_control`` on both the system prompt and the tool catalogue.
    Without this header, cache_control blocks are silently ignored.
    """
    return {
        "x-api-key": _api_key(),
        "anthropic-version": _ANTHROPIC_VERSION,
        "anthropic-beta": "prompt-caching-2024-07-31",
        "content-type": "application/json",
    }


async def _dns_preflight() -> None:
    """Pre-flight DNS resolution guard for api.anthropic.com.

    Performs a getaddrinfo lookup via the thread-pool executor, wrapped in
    asyncio.wait_for so that a hanging OS resolver is interrupted after
    _DNS_PREFLIGHT_TIMEOUT_SECS.  Raises asyncio.TimeoutError if DNS hangs,
    or socket.gaierror if DNS fails immediately — both are caught by the
    network/timeout handler in the retry loop so the attempt is retried with
    backoff.

    Why separate from client.post(): wrapping the full HTTP call in
    asyncio.wait_for at a short timeout cancels legitimate Anthropic responses
    that take 60–120 s on large prompts.  The pre-flight isolates the
    DNS/connect check so the read phase retains the full 300 s budget.
    """
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(
        loop.run_in_executor(
            None,
            socket.getaddrinfo,
            _DNS_PREFLIGHT_HOST,
            _DNS_PREFLIGHT_PORT,
            0,
            socket.SOCK_STREAM,
        ),
        timeout=_DNS_PREFLIGHT_TIMEOUT_SECS,
    )


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
    """Convert OpenAI-format tool definitions to Anthropic's input_schema format.

    The last tool in the converted list receives ``cache_control: ephemeral`` so
    Anthropic caches the entire tool catalogue after turn 1.  Without this, every
    turn pays full input-token price for all tool schemas — with 40+ GitHub MCP
    tools included that is easily 8–15K tokens per turn.  Caching cuts it to the
    cache-read rate (~10% of normal cost) from turn 2 onward.

    This is the server-side answer to "on-demand tool discovery": tools are
    loaded once, cached, and referenced cheaply on every subsequent turn.
    """
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
    # Mark the last tool as the cache boundary so the full list is cached.
    if result:
        result[-1] = {**result[-1], "cache_control": {"type": "ephemeral"}}
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
    json_schema: dict[str, object] | None = None,
) -> str:
    """Call Claude via the Anthropic API and return the full text response.

    Args:
        user_prompt: The user-turn message.
        system_prompt: Optional system-turn message.
        temperature: Sampling temperature (0.0--1.0).
        max_tokens: Maximum tokens in the completion.
        json_schema: When set, enables Anthropic's Structured Outputs beta and
            constrains the model to emit JSON matching this schema.  The
            response text will be valid JSON — no prose, no markdown fences.
            Requires the ``structured-outputs-2025-11-13`` beta header.

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
    if json_schema is not None:
        payload["output_format"] = {"type": "json_schema", "schema": json_schema}

    headers = dict(_base_headers())
    if json_schema is not None:
        # Add the structured-outputs beta alongside the existing prompt-caching beta.
        headers["anthropic-beta"] = (
            headers.get("anthropic-beta", "") + ",structured-outputs-2025-11-13"
        )

    logger.info("✅ LLM call — model=%s prompt_chars=%d", _MODEL, len(user_prompt))

    client = _get_client()
    last_error: Exception | None = None
    _total_attempts = _MAX_RETRIES + 1

    for attempt in range(_total_attempts):
        try:
            await _dns_preflight()
            resp = await client.post(_ANTHROPIC_URL, json=payload, headers=headers)
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code == 429:
                await _rate_limit_sleep(exc.response, attempt)
                continue
            if exc.response.status_code in (500, 502, 503, 504, 529):
                backoff = 2 ** (attempt + 1)
                logger.warning("⚠️ LLM retry %d/%d after %ds", attempt + 1, _total_attempts, backoff)
                await asyncio.sleep(backoff)
                continue
            _log_http_error(exc)
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException, httpx.NetworkError, ssl.SSLError, socket.gaierror) as exc:
            last_error = exc
            backoff = 2 ** (attempt + 1)
            logger.warning(
                "⚠️ LLM network/timeout retry %d/%d after %ds — %s",
                attempt + 1,
                _total_attempts,
                backoff,
                exc,
            )
            await asyncio.sleep(backoff)
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

    await _dns_preflight()
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
    max_tokens: int = 32000,
    extra_system_blocks: list[dict[str, object]] | None = None,
    session: AsyncSession | None = None,
    run_id: str | None = None,
    iteration: int = 0,
) -> ToolResponse:
    """Call Claude via the Anthropic API with tool-use support.

    Accepts and returns OpenAI-format data structures so the caller
    (agent_loop) does not need to change.  The conversion to Anthropic's
    wire format — content-block arrays, tool_use/tool_result blocks,
    input_schema instead of parameters — happens internally.

    Prompt caching is applied to both the system prompt and the tool catalogue
    (cache_control: ephemeral on each).  Turn 1 writes both caches; turns 2-N
    read them at ~10% of normal input cost.  With 40+ GitHub MCP tools included,
    tool-list caching alone saves 8–15K tokens per turn.

    Args:
        messages: OpenAI-format conversation history (user/assistant/tool).
        system: System prompt prepended to every request (cached).
        tools: OpenAI-format tool definitions the model may call.
        model: Anthropic model ID.
        temperature: Sampling temperature.  Defaults to 0 for determinism.
        max_tokens: Maximum tokens the model may emit per turn.  32 000 is safe
            at Tier 4 (400K output TPM) with up to 10 concurrent agents.
        extra_system_blocks: Additional Anthropic content blocks appended
            after the cached system prompt block.  Used to inject dynamic
            context (e.g. working memory) without invalidating the cache.

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
    # extra_system_blocks (e.g. working memory) are appended WITHOUT cache_control
    # so they are re-evaluated fresh every turn without busting the main cache.
    system_block: list[dict[str, object]] = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if extra_system_blocks:
        system_block.extend(extra_system_blocks)

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
    if session is not None and run_id is not None:
        persist_activity_event(
            session,
            run_id,
            "llm_iter",
            {"iteration": iteration, "model": model, "turns": len(messages)},
        )
        await session.flush()

    client = _get_client()
    last_error: Exception | None = None
    _total_attempts = _MAX_RETRIES + 1

    for attempt in range(_total_attempts):
        try:
            await _dns_preflight()
            resp = await client.post(_ANTHROPIC_URL, json=payload, headers=_base_headers())
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code == 429:
                await _rate_limit_sleep(exc.response, attempt)
                continue
            if exc.response.status_code in (500, 502, 503, 504, 529):
                backoff = 2 ** (attempt + 1)
                logger.warning("⚠️ LLM retry %d/%d after %ds", attempt + 1, _total_attempts, backoff)
                await asyncio.sleep(backoff)
                continue
            _log_http_error(exc)
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException, httpx.NetworkError, ssl.SSLError, socket.gaierror) as exc:
            last_error = exc
            backoff = 2 ** (attempt + 1)
            logger.warning(
                "⚠️ LLM network/timeout retry %d/%d after %ds — %s",
                attempt + 1,
                _total_attempts,
                backoff,
                exc,
            )
            await asyncio.sleep(backoff)
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

    # Extract token counts — used for rate-limit accounting and cost tracking.
    usage: object = data.get("usage", {})
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    if isinstance(usage, dict):
        raw_input = usage.get("input_tokens", 0)
        input_tokens = int(raw_input) if isinstance(raw_input, int) else 0
        raw_output = usage.get("output_tokens", 0)
        output_tokens = int(raw_output) if isinstance(raw_output, int) else 0
        raw_write = usage.get("cache_creation_input_tokens", 0)
        cache_creation_tokens = int(raw_write) if isinstance(raw_write, int) else 0
        raw_read = usage.get("cache_read_input_tokens", 0)
        cache_read_tokens = int(raw_read) if isinstance(raw_read, int) else 0
        logger.info(
            "✅ LLM usage — input=%d output=%d cache_written=%d cache_read=%d",
            input_tokens,
            output_tokens,
            cache_creation_tokens,
            cache_read_tokens,
        )
        if session is not None and run_id is not None:
            persist_activity_event(
                session,
                run_id,
                "llm_usage",
                {
                    "input_tokens": input_tokens,
                    "cache_write": cache_creation_tokens,
                    "cache_read": cache_read_tokens,
                },
            )
            await session.flush()

    content = "".join(text_parts)

    # Log the agent's full text reply so watch_run.py can display the complete
    # chain of thought.  Newlines are collapsed to spaces so the entry stays on
    # one log line (the log aggregator splits on newlines).
    if content.strip():
        snippet = content.replace("\n", " ").strip()
        logger.info("✅ LLM reply — chars=%d text=%s", len(content), snippet)
        if session is not None and run_id is not None:
            persist_activity_event(
                session,
                run_id,
                "llm_reply",
                {"chars": len(content), "text_preview": content[:200]},
            )
            await session.flush()

    logger.info(
        "✅ LLM tool-use done — stop_reason=%s content_chars=%d tool_calls=%d",
        stop_reason,
        len(content),
        len(tool_calls),
    )
    if session is not None and run_id is not None:
        persist_activity_event(
            session,
            run_id,
            "llm_done",
            {"stop_reason": stop_reason, "tool_call_count": len(tool_calls)},
        )
        await session.flush()

    return ToolResponse(
        stop_reason=stop_reason,
        content=content,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_tokens,
        cache_read_input_tokens=cache_read_tokens,
    )


# ---------------------------------------------------------------------------
# Local adapter — OpenAI-compatible server (e.g. mlx_lm.server)
# Implements the same contract as the Anthropic path: completion → str,
# completion_stream → AsyncGenerator[LLMChunk], completion_with_tools → ToolResponse.
# Used when settings.effective_llm_provider is local.
# ---------------------------------------------------------------------------


def _normalize_openai_message_content(message: dict[str, object]) -> str:
    """Extract final answer string from OpenAI-format message.content.

    Contract: adapter returns only the final answer (thinking/reasoning stripped).
    - If content is a string, return it stripped.
    - If content is a list of parts (e.g. [{"type": "text", "text": "..."}] or
      reasoning + text), concatenate only non-reasoning text parts so the
      returned string is the final answer.
    """
    raw: object = message.get("content")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, list):
        return str(raw).strip()
    parts: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Skip reasoning blocks; contract is final answer only.
        if item.get("type") == "reasoning":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts).strip()


def _tools_to_openai(tools: list[ToolDefinition]) -> list[dict[str, object]]:
    """Convert internal tool definitions to OpenAI /v1/chat/completions format."""
    out: list[dict[str, object]] = []
    for t in tools:
        fn = t["function"]
        out.append(
            {
                "type": "function",
                "function": {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                },
            }
        )
    return out


async def call_local_with_tools(
    messages: list[dict[str, object]],
    *,
    system: str,
    tools: list[ToolDefinition],
    model: str = "local",
    temperature: float = 0.0,
    max_tokens: int = 32000,
    extra_system_blocks: list[dict[str, object]] | None = None,
    session: AsyncSession | None = None,
    run_id: str | None = None,
    iteration: int = 0,
) -> ToolResponse:
    """Call a local OpenAI-compatible server (e.g. mlx_lm.server) with tool use.

    Same contract as :func:`call_anthropic_with_tools`: accepts OpenAI-format
    messages and tools, returns :class:`ToolResponse`. Used when
    ``settings.effective_llm_provider`` is ``local``.
    """
    base = settings.local_llm_base_url.rstrip("/")
    url = f"{base}{settings.local_llm_chat_path}"
    system_content = system
    if extra_system_blocks:
        for blk in extra_system_blocks:
            if isinstance(blk, dict) and blk.get("type") == "text":
                text = blk.get("text", "")
                if isinstance(text, str) and text:
                    system_content = f"{system_content}\n\n{text}"
    request_messages: list[dict[str, object]] = [
        {"role": "system", "content": system_content},
        *messages,
    ]
    payload: dict[str, object] = {
        "messages": request_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = _tools_to_openai(tools)
        payload["tool_choice"] = "auto"
    if settings.local_llm_model:
        payload["model"] = settings.local_llm_model
    # Else omit model so servers like mlx_lm.server use their loaded model (avoids 404).
    if session is not None and run_id is not None:
        persist_activity_event(
            session,
            run_id,
            "llm_iter",
            {"iteration": iteration, "model": model, "turns": len(messages)},
        )
        await session.flush()

    logger.info(
        "✅ Local LLM tool-use call — url=%s turns=%d tools=%d",
        url,
        len(messages),
        len(tools),
    )
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()

    data: object = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Local LLM response not a dict: {type(data)}")

    choices: object = data.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise ValueError("Local LLM response has no choices")
    first: object = choices[0]
    if not isinstance(first, dict):
        raise ValueError("Local LLM first choice not a dict")
    msg: object = first.get("message", {})
    if not isinstance(msg, dict):
        raise ValueError("Local LLM message not a dict")

    finish: object = first.get("finish_reason", "stop")
    if finish == "tool_calls":
        stop_reason = "tool_calls"
    elif finish == "length":
        stop_reason = "length"
    else:
        stop_reason = "stop"

    content = _normalize_openai_message_content(msg)
    raw_calls: object = msg.get("tool_calls") or []
    tool_calls: list[ToolCall] = []
    if isinstance(raw_calls, list):
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments", "{}")
            tool_calls.append(
                ToolCall(
                    id=str(tc.get("id", "")),
                    type="function",
                    function=ToolCallFunction(
                        name=str(fn.get("name", "")),
                        arguments=args if isinstance(args, str) else json.dumps(args),
                    ),
                )
            )

    usage: object = data.get("usage", {})
    input_tokens = 0
    output_tokens = 0
    if isinstance(usage, dict):
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)
    # Same log shape as Anthropic path so watch_run.py shows reply and done.
    if content.strip():
        snippet = content.replace("\n", " ").strip()
        logger.info("✅ LLM reply — chars=%d text=%s", len(content), snippet)
    logger.info(
        "✅ LLM tool-use done — stop_reason=%s content_chars=%d tool_calls=%d",
        stop_reason,
        len(content),
        len(tool_calls),
    )
    if session is not None and run_id is not None:
        persist_activity_event(
            session,
            run_id,
            "llm_usage",
            {"input_tokens": input_tokens, "cache_write": 0, "cache_read": 0},
        )
        await session.flush()
        if content:
            persist_activity_event(
                session,
                run_id,
                "llm_reply",
                {"chars": len(content), "text_preview": content[:200]},
            )
            await session.flush()
        persist_activity_event(
            session,
            run_id,
            "llm_done",
            {"stop_reason": stop_reason, "tool_call_count": len(tool_calls)},
        )
        await session.flush()

    return ToolResponse(
        stop_reason=stop_reason,
        content=content,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _local_base_url() -> str:
    """Base URL for local adapter (no trailing slash)."""
    return settings.local_llm_base_url.rstrip("/")


def _local_chat_url() -> str:
    """Full URL for chat completions."""
    return f"{_local_base_url()}{settings.local_llm_chat_path}"


def _local_completion_payload(
    system: str,
    user_message: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 128,
    stream: bool = False,
) -> dict[str, object]:
    """Build request body for local single-turn completion (no tools)."""
    payload: dict[str, object] = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if settings.local_llm_model:
        payload["model"] = settings.local_llm_model
    return payload


async def call_local_completion(
    system: str,
    user_message: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 128,
) -> str:
    """Single-turn completion against the local OpenAI-compatible server (no tools).

    Returns the assistant's text content, normalized (thinking/reasoning stripped).
    Used by the public completion() and as fallback for completion_stream().
    """
    url = _local_chat_url()
    payload = _local_completion_payload(
        system, user_message, temperature=temperature, max_tokens=max_tokens
    )
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    data: object = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Local LLM response not a dict: {type(data)}")
    choices: object = data.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise ValueError("Local LLM response has no choices")
    first: object = choices[0]
    if not isinstance(first, dict):
        raise ValueError("Local LLM first choice not a dict")
    msg = first.get("message", {})
    if not isinstance(msg, dict):
        raise ValueError("Local LLM message not a dict")
    return _normalize_openai_message_content(msg)


async def _local_completion_stream(
    system: str,
    user_message: str,
    *,
    temperature: float = 1.0,
    max_tokens: int = 16000,
) -> AsyncGenerator[LLMChunk, None]:
    """Stream completion from local server; maps delta.content / delta.reasoning_content to LLMChunk.

    If the server does not support streaming (4xx, invalid SSE, etc.), falls back
    to a single completion and yields one content chunk so the contract is always satisfied.
    """
    url = _local_chat_url()
    payload = _local_completion_payload(
        system,
        user_message,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                yielded_any = False
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    choices_inner: object = data.get("choices", [])
                    if not isinstance(choices_inner, list) or not choices_inner:
                        continue
                    first_inner: object = choices_inner[0]
                    if not isinstance(first_inner, dict):
                        continue
                    delta: object = first_inner.get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    # reasoning_content (e.g. some servers) → thinking
                    reasoning: object = delta.get("reasoning_content") or delta.get(
                        "reasoning"
                    )
                    if isinstance(reasoning, str) and reasoning:
                        yielded_any = True
                        yield LLMChunk(type="thinking", text=reasoning)
                    # content → content
                    content_part: object = delta.get("content")
                    if isinstance(content_part, str) and content_part:
                        yielded_any = True
                        yield LLMChunk(type="content", text=content_part)
                if yielded_any:
                    return
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
        logger.debug(
            "⚠️ Local LLM stream failed, falling back to one-shot: %s", exc
        )
    # Fallback: single completion, one content chunk
    text = await call_local_completion(
        system,
        user_message,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    yield LLMChunk(type="content", text=text)
