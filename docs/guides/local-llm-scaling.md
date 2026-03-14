# Local LLM Scaling with LiteLLM Proxy

This guide covers the multi-agent scaling architecture for running dozens to hundreds of AgentCeption agents against local models on Apple Silicon. It assumes you have completed the basic [Ollama setup](local-llm-mlx.md) (Phase 1) and now need to scale beyond a single Ollama instance.

---

## The fundamental constraint

Apple Silicon is unified memory — one model at a time.

| Hardware | Model fit | Realistic throughput (Qwen 3.5 35B 4-bit) |
|----------|-----------|------------------------------------------|
| M4 Max 128 GB | 1 instance (~18 GB for weights) | ~870 tok/s single-stream |
| Mac Pro M4 Ultra 192 GB | 2 instances | ~1700 tok/s across 2 streams |
| 4× Mac Studio M4 Max | 4 instances | ~3500 tok/s, true parallelism |

For agent tool calls (200–500 tok/turn at 870 tok/s): **~100–200 tool calls/minute per machine**. With a queue, a single chip can support ~5–10 concurrent agents without degradation. Beyond that, you need more hardware — there is no software fix for physics.

---

## Architecture overview

```
Agent pool (1 → N agents)
        │
        ▼
AgentCeption LLM layer  (agentception/services/llm.py)
completion() / completion_stream() / completion_with_tools()
        │
        ▼
LiteLLM Proxy  (localhost:4000)
 ├── request queue
 ├── load balancer
 └── cloud fallback (Anthropic)
        │
        ├─── Ollama instance 1  (port 11434) — Qwen 3.5 35B, planning
        ├─── Ollama instance 2  (port 11435) — Qwen 3.5 8B, agent tool calls
        └─── Anthropic API      (cloud fallback when local is saturated)
```

Each phase below is independent — implement only as far as your agent count requires.

---

## Phase 1: Single Ollama instance (1–10 agents)

**Already covered in [local-llm-mlx.md](local-llm-mlx.md).** Ollama handles a request queue natively; up to ~5–10 concurrent agents can share one Ollama instance without degradation.

Configuration:

```bash
# .env
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://host.docker.internal:11434
LOCAL_LLM_MODEL=qwen3.5:35b-a3b-q4_K_M
LOCAL_LLM_COMPLETION_TOKEN_CEILING=8192
```

---

## Phase 2: LiteLLM Proxy (10–100 agents)

LiteLLM Proxy accepts the OpenAI API shape that AgentCeption already sends and adds:

- **Request queuing** across multiple Ollama instances
- **Load balancing** — round-robin or least-busy across backends
- **Anthropic cloud fallback** when local capacity is saturated or a request fails
- **Retries and rate limiting**
- **Per-model spend and token tracking** across the fleet

The only AgentCeption config change: point `LOCAL_LLM_BASE_URL` at the proxy.

### Install LiteLLM Proxy

```bash
pip install 'litellm[proxy]'
```

Or run via Docker (see docker-compose snippet below).

### Create `litellm-config.yaml`

```yaml
# litellm-config.yaml
# Place alongside docker-compose.yml or pass via --config flag.

model_list:
  # Large model for Phase 1A planning (35B)
  - model_name: qwen-plan
    litellm_params:
      model: ollama/qwen3.5:35b-a3b-q4_K_M
      api_base: http://localhost:11434
      stream_timeout: 120

  # Fast model for agent tool calls (8B)
  - model_name: qwen-agent
    litellm_params:
      model: ollama/qwen3.5:9b
      api_base: http://localhost:11435
      stream_timeout: 60

  # Cloud fallback — used when local is saturated or fails
  - model_name: claude-fallback
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY

router_settings:
  # Retry failed requests on the next available backend
  num_retries: 2
  # Queue requests when all backends are busy (instead of 429-ing immediately)
  routing_strategy: least-busy

general_settings:
  # Expose Prometheus metrics at /metrics
  enable_prometheus: true
```

### Run LiteLLM Proxy

```bash
# Start with config
litellm --config litellm-config.yaml --port 4000
```

Or via Docker:

```yaml
# docker-compose.override.yml snippet — add to your existing file
services:
  litellm-proxy:
    image: ghcr.io/berriai/litellm:main-latest
    command: ["--config", "/config.yaml", "--port", "4000", "--num_workers", "4"]
    volumes:
      - ./litellm-config.yaml:/config.yaml:ro
    ports:
      - "4000:4000"
    environment:
      ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
```

### Configure AgentCeption to use the proxy

```bash
# .env — point at the proxy instead of Ollama directly
LOCAL_LLM_BASE_URL=http://host.docker.internal:4000

# Use generic model names — proxy routes to the right Ollama instance
LOCAL_LLM_MODEL=qwen-plan          # default model (used for planning)

# Per-usecase overrides (see Phase 3 below)
LOCAL_LLM_MODEL_PLAN=qwen-plan
LOCAL_LLM_MODEL_AGENT=qwen-agent
```

Restart AgentCeption:

```bash
docker compose restart agentception
```

### Verify

```bash
# Health check
curl http://localhost:4000/health

# Test a completion through the proxy
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-plan",
    "messages": [{"role": "user", "content": "Reply with: hello world"}],
    "temperature": 0.0,
    "max_tokens": 32,
    "stream": false
  }'

# Confirm AgentCeption reaches local model through proxy
curl -s http://localhost:10003/api/local-llm/hello
```

---

## Phase 3: Two models, purpose-matched (100+ agents)

The key insight: **not all LLM calls need a 35B model.**

| Call type | Prompt size | Output size | Model needed |
|-----------|-------------|-------------|--------------|
| Plan 1A (YAML generation) | Large (~4k tok) | Large (~4k tok) | 35B — quality matters |
| Agent tool calls | Medium (~2k tok) | Small (200–500 tok) | 8B — speed matters |
| Recon / research | Medium | Medium | 8B or 35B |

With two models on the same machine (e.g. M4 Max 128 GB: 35B takes ~18 GB, 8B takes ~5 GB, 23 GB total), you serve planning with the large model and agent loops with the small model. Throughput for agent tool calls roughly 4×.

### Run two Ollama instances

```bash
# Instance 1 — port 11434 — large planning model
OLLAMA_HOST=0.0.0.0:11434 ollama serve &

# Instance 2 — port 11435 — fast agent model
OLLAMA_HOST=0.0.0.0:11435 ollama serve &

# Pull models on each instance
OLLAMA_HOST=localhost:11434 ollama pull qwen3.5:35b-a3b-q4_K_M
OLLAMA_HOST=localhost:11435 ollama pull qwen3.5:9b
```

> **Note:** Ollama uses a single binary; run two processes with different `OLLAMA_HOST` values to get two independent instances on different ports.

### Update litellm-config.yaml

The config already routes by model name (`qwen-plan` → instance 1, `qwen-agent` → instance 2). Verify `api_base` matches the ports you used:

```yaml
model_list:
  - model_name: qwen-plan
    litellm_params:
      model: ollama/qwen3.5:35b-a3b-q4_K_M
      api_base: http://localhost:11434   # large model

  - model_name: qwen-agent
    litellm_params:
      model: ollama/qwen3.5:9b
      api_base: http://localhost:11435   # fast model
```

### Configure AgentCeption per-usecase overrides

```bash
# .env
LOCAL_LLM_BASE_URL=http://host.docker.internal:4000  # proxy
LOCAL_LLM_MODEL_PLAN=qwen-plan    # Phase 1A → 35B
LOCAL_LLM_MODEL_AGENT=qwen-agent  # agent tool calls → 8B
```

AgentCeption's `completion_stream()` uses `LOCAL_LLM_MODEL_PLAN`; `completion_with_tools()` uses `LOCAL_LLM_MODEL_AGENT`. Both resolve through the proxy to the correct Ollama instance.

---

## Phase 4: Multi-machine (1000+ agents)

Beyond a single Apple Silicon chip, the options are:

1. **Multiple Mac Studios/Mac Pros** — each runs its own Ollama instance. Add them to `litellm-config.yaml` as additional backends under the same model name; LiteLLM load-balances across all of them.

   ```yaml
   model_list:
     - model_name: qwen-agent
       litellm_params:
         model: ollama/qwen3.5:9b
         api_base: http://mac-studio-1.local:11434

     - model_name: qwen-agent
       litellm_params:
         model: ollama/qwen3.5:9b
         api_base: http://mac-studio-2.local:11434
   ```

2. **Cloud burst to Anthropic** — when local capacity is fully saturated (all queues at maximum), LiteLLM falls back to the `claude-fallback` model defined in the config. No code change required.

3. **Anthropic primary for agents, local for planning** — set `LLM_PROVIDER=anthropic` and only override the planning path with `LOCAL_LLM_BASE_URL_PLAN` + `LOCAL_LLM_MODEL_PLAN` pointing at a local Ollama instance. Agent tool calls use Anthropic; planning uses local.

---

## Monitoring

LiteLLM Proxy exposes Prometheus metrics at `http://localhost:4000/metrics` when `enable_prometheus: true` is set in the config. Key metrics:

| Metric | What it shows |
|--------|---------------|
| `litellm_request_total` | Total requests per model and status |
| `litellm_total_tokens_used` | Token consumption per model |
| `litellm_latency_seconds` | Per-model latency histogram |
| `litellm_queue_length` | Current queue depth |

Connect to Grafana or any Prometheus-compatible dashboard for live monitoring.

---

## Troubleshooting

**Ollama not reachable from Docker:**
Ensure Ollama is listening on `0.0.0.0`, not `127.0.0.1`. Check with `curl http://localhost:11434/api/tags`. If bound to loopback only, set `OLLAMA_HOST=0.0.0.0:11434`.

**LiteLLM proxy returns 429:**
All backends are saturated. Add more Ollama instances or fall back to Anthropic by including a `claude-fallback` entry in the config.

**Proxy routes to wrong model:**
Check that `LOCAL_LLM_MODEL_PLAN` and `LOCAL_LLM_MODEL_AGENT` in `.env` exactly match the `model_name` strings in `litellm-config.yaml`.

**High latency on first request:**
Ollama loads the model on first request (cold start). Run a warm-up request after starting Ollama:

```bash
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5:35b-a3b-q4_K_M","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
  > /dev/null
```

---

## References

- [Local LLM with Ollama setup](local-llm-mlx.md) — Phase 1 runbook.
- [LLM contract and provider abstraction](../reference/llm-contract.md) — AgentCeption's LLM contract.
- [Ollama](https://ollama.com) — local inference server.
- [LiteLLM Proxy docs](https://docs.litellm.ai/docs/proxy/quick_start) — full proxy configuration reference.
