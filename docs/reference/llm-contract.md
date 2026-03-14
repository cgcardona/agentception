# LLM Contract and Provider Abstraction

AgentCeption uses a **single internal LLM contract** so that plan generation, streaming preview, and the agent loop never depend on a specific provider. All callers use the same three entry points; provider selection is config-driven and implemented inside `agentception/services/llm.py`.

---

## 1. The contract

Callers (plan UI, phase planner, agent loop) **must** use only these three functions. They must **not** call `call_anthropic_*` or `call_local_*` or any other provider-specific API.

### 1.1 Entry points

| Function | Signature (summary) | Returns | Used by |
|----------|--------------------|---------|--------|
| `completion(user_prompt, *, system_prompt=..., temperature=..., max_tokens=..., json_schema=...)` | Single-turn, no tools | `str` — final answer only (thinking/reasoning stripped by adapter) | Phase planner (MCP), recon step, `/api/local-llm/hello` |
| `completion_stream(user_prompt, *, system_prompt=..., temperature=..., max_tokens=..., reasoning_fraction=...)` | Streaming, no tools | `AsyncGenerator[LLMChunk, None]` | Plan UI (Build preview) |
| `completion_with_tools(messages, *, system, tools, model=..., temperature=..., max_tokens=..., extra_system_blocks=..., session=..., run_id=..., iteration=...)` | Multi-turn with tools | `ToolResponse` | Agent loop (developer and reviewer turns) |

**Source:** `agentception/services/llm.py`. Import from there; do not reach into adapter internals.

### 1.2 Types

| Type | Description |
|------|-------------|
| `LLMChunk` | `TypedDict` with `type: Literal["thinking", "content"]` and `text: str`. Stream consumers **discard** `thinking` and **accumulate** `content` to get the final answer. |
| `ToolDefinition` | OpenAI-format tool spec: `type: "function"`, `function: { name, description, parameters }`. |
| `ToolResponse` | `stop_reason: str` (`"stop"` \| `"tool_calls"` \| `"length"`), `content: str`, `tool_calls: list[ToolCall]`, plus optional `input_tokens`, `output_tokens`, cache fields. |

Thinking vs content is **always** represented the same way to the app: stream chunks have `type: "thinking"` or `type: "content"`. How each provider produces that (e.g. Anthropic `thinking_delta` vs local `delta.reasoning_content`) is the adapter’s concern.

### 1.3 Consumer rules

- **Do not** branch on provider in plan_ui, llm_phase_planner, or agent_loop. Branch only on the **result** (e.g. stop_reason, chunk type).
- **Do not** pass provider-specific options through the public API. Adapters read what they need from `agentception/config.py` settings.
- **Do** handle both chunk types in streams: discard `thinking`, accumulate `content`, then validate or persist the accumulated string.

---

## 2. Provider selection

Provider choice is **single source**: config, not code.

| Setting | Env var | Values | Default | Notes |
|---------|---------|--------|---------|--------|
| `llm_provider` | `LLM_PROVIDER` | `anthropic`, `local` | `anthropic` | Preferred knob for “which backend”. |
| `use_local_llm` | `USE_LOCAL_LLM` | `true`, `false` | `false` | When `true`, **overrides** `llm_provider` to `local` (backward compatibility). |
| **Effective** | — | — | — | `settings.effective_llm_provider`: if `use_local_llm` then `local`, else `llm_provider`. |

Inside the LLM layer, **only** `effective_llm_provider` is used to decide which adapter to call. No other module should branch on `use_local_llm` for “which LLM”; use `effective_llm_provider == LLMProviderChoice.local` (or `.anthropic`) when you must (e.g. local-only routes or context truncation for local).

---

## 3. Adapters (internal)

Adapters live inside `agentception/services/llm.py`. They are **not** part of the public API.

| Provider | Completion | Stream | Tools |
|----------|------------|--------|-------|
| **Anthropic** | `call_anthropic()` | `call_anthropic_stream()` | `call_anthropic_with_tools()` |
| **Local** (Chat Completions–compatible HTTP on the host) | `call_local_completion()` | `_local_completion_stream()` (SSE or one-shot fallback) | `call_local_with_tools()` |

The public entry points branch on `settings.effective_llm_provider` and call the corresponding adapter. The local adapter talks to **your** server (e.g. MLX) using the same **request/response shape** as OpenAI’s Chat Completions API—“OpenAI” here means **wire format**, not OpenAI Inc.; no cloud call is required. Responses are normalized (e.g. `content` as string or list of parts; reasoning stripped) so the contract is always satisfied.

---

## 4. How to add a new provider

To add a new backend (e.g. OpenAI, Azure, or another API):

1. **Implement the three behaviours** inside `llm.py` (or a dedicated adapter module if you extract later):
   - **Completion:** function that takes the same logical inputs as `completion()` and returns a single `str` (final answer only).
   - **Stream:** async generator that yields `LLMChunk`; map the provider’s stream format to `type: "thinking"` and `type: "content"`. If the provider does not support streaming, do a single completion and yield one `LLMChunk(type="content", text=...)`.
   - **Tools:** function with the same signature as `completion_with_tools` that returns `ToolResponse` (content, tool_calls, stop_reason, optional usage).

2. **Add config:**
   - In `agentception/config.py`, add a new value to `LLMProviderChoice` (e.g. `openai = "openai"`).
   - Add any provider-specific settings (base URL, API key, model name) and document their env vars.

3. **Wire the branch:** In `completion()`, `completion_stream()`, and `completion_with_tools()`, add a branch for the new provider (e.g. `elif settings.effective_llm_provider == LLMProviderChoice.openai: ...`). Keep the list of providers in one place; do not scatter provider checks in callers.

4. **Document:** Update this reference, the [architecture doc](architecture/llm-provider-abstraction.md), and any deployment or setup guides that list env vars.

5. **Test:** Add tests that mock `settings.effective_llm_provider` and assert the correct adapter is called (see `agentception/tests/test_llm.py` for the pattern).

---

## 5. Related docs

| Doc | Content |
|-----|--------|
| [LLM provider abstraction (architecture)](../architecture/llm-provider-abstraction.md) | Rationale, options, target architecture, implementation steps. |
| [Local LLM with MLX (deployment)](../guides/local-llm-mlx.md) | End-to-end local provider setup: config, env vars, MLX server, probes. |
| [Type contracts — LLM Service Types](type-contracts.md#llm-service-types) | TypedDict and function signatures in detail. |
| [Setup](../guides/setup.md) | First-run env vars; optional local LLM subsection. |
