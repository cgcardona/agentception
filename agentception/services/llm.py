from __future__ import annotations

"""Async OpenRouter client for AgentCeption's direct LLM calls.

Patterns adopted from maestro/core/llm_client.py:
  - Provider routing lock (Anthropic direct, no Bedrock/Vertex fallback)
  - Extended reasoning via payload["reasoning"] — yields thinking deltas
    separately from content deltas so the UI can display them differently
  - Exponential backoff retry on 429/5xx/timeout
  - Persistent httpx.AsyncClient (re-used across requests, not recreated per call)

Two public entry points:

``call_openrouter(user_prompt, ...)``
    Waits for the full completion and returns the text.  No retry for now on
    the non-streaming path (used only for MCP tools where latency matters less).

``call_openrouter_stream(user_prompt, ...)``
    AsyncGenerator that yields dicts as SSE-ready events:
      {"type": "thinking", "text": "..."}  -- reasoning token (chain of thought)
      {"type": "content",  "text": "..."}  -- output token (the actual YAML)
    Callers map these to their own SSE event format.

The key is read from ``settings.openrouter_api_key`` (env var
``AC_OPENROUTER_API_KEY``).  A missing key raises ``RuntimeError``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import TypedDict

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

    type: str   # "thinking" | "content"
    text: str


def _base_headers() -> dict[str, str]:
    """Build the shared HTTP headers for every OpenRouter request."""
    api_key = settings.openrouter_api_key
    if not api_key:
        raise RuntimeError(
            "AC_OPENROUTER_API_KEY is not configured -- "
            "set it in .env and restart the agentception service."
        )
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://agentception.local",
        "X-Title": "AgentCeption",
    }


# ---------------------------------------------------------------------------
# Persistent client (re-used across requests — matches maestro/core/llm_client.py)
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
        RuntimeError: When ``AC_OPENROUTER_API_KEY`` is not set.
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

    Provider lock, reasoning budget, and retry match maestro/core/llm_client.py.

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
                # (same pattern as maestro/core/llm_client.py).
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
