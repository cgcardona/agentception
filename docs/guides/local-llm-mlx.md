# Local LLM with MLX (Qwen 3.5 on Apple Silicon)

This guide describes how to run **Qwen 3.5 35B** (or a quantized variant) locally on macOS using the MLX framework, and how to capture CPU, GPU, and Neural Engine usage during inference. It supports the prototype in [issue #964](https://github.com/cgcardona/agentception/issues/964) and the AgentCeption **local provider** integration.

**Target hardware:** Apple Silicon Mac (M1/M2/M3/M4) with at least 24 GB unified memory; 48 GB recommended for the 35B 4-bit model.

AgentCeption uses a **provider-agnostic LLM contract**. When the effective provider is **local**, all LLM calls (planning, streaming preview, agent loop) go to your OpenAI-compatible server instead of Anthropic. Thinking vs content is **normalized by the adapter**: stream chunks always have `type: "thinking"` or `type: "content"`; completion returns only the final answer string. See [LLM contract and provider abstraction](../reference/llm-contract.md) for the full contract and config model.

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
| `LOCAL_LLM_BASE_URL` | `http://host.docker.internal:8080` | Base URL of the OpenAI-compatible server (no trailing slash). From Docker, use `host.docker.internal` to reach a server on the host. |
| `LOCAL_LLM_CHAT_PATH` | `/v1/chat/completions` | Path appended to the base URL for chat completions. Some servers use `/chat/completions` without `/v1`. |
| `LOCAL_LLM_MODEL` | *(empty)* | Model name sent in requests. If empty, the field is omitted so servers like mlx_lm use their loaded model (avoids 404). |
| `LOCAL_LLM_MAX_CONTEXT_CHARS` | `12000` | Max characters for the first user message (task briefing) when using the local LLM. Reduces load on small models. |
| `LOCAL_LLM_MAX_SYSTEM_CHARS` | `6000` | Max characters for the system prompt (role + cognitive arch). Truncation is applied when using the local provider. |
| `LOCAL_LLM_MAX_TOKENS` | `4096` | Max completion tokens per turn. Use 4096 or 8192 for small models; avoid 32k. |

**Effective provider:** If `USE_LOCAL_LLM=true`, the effective provider is **local** regardless of `LLM_PROVIDER`. Otherwise the effective provider is the value of `LLM_PROVIDER`. Only the effective provider is used when deciding which adapter to call.

---

## How the local adapter works

The local adapter in `agentception/services/llm.py` implements the same **contract** as the Anthropic path:

- **Completion:** Single request to your server; response `choices[0].message.content` is normalized (string or list of parts; reasoning parts stripped) and returned as a single final-answer string.
- **Streaming:** If the server supports `stream: true`, the adapter parses SSE and maps `delta.content` / `delta.reasoning_content` to `LLMChunk(type="content", ...)` and `LLMChunk(type="thinking", ...)`. If streaming is not supported or fails, it falls back to one completion and yields one content chunk so the contract is always satisfied.
- **Tools:** Same request/response shape as completion but with `tools` and `tool_choice`; response content and `tool_calls` are normalized to the shared `ToolResponse` type.

No caller code (plan UI, phase planner, agent loop) branches on provider; they all use `completion()`, `completion_stream()`, or `completion_with_tools()`. See [LLM contract](../reference/llm-contract.md) for details.

---

## Prerequisites

| Requirement | Notes |
|-------------|--------|
| macOS | Apple Silicon only (M-series). Intel Macs are not supported by MLX. |
| Python | 3.11 or newer. Prefer a venv or conda environment. |
| Memory | 48 GB unified memory recommended for Qwen 3.5 35B 4-bit to avoid swap. |

---

## Install MLX and model runtime

Two common options:

- **Text-only (mlx-lm):** for chat/completion and the built-in OpenAI-compatible server.
- **Vision/multimodal (mlx-vlm):** required for the 4-bit 35B model from mlx-community (`Qwen3.5-35B-A3B-4bit`).

For the **35B 4-bit** model (fits ~48 GB) you need **mlx-vlm ≥ 0.3.12** (for `qwen3_5_moe` support). If you use mlx-openai-server, install it first then run `pip install -U "mlx-vlm>=0.3.12"` in a second step to avoid pip `ResolutionImpossible`. If you see "Model type qwen3_5_moe not supported", upgrade mlx-vlm (see "48 GB Mac" runbook below).

For **text-only** models and the standard server (e.g. smaller Qwen or other MLX checkpoints):

```bash
pip install -U mlx-lm
```

---

## Model choice: Qwen 3.5 35B

A practical option for 35B on 48 GB is the **4-bit quantized** MLX conversion from the mlx-community:

| Model | Hugging Face ID | Package | Notes |
|-------|------------------|---------|--------|
| Qwen 3.5 35B (4-bit) | `mlx-community/Qwen3.5-35B-A3B-4bit` | `mlx-vlm` | Fits 48 GB; supports text and vision. |

Smaller **Qwen 3.5** models (4B, 9B) use `mlx-lm` and are listed in the "Larger Qwen for real coding" section below; Qwen 2.5 (7B, 14B) is also available from mlx-community if you prefer that line.

---

## Run inference (minimal test)

### Option A: Generate (one-off prompt)

**With mlx-vlm** (35B 4-bit):

```bash
python -m mlx_vlm.generate \
  --model mlx-community/Qwen3.5-35B-A3B-4bit \
  --max-tokens 100 \
  --temperature 0.0 \
  --prompt "Write a one-line Python function that returns the sum of two integers."
```

The first run downloads the model from Hugging Face; later runs use the cache.

**With mlx-lm** (smaller text-only, e.g. 7B):

```bash
mlx_lm.generate \
  --model Qwen/Qwen2.5-7B-Instruct-MLX \
  --max-tokens 100 \
  --prompt "Write a one-line Python function that returns the sum of two integers."
```

### Option B: Interactive chat

**mlx-vlm:**

```bash
python -m mlx_vlm.chat --model mlx-community/Qwen3.5-35B-A3B-4bit
```

**mlx-lm:**

```bash
mlx_lm.chat --model Qwen/Qwen2.5-7B-Instruct-MLX
```

Type prompts at the prompt; exit with your shell’s EOF (e.g. Ctrl+D).

### Option C: OpenAI-compatible server (for AgentCeption integration)

Start a local HTTP server that exposes `/v1/chat/completions`:

**mlx-lm** (text models, e.g. 4B/9B):

```bash
mlx_lm.server --model Qwen/Qwen2.5-7B-Instruct-MLX --host 0.0.0.0 --port 8080
```

**Qwen 3.5 35B (48 GB Mac)** — use **mlx-openai-server** (multimodal). Command below works on all versions (e.g. 1.1.x and 1.4+). If you see “No such option: --reasoning-parser”.

```bash
pip install -U mlx-openai-server
mlx-openai-server launch \
  --model-path mlx-community/Qwen3.5-35B-A3B-4bit \
  --model-type multimodal \
  --port 8080
```

Optional (v1.4.1+ only): add `--reasoning-parser qwen3_5` and `--tool-call-parser qwen3_coder`. If you see "No such option: --reasoning-parser", omit them (e.g. you have 1.1.x or Python 3.13 with no newer wheel).

Bind to all interfaces so Docker can reach it: add `--host 0.0.0.0` if the server supports it (see mlx-openai-server docs). AgentCeption uses `host.docker.internal:8080` by default.

Then test with:

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "default", "messages": [{"role": "user", "content": "Say hello in one word."}], "max_tokens": 20}'
```

---

## Capturing resource usage (CPU, GPU, Neural Engine)

Use **Activity Monitor** for a quick view, or **powermetrics** for detailed, script-friendly logs.

### Activity Monitor

1. Open **Activity Monitor** (Applications → Utilities).
2. Window → **CPU History**, **GPU History** (if available).
3. Run your inference in a terminal; watch **CPU**, **Memory**, and **GPU** during the run.

### powermetrics (command line)

Requires `sudo`. Sample every 1 second, 60 samples (~1 minute), and write to a file:

```bash
sudo powermetrics -i 1000 -n 60 -o powermetrics_run.txt -s cpu_power,gpu_power,ane_power
```

Then start your inference in another terminal. When the run finishes, stop powermetrics (Ctrl+C if running in foreground). The file will contain CPU, GPU, and ANE (Neural Engine) power estimates.

**Useful samplers:**

- `cpu_power` — CPU usage and power
- `gpu_power` — GPU
- `ane_power` — Apple Neural Engine
- `all` — everything (noisier, larger output)

**Example (minimal, 10 samples at 2 s):**

```bash
sudo powermetrics -i 2000 -n 10 -o ~/mlx_resource_run.txt -s cpu_power,gpu_power,ane_power
```

Open `~/mlx_resource_run.txt` after the run to inspect CPU/GPU/ANE utilization during inference.

### Interpreting results

- **Latency:** Wall-clock time from prompt submit to last token (or to EOS).
- **Throughput:** (Generated tokens) / (wall-clock seconds) → tokens per second.
- **Resource usage:** Use the powermetrics output or Activity Monitor to see whether CPU, GPU, or ANE is dominant and whether the machine is thermally or memory-bound.

---

## AgentCeption integration (developer agent with local model)

When the local server is running, you can point the **developer** agent at it so it uses the local model instead of Anthropic for tool-use turns.

1. **Start the MLX server on the host** (in a separate terminal, so Metal/GPU is available):

   ```bash
   cd /path/to/agentception
   .venv-mlx/bin/mlx_lm.server --model mlx-community/Qwen3-4B-Instruct-2507-4bit --host 0.0.0.0 --port 8080
   ```

   Leave this running. From inside Docker, the agent reaches it at `host.docker.internal:8080`.

2. **Enable the local provider** in AgentCeption:

   - Set **either** `LLM_PROVIDER=local` **or** `USE_LOCAL_LLM=true` in `.env` (or in `docker-compose.override.yml` under `agentception.environment`). See [Config and environment](#config-and-environment) for the full table.
   - Optionally set `LOCAL_LLM_BASE_URL=http://host.docker.internal:8080` (this is the default).
   - Restart the stack: `docker compose restart agentception`.

3. **Probe the local model (no agent)** — confirm the server responds:

   ```bash
   curl -s http://localhost:10003/api/local-llm/hello
   ```

   You should get `{"ok": true, "reply": "hello world"}` (or similar). If you get 502, the container cannot reach the MLX server; check `host.docker.internal` and that the server is running on the host.

4. **Optional: run an agent that says "hello world"** — same model, but through the full agent loop (one turn, no tools):

   ```bash
   curl -s -X POST http://localhost:10003/api/local-llm/hello-agent
   ```

   Returns `{"run_id": "local-hello-<uuid>", "status": "implementing"}`. Watch with `python scripts/watch_run.py local-hello-<uuid>` or check the Build dashboard; the run completes after one LLM reply.

5. **Dispatch a developer agent** as usual (e.g. from the Build dashboard or `POST /api/dispatch/issue`). Only the **developer** role uses the local model; the planner and reviewer still use Anthropic. The local path uses the **same pipeline** as Anthropic: full system prompt (role file + cognitive architecture + runtime note), same task briefing, same tools, and same extra blocks (context pressure, pytest stop, etc.). Only the LLM endpoint and context caps differ, so real tickets run identically—just against your local Qwen instead of Claude.

6. **Scale up: basic coding** — Use the same local model for a small coding task. Keep the effective provider as **local** (`LLM_PROVIDER=local` or `USE_LOCAL_LLM=true`). Dispatch a developer for a minimal issue (e.g. "In `docs/guides/dispatch.md` add one sentence in the Request shape section: 'Always pass issue_body.'"). Example:

   ```bash
   # From repo root; replace 969 with your small issue number
   BODY=$(curl -s -H "Accept: application/vnd.github+json" \
     "https://api.github.com/repos/cgcardona/agentception/issues/969" | \
     python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({
       'issue_number': 969, 'issue_title': d['title'], 'issue_body': d['body'] or '',
       'role': 'developer', 'repo': 'cgcardona/agentception'
     }))")
   curl -s -X POST http://localhost:10003/api/dispatch/issue \
     -H "Content-Type: application/json" -d "$BODY"
   python3 scripts/watch_run.py issue-969
   ```

   The agent gets the full tool set (read_file, search_codebase, replace_in_file, etc.) with context truncated to `LOCAL_LLM_MAX_CONTEXT_CHARS` / `LOCAL_LLM_MAX_SYSTEM_CHARS`. If the model stalls or loops, lower those caps or try an even smaller task.

7. **Turn off** when done: set `LLM_PROVIDER=anthropic` (or `USE_LOCAL_LLM=false` / remove it) and restart so the next run uses Anthropic again.

### Small models (e.g. Qwen 4B) — context caps

Small local models are easily overloaded by the full task briefing and system prompt. AgentCeption truncates context when the **effective provider is local** so a "hello world" run can complete. Tune these in `.env`; see [Config and environment](#config-and-environment) for the full list:

| Env var | Default | Purpose |
|--------|---------|--------|
| `LOCAL_LLM_MAX_CONTEXT_CHARS` | 12000 | Max characters for the first user message (task briefing). |
| `LOCAL_LLM_MAX_SYSTEM_CHARS` | 6000 | Max characters for the system prompt (role + cognitive arch). |
| `LOCAL_LLM_MAX_TOKENS` | 4096 | Max completion tokens per turn (avoid 32k for small models). |

If the agent stalls or produces no tool calls, ensure the MLX server is reachable from the container (`host.docker.internal`; on Linux add `extra_hosts: ["host.docker.internal:host-gateway"]` in docker-compose) and consider lowering the caps further.

### Pushing further (Qwen 4B and up)

With the defaults above, the following have been verified with Qwen 4B (e.g. `mlx-community/Qwen3-4B-Instruct-2507-4bit`):

- **Hello world** — one turn, no tools (`POST /api/local-llm/hello-agent`).
- **Single-file doc edit** — search → read → replace → done (e.g. add one sentence to a guide). Full developer tool set (12 tools), truncated context.

To push further:

- **Multi-file or add-a-test** — Try an issue that touches 2–3 files or adds a test. If the model stalls or loops, lower `LOCAL_LLM_MAX_CONTEXT_CHARS` / `LOCAL_LLM_MAX_SYSTEM_CHARS` a bit.
- **Larger Qwen 3.5** — See the "Larger Qwen for real coding" section below for 9B and 35B Qwen 3.5 models and recommended env caps.
- **Watch script** — Use `python3 scripts/watch_run.py <run_id>`; the ITER line shows `local` (green) when the run is using the local LLM so you can confirm the provider.

### Larger Qwen 3.5 for real coding

Use a **Qwen 3.5** model that fits your Mac’s unified memory. Same AgentCeption setup (`USE_LOCAL_LLM=true`); only the model and (optionally) context caps change.

| Mac RAM (unified) | Model | Hugging Face ID | Notes |
|-------------------|--------|------------------|--------|
| 8–16 GB | Qwen 3.5 4B | `mlx-community/Qwen3.5-4B-OptiQ-4bit` | Same family as the 4B you used; optional upgrade from Qwen3-4B. |
| 16–24 GB | Qwen 3.5 9B | `mlx-community/Qwen3.5-9B-4bit` | Good step up for real coding; more context and better tool use. |
| 48 GB | Qwen 3.5 35B (MoE) | `mlx-community/Qwen3.5-35B-A3B-4bit` | Use **mlx-openai-server** (multimodal). Best quality; avoid on 24 GB or less. |

**1. Install and start the server** (one of the following; adjust port if needed):

```bash
# Qwen 3.5 9B (recommended for 16–24 GB; real coding)
.venv-mlx/bin/mlx_lm.server --model mlx-community/Qwen3.5-9B-4bit --host 0.0.0.0 --port 8080

# Qwen 3.5 35B (48 GB Mac) — use mlx-openai-server (see "48 GB Mac: Qwen 3.5 35B" below)
mlx-openai-server launch \
  --model-path mlx-community/Qwen3.5-35B-A3B-4bit \
  --model-type multimodal \
  --port 8080
```

For 35B we recommend [mlx-openai-server](https://github.com/cubist38/mlx-openai-server) (multimodal + tool-call parsing). A dedicated runbook for 48 GB Macs is in the next subsection.

**2. Raise context caps** in `.env` so the larger model gets more of the briefing and system prompt (optional but recommended for 9B/35B):

```bash
LOCAL_LLM_MAX_CONTEXT_CHARS=24000
LOCAL_LLM_MAX_SYSTEM_CHARS=12000
LOCAL_LLM_MAX_TOKENS=8192
```

Restart AgentCeption, then dispatch developer agents as usual. With the effective provider set to **local**, they will use the new model at `host.docker.internal:8080` with no code changes.

### 48 GB Mac: Qwen 3.5 35B with AgentCeption

Step-by-step to run the 35B 4-bit model with AgentCeption on a 48 GB Apple Silicon Mac.

**1. Create a venv and install mlx-openai-server, then mlx-vlm ≥ 0.3.12** (35B MoE needs recent mlx-vlm)

Install in **two steps** so pip does not hit `ResolutionImpossible` (mlx-openai-server pins mlx-vlm==0.3.0):

```bash
python3 -m venv .venv-mlx
source .venv-mlx/bin/activate
pip install -U mlx-openai-server
pip install -U "mlx-vlm>=0.3.12"
```

You may see dependency conflict warnings after the second command; **try running the server anyway**—newer mlx-vlm usually works. If the 35B server fails with "Model type qwen3_5_moe not supported", the mlx-vlm upgrade did not take effect; retry the second step or use `pip install -U --force-reinstall "mlx-vlm>=0.3.12"`.

If the server fails with **"Qwen3VLVideoProcessor requires the Torchvision library"** (or PyTorch), the model's processor config pulls in a video component that needs torch/torchvision at load time. Install them (inference still uses MLX):

```bash
pip install torch torchvision
```

**2. Start the 35B server on port 8080** (so AgentCeption’s default `host.docker.internal:8080` works)

```bash
mlx-openai-server launch \
  --model-path mlx-community/Qwen3.5-35B-A3B-4bit \
  --model-type multimodal \
  --port 8080
```

If your version is v1.4.1+ you can add `--reasoning-parser qwen3_5` and `--tool-call-parser qwen3_coder`. If you get "No such option", use the command above as-is (common with 1.1.x or Python 3.13).

First run downloads the model from Hugging Face. If the server only binds to localhost, check the project’s docs for `--host 0.0.0.0` so Docker can reach it from the container.

**3. Configure AgentCeption**

In `.env`:

```bash
LLM_PROVIDER=local
# Or: USE_LOCAL_LLM=true
LOCAL_LLM_BASE_URL=http://host.docker.internal:8080
LOCAL_LLM_MAX_CONTEXT_CHARS=24000
LOCAL_LLM_MAX_SYSTEM_CHARS=12000
LOCAL_LLM_MAX_TOKENS=8192
```

Leave `LOCAL_LLM_MODEL` unset (or empty) so the server uses its loaded model. See [Config and environment](#config-and-environment) for all local-provider env vars.

**4. Ensure Docker can reach the host**

`docker-compose.yml` should have:

```yaml
extra_hosts: ["host.docker.internal:host-gateway"]
```

**5. Restart AgentCeption and verify**

```bash
docker compose restart agentception
curl -s http://localhost:10003/api/local-llm/hello
```

Expect `{"ok": true, "reply": "..."}`. Then dispatch a developer agent (e.g. from the Build UI or MCP) with an issue and watch with `python3 scripts/watch_run.py issue-<N>`. The ITER line should show `local` (green) when the run uses the 35B model.

### Sample coding task (test code generation)

Use the following as a GitHub issue to see what code the local model can produce. Create the issue, ensure the effective provider is **local** (`LLM_PROVIDER=local` or `USE_LOCAL_LLM=true`), then dispatch a developer and watch with `python3 scripts/watch_run.py issue-<N>`.

**Title:** `Add clamp() helper and test (local LLM code gen)`

**Body:**

```markdown
## Context
Small code-generation test for the local LLM (Qwen 4B) pipeline.

## Objective
1. Create `agentception/utils.py` with a single function:
   - `def clamp(value: float, low: float, high: float) -> float`
   - Return `value` clamped to the range `[low, high]` (if value < low return low; if value > high return high; else return value).
   - Add `from __future__ import annotations` at the top and a one-line module docstring.
2. Create `agentception/tests/test_utils.py` with one test:
   - `def test_clamp_returns_value_within_bounds()`
   - Assert `clamp(5.0, 0.0, 10.0) == 5.0`, `clamp(-1.0, 0.0, 10.0) == 0.0`, `clamp(11.0, 0.0, 10.0) == 10.0`.
   - Use the same `from __future__ import annotations` and a test module docstring if you like.

## Acceptance criteria
- [ ] `agentception/utils.py` exists with `clamp()` and type hints.
- [ ] `agentception/tests/test_utils.py` exists and `pytest agentception/tests/test_utils.py -v` passes.
```

After the run, run `docker compose exec agentception pytest agentception/tests/test_utils.py -v` and `mypy agentception/utils.py` to confirm the code is valid.

**If no PR appeared:** The agent must push the branch (`git_commit_and_push`) before calling `create_pull_request`; GitHub only creates a PR when the head branch exists on the remote. If the model called `create_pull_request` first (or the push failed), you'll see "couldn't find remote ref feat/issue-N" and no PR. To recover: from the host, push the worktree branch and open the PR manually:

```bash
cd ~/.agentception/worktrees/agentception/issue-970   # or your run's worktree
git status                                            # see if there are commits
git push -u origin feat/issue-970                    # push the branch
gh pr create --base dev --fill                        # create PR from the branch
```

---

## Checklist for issue #964

- [ ] One of the run options above (generate, chat, or server) completes successfully with Qwen 3.5 35B (or the 4-bit variant) on your Mac.
- [ ] At least one run has been measured with powermetrics or Activity Monitor; note CPU, GPU, and ANE usage.
- [ ] You have a repeatable command (or short script) and the model ID/path documented above for the next step (AgentCeption local provider).

---

## References

- [LLM contract and provider abstraction](../reference/llm-contract.md) — AgentCeption’s LLM contract, provider selection, and how to add a provider.
- [MLX LM](https://github.com/ml-explore/mlx-lm) — run LLMs with MLX (text).
- [MLX VLM](https://github.com/ml-explore/mlx-vlm) — vision/language models, used for Qwen 3.5 35B 4-bit.
- [mlx-community/Qwen3.5-35B-A3B-4bit](https://huggingface.co/mlx-community/Qwen3.5-35B-A3B-4bit) — 4-bit quantized Qwen 3.5 35B for MLX.
- [Qwen docs: MLX LM](https://qwen.readthedocs.io/en/latest/run_locally/mlx-lm.html) — Qwen + mlx-lm.
- [powermetrics(1)](https://keith.github.io/xcode-man-pages/powermetrics.1.html) — macOS power and usage sampling.
