# Local LLM with Ollama

This guide explains how to run a local inference server and connect it to AgentCeption's **local provider**. **Ollama is the recommended backend.**

AgentCeption works on **macOS, Linux, and Windows** with any [Ollama](https://ollama.com)-compatible server — no API key, no cloud bill, no data leaving your machine.

**Recommended hardware (any platform):**
- 8 GB RAM minimum (4B parameter models)
- 16–24 GB for 9B models (good for real coding tasks)
- 48 GB+ for 35B models (best planning quality)

GPU acceleration is automatic:
- **Apple Silicon** — Metal
- **NVIDIA** — CUDA
- **AMD** — ROCm
- **CPU-only** — works, but slower

AgentCeption uses a **provider-agnostic LLM contract**. When the effective provider is **local**, all LLM calls (planning, streaming preview, agent loop) go to your Chat Completions–compatible HTTP server instead of Anthropic. See [LLM contract and provider abstraction](../reference/llm-contract.md) for the full contract and config model.

### Naming: "OpenAI" in tooling — not OpenAI's cloud

You will see names like **OpenAI-compatible** and the package **`mlx-openai-server`**. Here is what that means:

| Phrase / name | Meaning |
|---------------|--------|
| **OpenAI-compatible / Chat Completions API** | The HTTP shape many tools use: `POST /v1/chat/completions`, JSON body with `messages`, response with `choices[].message`. It is a **wire format** — not a claim that traffic goes to OpenAI Inc. |
| **Ollama** | Production-grade local inference server. Implements the OpenAI-compatible API. **Recommended.** |
| **`mlx-openai-server`** | Developer-preview local MLX server (Apple Silicon only). Single-process, no request queue, KV cache saturates after large generations. **For development and one-off tests only.** |

So: **no OpenAI account or cloud call is required** for the local provider. You are the datacenter; the "OpenAI" wording is only about **request/response shape**, so the same client code can talk to many backends.

---

## Config and environment

Provider selection is the **single source of truth** in config. Use one of the following.

| Env var | Values | Default | Purpose |
|---------|--------|---------|--------|
| `LLM_PROVIDER` | `anthropic`, `local` | `anthropic` | Which LLM backend to use. Set to `local` to use the local server for all LLM calls (planning, streaming, agent loop). |
| `USE_LOCAL_LLM` | `true`, `false` | `false` | **Legacy.** When `true`, overrides `LLM_PROVIDER` to `local`. Prefer `LLM_PROVIDER=local` for new setups. |

When the effective provider is **local**, the following env vars apply. Set them in `.env` or in `docker-compose.override.yml` under `agentception.environment`.

| Env var | Default | Purpose |
|---------|---------|--------|
| `LOCAL_LLM_BASE_URL` | `http://host.docker.internal:11434` | Base URL of your local Chat Completions–compatible server (no trailing slash). From Docker, use `host.docker.internal` to reach a server on the host. Ollama default port is **11434**. |
| `LOCAL_LLM_CHAT_PATH` | `/v1/chat/completions` | Path appended to the base URL for chat completions. Ollama exposes this path. |
| `LOCAL_LLM_MODEL` | *(empty)* | Model name sent in requests. For Ollama, set this to the exact tag (e.g. `qwen2.5-coder:7b`). If empty, some servers use their loaded model; Ollama requires a model name. |
| `LOCAL_LLM_MAX_CONTEXT_CHARS` | `12000` | Max characters for the first user message (task briefing) when using the local LLM. Reduces load on small models. |
| `LOCAL_LLM_MAX_SYSTEM_CHARS` | `6000` | Max characters for the system prompt (role + cognitive arch). Truncation is applied when using the local provider. |
| `LOCAL_LLM_MAX_TOKENS` | `4096` | Desired max completion tokens per turn (agent loop). Never sent above the ceiling below. |
| `LOCAL_LLM_COMPLETION_TOKEN_CEILING` | `8192` | Hard cap on `max_tokens` in every local chat request. Ollama supports 8192+; lower to 4096 only if you are using `mlx-openai-server` (which returns 422 above 4096). |

### Per-usecase model overrides (Phase 3 — two models)

When routing through LiteLLM Proxy with separate model instances for planning and agent tool calls:

| Env var | Default | Purpose |
|---------|---------|--------|
| `LOCAL_LLM_BASE_URL_PLAN` | *(empty, falls back to `LOCAL_LLM_BASE_URL`)* | Base URL for Phase 1A planning/streaming calls only. |
| `LOCAL_LLM_MODEL_PLAN` | *(empty, falls back to `LOCAL_LLM_MODEL`)* | Model name for planning calls. Route to the large 35B model. |
| `LOCAL_LLM_BASE_URL_AGENT` | *(empty, falls back to `LOCAL_LLM_BASE_URL`)* | Base URL for agent tool-call turns only. |
| `LOCAL_LLM_MODEL_AGENT` | *(empty, falls back to `LOCAL_LLM_MODEL`)* | Model name for agent calls. Route to the fast 8B model. |

See [Local LLM Scaling](local-llm-scaling.md) for the full multi-agent scaling guide.

**Effective provider:** If `USE_LOCAL_LLM=true`, the effective provider is **local** regardless of `LLM_PROVIDER`. Otherwise the effective provider is the value of `LLM_PROVIDER`. Only the effective provider is used when deciding which adapter to call.

---

## How the local adapter works

The local adapter in `agentception/services/llm.py` implements the same **contract** as the Anthropic path:

- **Completion:** Single request to your server; response `choices[0].message.content` is normalized (string or list of parts; reasoning parts stripped) and returned as a single final-answer string.
- **Streaming:** If the server supports `stream: true`, the adapter parses SSE and maps `delta.content` / `delta.reasoning_content` to `LLMChunk(type="content", ...)` and `LLMChunk(type="thinking", ...)`. Models that embed `<think>...</think>` tags in `content` (Qwen3, DeepSeek) are handled by `_normalize_think_tags`, which reclassifies the enclosed text as `thinking` chunks transparently. If streaming is not supported or fails, it falls back to one completion and yields one content chunk so the contract is always satisfied.
- **Tools:** Same request/response shape as completion but with `tools` and `tool_choice`; response content and `tool_calls` are normalized to the shared `ToolResponse` type.

No caller code (plan UI, phase planner, agent loop) branches on provider; they all use `completion()`, `completion_stream()`, or `completion_with_tools()`. See [LLM contract](../reference/llm-contract.md) for details.

---

## Recommended setup: Ollama

Ollama is the recommended local inference backend. It is production-grade:

- **Built-in request queue** — handles concurrent requests without crashing
- **Model keep-alive** — model stays loaded between requests; no restart needed
- **KV cache management** — proper context window enforcement; returns an error cleanly if context is exceeded, rather than hanging
- **Prefix caching** — if multiple agents share the same system prompt prefix, the KV computation for that prefix is cached and reused
- **OpenAI-compatible API** — AgentCeption's `agentception/services/llm.py` works unchanged; only config values change

### Install Ollama

```bash
# macOS (Homebrew)
brew install ollama && brew services start ollama

# Linux (one-line installer)
curl -fsSL https://ollama.com/install.sh | sh

# Windows
# Download the installer from https://ollama.com/download
```

### Pull a model

```bash
# Qwen 2.5 Coder 7B — fast, good quality (~4 GB), works on 8 GB+ RAM
ollama pull qwen2.5-coder:7b

# Qwen 3.5 35B (MoE, 4-bit) — best quality; requires ~20 GB RAM for weights
ollama pull qwen3.5:35b-a3b-q4_K_M

# Qwen 3.5 9B (4-bit) — good for real coding tasks, 16–24 GB RAM
ollama pull qwen3.5:9b

# Qwen 3.5 4B (4-bit) — for 8–16 GB RAM
ollama pull qwen3.5:4b
```

### Start the Ollama server

```bash
# macOS/Linux: start in the background (listens on 0.0.0.0:11434 by default)
ollama serve

# Windows: Ollama runs as a system service after installation; no manual start needed.
```

Ollama binds to `0.0.0.0:11434` by default, so Docker containers reach it at `host.docker.internal:11434`.

### Configure AgentCeption

In `.env`:

```bash
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://host.docker.internal:11434
LOCAL_LLM_CHAT_PATH=/v1/chat/completions
LOCAL_LLM_MODEL=qwen2.5-coder:7b
LOCAL_LLM_COMPLETION_TOKEN_CEILING=8192
LOCAL_LLM_MAX_CONTEXT_CHARS=24000
LOCAL_LLM_MAX_SYSTEM_CHARS=12000
LOCAL_LLM_MAX_TOKENS=8192
```

Then restart AgentCeption:

```bash
docker compose restart agentception
```

### Verify

```bash
# From the host
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [{"role": "user", "content": "Reply with exactly: hello world"}],
    "temperature": 0.0,
    "max_tokens": 32,
    "stream": false
  }'

# From inside Docker
docker compose exec agentception curl -s http://host.docker.internal:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-coder:7b","messages":[{"role":"user","content":"Say hi"}],"temperature":0,"max_tokens":16,"stream":false}'

# Confirm AgentCeption reaches the local model
curl -s http://localhost:10003/api/local-llm/hello
# Expect: {"ok": true, "reply": "..."}
```

---

## AgentCeption integration

When Ollama is running, point the agent at it so it uses the local model instead of Anthropic.

1. **Start Ollama on the host:**

   ```bash
   ollama serve
   ```

2. **Enable the local provider** in AgentCeption:

   - Set `LLM_PROVIDER=local` (or `USE_LOCAL_LLM=true`) in `.env`.
   - Set `LOCAL_LLM_BASE_URL=http://host.docker.internal:11434`.
   - Set `LOCAL_LLM_MODEL` to the pulled model tag.
   - Restart: `docker compose restart agentception`.

3. **Probe the local model (no agent):**

   ```bash
   curl -s http://localhost:10003/api/local-llm/hello
   ```

   You should get `{"ok": true, "reply": "..."}`. If you get 502, the container cannot reach Ollama; check `host.docker.internal` and that Ollama is running on the host.

4. **Optional: run an agent that says "hello world":**

   ```bash
   curl -s -X POST http://localhost:10003/api/local-llm/hello-agent
   ```

   Returns `{"run_id": "local-hello-<uuid>", "status": "implementing"}`. Watch with `python3 scripts/watch_run.py local-hello-<uuid>`.

5. **Dispatch a developer agent** as usual (Build dashboard or `POST /api/dispatch/issue`). The local path uses the same pipeline as Anthropic: full system prompt, same tools, same task briefing. Only the LLM endpoint and context caps differ.

6. **Turn off** when done: set `LLM_PROVIDER=anthropic` (or remove `USE_LOCAL_LLM`) and restart.

---

## Model choice by available RAM

| RAM | Model | Ollama tag | Notes |
|-----|-------|------------|-------|
| 8–16 GB | Qwen 2.5 Coder 7B | `qwen2.5-coder:7b` | Fast; good for coding tasks |
| 8–16 GB | Qwen 3.5 4B | `qwen3.5:4b` | Fast; limited context |
| 16–24 GB | Qwen 3.5 9B | `qwen3.5:9b` | Good for real coding tasks |
| 48 GB+ | Qwen 3.5 35B (MoE) | `qwen3.5:35b-a3b-q4_K_M` | Best quality; preferred for planning |

**Recommended context caps by model:**

```bash
# Qwen 3.5 9B (16–24 GB)
LOCAL_LLM_MAX_CONTEXT_CHARS=24000
LOCAL_LLM_MAX_SYSTEM_CHARS=12000
LOCAL_LLM_MAX_TOKENS=8192

# Qwen 3.5 4B / Qwen 2.5 Coder 7B (8–16 GB) — more conservative
LOCAL_LLM_MAX_CONTEXT_CHARS=12000
LOCAL_LLM_MAX_SYSTEM_CHARS=6000
LOCAL_LLM_MAX_TOKENS=4096
```

---

## Capturing resource usage

### macOS — Activity Monitor and powermetrics

Use **Activity Monitor** for a quick visual view: Window → CPU History, GPU History.

For scripted logs, use **powermetrics** (requires `sudo`):

```bash
# Sample every 1 second, 60 samples (~1 minute)
sudo powermetrics -i 1000 -n 60 -o powermetrics_run.txt -s cpu_power,gpu_power,ane_power
```

Useful samplers: `cpu_power`, `gpu_power`, `ane_power` (Apple Neural Engine), `all`.

### Linux — nvidia-smi / rocm-smi

```bash
# NVIDIA
watch -n 1 nvidia-smi

# AMD
watch -n 1 rocm-smi
```

### Windows

Use **Task Manager → Performance** for a quick view, or install **GPU-Z** / **HWiNFO** for detailed GPU metrics.

---

## Scaling beyond one agent

For multi-agent workloads (10+ concurrent agents), add LiteLLM Proxy as a routing and load-balancing layer between AgentCeption and Ollama. See [Local LLM Scaling](local-llm-scaling.md).

---

## Verify with curl (generic)

These work for any OpenAI-compatible backend (Ollama or otherwise). Replace the URL and model name to match your setup.

**1. Minimal request:**

```bash
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Reply with exactly: hello world"}
    ],
    "temperature": 0.2,
    "max_tokens": 128,
    "stream": false
  }'
```

**2. From inside Docker** (to match AgentCeption's network path):

```bash
docker compose exec agentception curl -s http://host.docker.internal:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-coder:7b","messages":[{"role":"user","content":"Say hi"}],"temperature":0,"max_tokens":64,"stream":false}'
```

If the host call works but the Docker call fails, check that Ollama is not bound only to `127.0.0.1`. Ollama binds to `0.0.0.0` by default, so this should work without extra config.

---

## Apple Silicon: MLX (developer footnote)

`mlx-openai-server` is an Apple Silicon–only developer preview tool. It is **not suitable for multi-agent workloads** because:

- Single-process, no request queue — one request at a time
- No KV cache management — cache saturates after a large generation (~4000 tok); subsequent requests hang until server restart
- Hard max-token cap of 4096 (returns HTTP 422 above this)
- No model keep-alive between requests

Use it only for one-off tests or model exploration on a Mac that does not have Ollama installed. If you must use it, set `LOCAL_LLM_COMPLETION_TOKEN_CEILING=4096` and expect to restart it between large generations.

**Quick start (mlx-openai-server, developer use only, Apple Silicon):**

```bash
pip install -U mlx-openai-server
# For the 35B 4-bit model, also upgrade mlx-vlm:
pip install -U "mlx-vlm>=0.3.12"

mlx-openai-server launch \
  --model-path mlx-community/Qwen3.5-35B-A3B-4bit \
  --model-type multimodal \
  --host 0.0.0.0 \
  --port 8080
```

Then set `LOCAL_LLM_BASE_URL=http://host.docker.internal:8080` and `LOCAL_LLM_COMPLETION_TOKEN_CEILING=4096` in `.env`.

---

## Sample coding task (test code generation)

Use the following as a GitHub issue to see what code the local model can produce. Create the issue, ensure the effective provider is **local** (`LLM_PROVIDER=local` or `USE_LOCAL_LLM=true`), then dispatch a developer and watch with `python3 scripts/watch_run.py issue-<N>`.

**Title:** `Add clamp() helper and test (local LLM code gen)`

**Body:**

```markdown
## Context
Small code-generation test for the local LLM (Qwen) pipeline.

## Objective
1. Create `agentception/utils.py` with a single function:
   - `def clamp(value: float, low: float, high: float) -> float`
   - Return `value` clamped to the range `[low, high]`.
   - Add `from __future__ import annotations` at the top and a one-line module docstring.
2. Create `agentception/tests/test_utils.py` with one test:
   - `def test_clamp_returns_value_within_bounds()`
   - Assert `clamp(5.0, 0.0, 10.0) == 5.0`, `clamp(-1.0, 0.0, 10.0) == 0.0`, `clamp(11.0, 0.0, 10.0) == 10.0`.

## Acceptance criteria
- [ ] `agentception/utils.py` exists with `clamp()` and type hints.
- [ ] `agentception/tests/test_utils.py` exists and `pytest agentception/tests/test_utils.py -v` passes.
```

After the run, verify: `docker compose exec agentception pytest agentception/tests/test_utils.py -v` and `mypy agentception/utils.py`.

---

## References

- [Local LLM Scaling](local-llm-scaling.md) — multi-agent scaling with LiteLLM Proxy and multiple Ollama instances.
- [LLM contract and provider abstraction](../reference/llm-contract.md) — AgentCeption's LLM contract, provider selection, and how to add a provider.
- [Ollama](https://ollama.com) — local inference server (recommended).
- [mlx-community/Qwen3.5-35B-A3B-4bit](https://huggingface.co/mlx-community/Qwen3.5-35B-A3B-4bit) — 4-bit quantized Qwen 3.5 35B for MLX (Apple Silicon only).
- [powermetrics(1)](https://keith.github.io/xcode-man-pages/powermetrics.1.html) — macOS power and usage sampling.
