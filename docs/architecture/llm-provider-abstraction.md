# Model-agnostic LLM architecture — planning

**Goal:** AgentCeption should support plugging in any model (Anthropic, Qwen/local, future providers) without tight coupling. Streaming, chain-of-thought, and response shape must be in a **universal format** the rest of the app consumes. Operators should be able to swap providers via config, not code.

**Status:** Phases 1–5 implemented. Public API, callers wired, provider selection in config, local adapter behind contract; documentation and deployment guide complete.

---

## 1. Current state

### 1.1 How the app uses LLMs

| Use case | Entry point | Contract today |
|----------|-------------|-----------------|
| **Plan generation (Phase 1A)** | `llm_phase_planner.generate_plan_yaml` | Single-turn: `(user_prompt, system_prompt) → full text`. Non-streaming for MCP; streaming for Build UI preview. |
| **Plan preview (streaming)** | `plan_ui._llm_stream()` | Async generator of chunks. UI **discards** `thinking`, **accumulates** `content`, then validates YAML. |
| **Agent loop (tools)** | `agent_loop` | Multi-turn with tools: `(messages, tools, system) → ToolResponse` (text + tool_calls + stop_reason). Used for server-side agent runs. |
| **Recon / one-off** | `agent_loop` (recon phase) | Single-turn no tools: same as plan, returns text. |

So the app needs:

- **Single completion:** `(prompt, system?) → str` (optionally with thinking discarded).
- **Streaming completion:** `AsyncGenerator[Chunk]` where each chunk has `type: "thinking" | "content"` and `text: str`. Callers discard thinking and accumulate content.
- **Tool-use completion:** `(messages, tools, system) → ToolResponse` (OpenAI-like: content + tool_calls + usage).

### 1.2 Where providers differ today

| Aspect | Anthropic | Local (OpenAI-compatible / Qwen) |
|--------|-----------|-----------------------------------|
| **Auth** | API key header | None or API key (server-dependent) |
| **URL** | `api.anthropic.com` | Configurable base URL + path |
| **Request shape** | Messages API, `thinking: { budget_tokens }` | OpenAI `messages`, `enable_thinking` (often ignored) |
| **Response (non-stream)** | `content: [{ type: "text", text }]` | `choices[0].message.content` (string or list) |
| **Response (stream)** | `content_block_delta` with `thinking_delta` vs `text_delta` | `delta.content`, `delta.reasoning_content` (often null) |
| **Thinking** | API-separated; we only use `text` blocks / `text_delta` | Often concatenated in `content`; no standard separation |

The “chain of thought” problem: with Anthropic we get thinking and content **separately** in the API. With many local servers everything is in one `content` string, so we had to add extraction heuristics (e.g. strip “Thinking Process”, find ```yaml, <think>/</think>). That is fragile and provider-specific.

---

## 2. What “model agnostic” and “universal format” mean

- **Model agnostic:** The rest of the app (plan_ui, llm_phase_planner, agent_loop) does not call `call_anthropic_*` or `call_local_*` directly. It calls a **single set of entry points** (e.g. `llm.completion`, `llm.completion_stream`, `llm.completion_with_tools`) that are backed by whichever provider is configured.
- **Universal format (internal):**
  - **Completion:** `str` (final answer only). Any thinking is stripped or never returned by the adapter.
  - **Streaming:** `AsyncGenerator[LLMChunk]` with `LLMChunk = { type: "thinking" | "content", text: str }`. Downstream always discards `thinking` and accumulates `content`.
  - **Tool-use:** Existing `ToolResponse` (content, tool_calls, stop_reason, usage) — already abstract enough.

So we define an **AgentCeption LLM contract**: the same types and function signatures regardless of provider. Providers are responsible for mapping their API and response shape into this contract (including normalizing or stripping CoT).

---

## 3. Options

### 3.1 Adopt industry format (OpenAI/Anthropic) and push everything through it

- **Idea:** Treat “OpenAI format” as the universal wire format. Use a gateway or library that converts every provider to OpenAI-shaped requests/responses.
- **Pros:** One format; ecosystem tooling (LiteLLM, proxies) exists.
- **Cons:** Our main production path is Anthropic; we’d be converting Anthropic → OpenAI shape then back. Streaming “thinking” is not fully standardized across providers; we’d still need a single internal notion of “thinking vs content” for the UI.

### 3.2 Define an AgentCeption format; adapters per provider

- **Idea:** Keep a small **internal** contract (completion → str, stream → `LLMChunk`, tools → `ToolResponse`). Each provider has an **adapter** that talks to the real API and returns only this contract. Thinking/CoT is handled inside the adapter (e.g. strip, or map `reasoning_content` → thinking chunks).
- **Pros:** No dependency on a third-party abstraction; we own the contract; we can add providers (OpenAI, Azure, etc.) without changing callers.
- **Cons:** We maintain adapters and any normalization (e.g. “extract YAML from blob”) in our code or in the server.

### 3.3 Use LiteLLM (or similar) for “other” providers only

- **Idea:** Keep Anthropic direct (as today). Use LiteLLM for “local” and any non-Anthropic provider. LiteLLM normalizes to OpenAI-style; we then map LiteLLM’s response (and optional `reasoning_content` / thinking) into our `LLMChunk` stream and completion string.
- **Pros:** Less custom code for many backends; LiteLLM handles URL, auth, retries, streaming for 100+ providers.
- **Cons:** Two code paths (Anthropic native vs LiteLLM); we must still define how we consume LiteLLM’s output (e.g. `reasoning_content` in chunks) so the rest of the app stays model-agnostic.

### 3.4 Recommendation

- **Preferred:** **3.2 (AgentCeption format + adapters)** with an option to **use LiteLLM inside the “local” adapter** so we don’t reimplement every OpenAI-compatible and open-weight backend.
  - **Internal contract:** `completion()`, `completion_stream()`, `completion_with_tools()` with the types above. No `call_anthropic_*` or `call_local_*` in plan_ui, llm_phase_planner, or agent_loop.
  - **Adapters:**  
    - **Anthropic adapter:** Current Anthropic API; map `content` blocks and `thinking_delta`/`text_delta` into our completion string and `LLMChunk` stream.  
    - **Local / OpenAI-compatible adapter:** Either (a) current httpx calls + response normalization (e.g. content as string or list; extract thinking if present), or (b) LiteLLM with a fixed “OpenAI” output contract and map LiteLLM’s streaming/completion into our stream/str.  
  - **Config:** One place (e.g. `LLM_PROVIDER=anthropic | local | openai`) plus provider-specific env (API keys, base URL, model name). No `use_local_llm` branching in business logic; only in the adapter selection.

This gives a clean “plug in a model” story: add a new adapter and config value; the rest of the app stays unchanged and always uses the universal format.

---

## 4. Target architecture (high level)

```
┌─────────────────────────────────────────────────────────────────┐
│  plan_ui, llm_phase_planner, agent_loop                         │
│  (only call llm.completion / completion_stream / completion_     │
│   with_tools; never call provider-specific functions)             │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  AgentCeption LLM layer (agentception/services/llm.py or         │
│  agentception/llm/)                                              │
│  - completion(prompt, system?, ...) → str                        │
│  - completion_stream(prompt, system?, ...) → AsyncIter[Chunk]   │
│  - completion_with_tools(messages, tools, system, ...) →          │
│    ToolResponse                                                  │
│  Chunk = { type: "thinking" | "content", text: str }             │
│  (thinking is optional; callers discard it for “final answer”)   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                ┌───────────────┴───────────────┐
                ▼                               ▼
┌───────────────────────────┐   ┌───────────────────────────────┐
│  Anthropic adapter        │   │  Local / OpenAI adapter        │
│  - Anthropic API          │   │  - httpx to configurable URL   │
│  - content[] + stream     │   │  - or LiteLLM completion()     │
│    deltas → Chunk / str   │   │  - content/reasoning_content  │
│  - tools → ToolResponse   │   │    → Chunk / str               │
└───────────────────────────┘   │  - optional: server-side split │
                                │    (e.g. </think> → content)        │
                                └───────────────────────────────┘
```

- **Single completion:** Adapter returns only the final answer string (thinking already stripped or never requested).
- **Streaming:** Adapter yields `Chunk`; if the provider sends thinking and content separately, map them to `type: "thinking"` and `type: "content"`; if the provider sends one blob, adapter can strip/normalize before yielding (e.g. only yield `content` after </think> or after extracting YAML for plan).

---

## 5. Config and provider selection

- **Single source of provider choice:** e.g. `LLM_PROVIDER=anthropic` (default) or `local` (or `openai` if we add it). Deprecate or map `USE_LOCAL_LLM=true` → `LLM_PROVIDER=local`.
- **Provider-specific settings** (env or config file):
  - **anthropic:** `ANTHROPIC_API_KEY`, model names, timeouts, retries (existing).
  - **local:** `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_CHAT_PATH`, `LOCAL_LLM_MODEL`, `LOCAL_LLM_MAX_TOKENS`, etc. Optional: “response normalizer” (e.g. server returns `content` + `reasoning_content`; or we strip </think> on the client).
- **Plan vs agent:** Today plan can use “local” while agent uses Anthropic (or both local). We can keep “plan provider” and “agent provider” as two config knobs (e.g. `LLM_PROVIDER_PLAN`, `LLM_PROVIDER_AGENT`) both defaulting to `LLM_PROVIDER`, or a single provider for everything. Decision can follow this refactor.

---

## 6. Streaming and chain-of-thought (universal format)

- **Contract:** Stream is always `AsyncGenerator[LLMChunk]` with `LLMChunk = { type: "thinking" | "content", text: str }`.
- **Consumer rule:** Plan UI (and any other consumer) **discards** chunks with `type == "thinking"` and **accumulates** `type == "content"`. Validation (e.g. PlanSpec YAML) is applied only to the accumulated content.
- **Adapter responsibility:**  
  - Anthropic: map `thinking_delta` → `LLMChunk(type="thinking", ...)`, `text_delta` → `LLMChunk(type="content", ...)`.  
  - Local: if the server streams `delta.reasoning_content` and `delta.content` separately, map them to thinking/content chunks; if the server sends one blob in `content`, the adapter can either (a) buffer and split on </think> and then stream only content, or (b) yield one chunk with the full blob as content and document that “no thinking” is available for that provider.  
  So “chain of thought” is **always** represented the same way to the app (thinking vs content chunks); how each provider produces that is the adapter’s concern.

---

## 7. Existing open-source use

- **LiteLLM:** Call 100+ LLMs with one OpenAI-like API; supports streaming and has a notion of `reasoning_content` / thinking. We can use it **inside** the local adapter to talk to OpenAI-compatible and other backends, then map LiteLLM’s response/chunks into our `LLMChunk` and completion string. We do **not** need to route Anthropic through LiteLLM unless we want to.
- **Alternative:** Keep current httpx-based local client and invest in **server-side** normalization (e.g. server splits on </think> and returns only final answer in `content`, or returns `content` as list of parts with `type: "reasoning"` vs `"text"`). Then the adapter stays thin: read `content` (and optional `reasoning_content`), map to our format.

---

## 8. Concrete steps (implementation order)

1. **Define the public API and types**  
   In `agentception/services/llm.py` (or a new `agentception/llm/` package):  
   - `completion(...) → str`  
   - `completion_stream(...) → AsyncGenerator[LLMChunk, None]`  
   - `completion_with_tools(...) → ToolResponse`  
   - `LLMChunk` and any config types (e.g. `LLMProvider`) in one place.

2. **Implement Anthropic adapter**  
   Move current `call_anthropic`, `call_anthropic_stream`, `call_anthropic_with_tools` behind an adapter that implements the above. No change in behavior; just call the adapter from the new public API when `LLM_PROVIDER=anthropic`.

3. **Implement Local adapter** ✅  
   Implemented: Local adapter in `agentception/services/llm.py` (same contract as Anthropic).  
   - **Content normalization:** `_normalize_openai_message_content()` — `choices[0].message.content` as string or list of parts; reasoning parts stripped, text parts concatenated to final answer.  
   - **Completion:** `call_local_completion()` with temperature/max_tokens; used by public `completion()` and as stream fallback.  
   - **Streaming:** `_local_completion_stream()` — POST with `stream: true`, parse SSE, map `delta.content` / `delta.reasoning_content` to `LLMChunk`; on failure or unsupported server, fall back to one-shot and yield one content chunk.  
   - **Tools:** `call_local_with_tools()` uses same normalizer for message content.  
   - **Callers:** `/api/local-llm/hello` uses public `completion()`; no direct `call_local_*` outside llm.py.  
   - Optionally (later): use LiteLLM inside the local adapter for 100+ backends.

4. **Provider selection in config** ✅  
   Implemented: `LLM_PROVIDER` (env: `anthropic` | `local`, default `anthropic`) and `effective_llm_provider` (property: `USE_LOCAL_LLM=true` → local overrides). In the LLM layer, `completion`, `completion_stream`, and `completion_with_tools` branch only on `settings.effective_llm_provider`; no provider-specific logic in plan_ui, llm_phase_planner, or agent_loop.

5. **Extract “response normalization” for plan**  
   Move “extract YAML from blob” (<think>/</think>, ```yaml, “Thinking Process” strip) into the **local adapter** for the plan use case, so the adapter returns (or streams) only the final YAML when possible. Alternatively, keep extraction in the reader but document that the adapter should return “content” that is as close to final answer as the server allows; then we have one place (adapter vs reader) for that policy.

6. **Document and test** ✅  
   - **Contract:** [LLM contract and provider abstraction](../reference/llm-contract.md) — entry points, types, provider selection, how to add a provider.  
   - **Tests:** `test_llm.py` and `test_config.py` cover provider selection and adapter behaviour; plan and agent_loop tests use the public API only.  
   - **Deployment:** [Local LLM / Ollama](../guides/local-llm.md) — config table, how the local adapter works, Ollama runbook; setup and security guides updated for both providers.

---

## 9. Summary

| Question | Answer |
|----------|--------|
| Correct architecture? | **Single internal contract (completion, stream, tools) + one adapter per provider.** Rest of app only talks to the contract. |
| Industry format? | Use **internally** as the contract: streaming = chunks with `type: "thinking" \| "content"`; completion = final answer string. Map provider responses into this; no need to force everything through OpenAI’s wire format unless we use LiteLLM. |
| Existing library? | **LiteLLM** is a good fit for the “local” (and future non-Anthropic) adapter to talk to 100+ backends; we still map its output into our Chunk/str/ToolResponse. |
| Massage Qwen into Anthropic/OpenAI format? | Either (a) **server-side:** server returns only final answer in `content` or splits </think> and populates `content`/`reasoning_content`, or (b) **client-side:** in the local adapter, normalize the blob (e.g. extract after </think> or last ```yaml) and return that as the completion string / content chunks. |
| How to keep it clean? | No `call_anthropic_*` or `call_local_*` in UI/planner/agent_loop; one config-driven adapter selection; thinking/content always represented the same way in the stream. |

This gives a clear path to “plug in any model” without tying AgentCeption to Anthropic or to the Qwen API, while keeping streaming and chain-of-thought in a universal format.
