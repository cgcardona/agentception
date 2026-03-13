# Local LLM with MLX (Qwen 3.5 on Apple Silicon)

This guide describes how to run **Qwen 3.5 35B** (or a quantized variant) locally on macOS using the MLX framework, and how to capture CPU, GPU, and Neural Engine usage during inference. It supports the prototype in [issue #964](https://github.com/cgcardona/agentception/issues/964) and the later AgentCeption local-provider integration.

**Target hardware:** Apple Silicon Mac (M1/M2/M3/M4) with at least 24 GB unified memory; 48 GB recommended for the 35B 4-bit model.

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

For the **35B 4-bit** model (fits ~48 GB):

```bash
pip install -U mlx-vlm
```

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

Smaller models (e.g. 7B, 14B) can use `mlx-lm` and `Qwen/Qwen2.5-7B-Instruct-MLX` or community conversions; adjust the commands below accordingly.

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

**mlx-lm** (text models):

```bash
mlx_lm.server --model Qwen/Qwen2.5-7B-Instruct-MLX --host 0.0.0.0 --port 8080
```

**mlx-vlm** (if the package provides a server command for the 35B model, use the same pattern with the model ID above). Default is often `localhost:8080`.

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

## Checklist for issue #964

- [ ] One of the run options above (generate, chat, or server) completes successfully with Qwen 3.5 35B (or the 4-bit variant) on your Mac.
- [ ] At least one run has been measured with powermetrics or Activity Monitor; note CPU, GPU, and ANE usage.
- [ ] You have a repeatable command (or short script) and the model ID/path documented above for the next step (AgentCeption local provider).

---

## References

- [MLX LM](https://github.com/ml-explore/mlx-lm) — run LLMs with MLX (text).
- [MLX VLM](https://github.com/ml-explore/mlx-vlm) — vision/language models, used for Qwen 3.5 35B 4-bit.
- [mlx-community/Qwen3.5-35B-A3B-4bit](https://huggingface.co/mlx-community/Qwen3.5-35B-A3B-4bit) — 4-bit quantized Qwen 3.5 35B for MLX.
- [Qwen docs: MLX LM](https://qwen.readthedocs.io/en/latest/run_locally/mlx-lm.html) — Qwen + mlx-lm.
- [powermetrics(1)](https://keith.github.io/xcode-man-pages/powermetrics.1.html) — macOS power and usage sampling.
