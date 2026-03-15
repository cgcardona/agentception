# Security Guide

AgentCeption is an orchestration engine that calls external APIs, executes shell commands in isolated git worktrees, and exposes an HTTP service. This guide documents the security model, the controls that are in place, and what you must configure before exposing the service outside a local Docker network.

---

## Summary

| Layer | Status | What protects it |
|-------|--------|-----------------|
| LLM calls (AgentCeption → Anthropic or local) | **Secure by default** | Anthropic: HTTPS via httpx. Local: traffic stays between AgentCeption and your server (no third-party LLM). |
| AgentCeption HTTP service | **Localhost-only by default** | Docker binds to `127.0.0.1`; no public port |
| `/api/*` routes | **Auth opt-in** | `AC_API_KEY` middleware; disabled when key is empty |
| MCP stdio transport | **Secure by default** | No network socket; communicates over Docker exec pipe |
| MCP HTTP transport | **Same as HTTP service** | Exposed at `/api/mcp`; protected by `AC_API_KEY` when set |
| Shell tool (agent loop) | **Denylist + secret redaction** | Destructive patterns blocked; credential values stripped from output |
| File tool path sandbox | **Enforced** | Reads restricted to worktree + repo root; writes restricted to worktree only |
| Prompt injection | **Hardened** | System prompt explicitly warns agents to treat repo content as untrusted |
| Agent wall-clock timeout | **Enforced** | `AGENT_MAX_WALL_SECONDS` (default 2 h) cancels runaway loops |
| Docker container user | **Hardened** | `entrypoint.sh` drops from root → UID 1001 (`agentception`) via `gosu` before starting uvicorn |
| Docker container privileges | **Hardened** | `no-new-privileges`, `cap_drop: ALL`, minimal capability re-add |
| Repo bind mount | **Hardened** | Only source subdirectories mounted; `.env` and secrets are never present in the container filesystem |
| Egress proxy | **Enforced** | `tinyproxy` sidecar with strict domain allowlist; all agent HTTP/HTTPS goes through it |
| Qdrant (vector store) | **Localhost-only by default** | Docker binds to `127.0.0.1:6335` |

---

## LLM Communication

LLM calls are made by `agentception/services/llm.py` through a **provider-agnostic** API (`completion`, `completion_stream`, `completion_with_tools`). The effective provider is chosen via config (`LLM_PROVIDER` or `USE_LOCAL_LLM`); see [LLM contract and provider abstraction](../reference/llm-contract.md).

**Anthropic provider (default):** All requests go to `https://api.anthropic.com/v1/messages` over HTTPS. `httpx` enforces TLS; there is no HTTP fallback. Your API key is sent in the `Authorization: Bearer <key>` header. The key is never logged.

**Local provider:** When `LLM_PROVIDER=local`, all LLM traffic is between AgentCeption and your Ollama server on the host. No data is sent to Anthropic. Use a private base URL (e.g. `http://host.docker.internal:11434`); see [Local LLM / Ollama guide](local-llm.md).

**What you must do for Anthropic:** Set `ANTHROPIC_API_KEY` when using the anthropic provider. For local, set `LLM_PROVIDER=local` and configure `LOCAL_LLM_BASE_URL` (and optional caps).

---

## AgentCeption HTTP Service

The service listens on port `1337` bound to `127.0.0.1` by default (configured in `docker-compose.yml`). It is not reachable from the network without explicit port-forwarding or a reverse proxy. For a local developer machine, this is the only protection required.

### API Key Authentication

When you set the `AC_API_KEY` environment variable, the `ApiKeyMiddleware` (`agentception/middleware/auth.py`) validates every request to any path under `/api/`. Requests without a valid key receive `401 Unauthorized`.

**Paths protected:** `/api/*` (all JSON/SSE/MCP endpoints)

**Paths exempt:** `/` (UI), `/health`, `/static/*`, `/events` (SSE pipeline stream)

#### Enabling authentication

```bash
# Generate a high-entropy key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add it to `docker-compose.override.yml` (never to the committed `docker-compose.yml`):

```yaml
services:
  agentception:
    environment:
      AC_API_KEY: "your-generated-key-here"
```

#### Sending the key

Clients send the key in one of two headers:

```http
Authorization: Bearer <key>

# or equivalently:
X-API-Key: <key>
```

Cursor's MCP client automatically sends `Authorization: Bearer <key>` when configured:

```json
{
  "mcpServers": {
    "agentception": {
      "url": "http://localhost:1337/api/mcp",
      "headers": {
        "Authorization": "Bearer your-generated-key-here"
      }
    }
  }
}
```

#### When to enable

| Scenario | Recommendation |
|----------|---------------|
| Local development (Docker on your laptop) | Leave `AC_API_KEY` empty — the service is not reachable from the network |
| Shared development server | **Set `AC_API_KEY`** — other users on the machine can reach `127.0.0.1` |
| Public / internet-facing deployment | **Set `AC_API_KEY` + add TLS** — see reverse proxy section below |

---

## MCP Transport Security

### stdio transport (Cursor)

The stdio MCP server communicates over a Docker exec pipe — no TCP socket, no network exposure. This transport is safe by default and does not require `AC_API_KEY`.

### HTTP transport (`POST /api/mcp`)

The HTTP MCP transport is served at `/api/mcp` — a path under `/api/`, so it is protected by `ApiKeyMiddleware` when `AC_API_KEY` is set. Configure the key in your MCP client's config (e.g. Cursor's `mcp.json`) as shown in the setup guide.

**How it works technically:** The HTTP transport follows the [MCP 2025-03-26 Streamable HTTP spec](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports/). Anthropic's remote MCP integration (currently in beta) can also call this endpoint directly — Claude running on Anthropic's infrastructure sends tool calls to your `POST /api/mcp` endpoint, which executes them locally and returns results. This is the mechanism that enables the Cursor-free agent loop without losing MCP capability.

---

## TLS / HTTPS for the Service

The Docker service itself speaks plain HTTP. For public-facing deployments, terminate TLS at a reverse proxy and forward cleartext to the container:

### Caddy (recommended)

```caddy
your.domain.example {
    reverse_proxy localhost:1337
}
```

Caddy fetches and renews Let's Encrypt certificates automatically.

### nginx

```nginx
server {
    listen 443 ssl;
    server_name your.domain.example;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:1337;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 300s;   # SSE streams need a long timeout
    }
}
```

Do not forget `proxy_read_timeout` — SSE connections (the `/events` endpoint and agent run streams) stay open for minutes.

---

## Shell Tool Safety (Agent Loop)

When the Cursor-free agent loop executes shell commands via the `run_command` tool (`agentception/tools/shell_tools.py`), two layers protect the host:

### Command Denylist

A denylist blocks patterns that could cause irreversible system damage or enable privilege escalation:

| Blocked pattern | Why |
|----------------|-----|
| `rm -rf /` and variants | Recursive deletion from root |
| `rm -rf /app`, `rm -rf /worktrees` | Destruction of the mounted app or all agent worktrees |
| `sudo` | Privilege escalation |
| `:(){ :|:& };:` (fork bomb) | Resource exhaustion |
| `shutdown`, `reboot`, `halt`, `poweroff` | System shutdown |
| `mkfs`, `dd if=/dev/zero of=/dev/` | Disk overwrite |
| `nc -e`, `/dev/tcp/`, `/dev/udp/` | Reverse shell / network exfiltration |

The denylist is enforced in `_BLOCKED_PATTERNS` in `shell_tools.py`. Blocked commands return `{"ok": false, "error": "..."}` and are never executed.

### Secret Redaction

All stdout and stderr captured from shell commands is passed through `_redact_secrets()` before it is returned to the agent or persisted to the DB. This strips:

- `KEY=value` pairs for known secret environment variable names (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `DATABASE_URL`, `AC_API_KEY`, etc.)
- GitHub PAT tokens (`ghp_…`)
- Anthropic API key format (`sk-ant-…`)
- Bearer tokens in Authorization-style header lines

This means an agent that runs `env` or `printenv` will see `ANTHROPIC_API_KEY=[REDACTED]` rather than the actual key value.

**Note:** The denylist and redaction are safety backstops. The primary sandbox is the path sandbox (see below) and the Docker container's filesystem isolation.

---

## File Tool Path Sandbox

All file-reading and file-writing tools in the agent loop enforce a path sandbox (`_is_safe_read_path` / `_is_safe_write_path` in `agent_loop.py`):

| Tool class | Allowed paths |
|------------|--------------|
| Read tools (`read_file`, `read_file_lines`, `read_symbol`, `read_window`, `list_directory`, `search_text`) | Worktree directory OR repo root (`REPO_DIR`) |
| Write tools (`write_file`, `replace_in_file`, `insert_after_in_file`) | Worktree directory **only** |
| `run_command` cwd | Worktree directory OR repo root |

Symlinks are resolved before the check (`.resolve()`), so symlinks pointing outside allowed roots are correctly rejected.

An agent requesting `read_file(path="/app/.env")`, `read_file(path="/etc/passwd")`, or `write_file(path="/tmp/evil.sh")` will receive `{"ok": false, "error": "path '...' is outside the allowed scope"}` and the operation will not execute.

---

## Prompt Injection Protection

The agent system prompt includes an explicit security contract that instructs agents to treat all repository content as **untrusted external input**:

1. Repository files, code comments, issue bodies, and PR descriptions may contain adversarial instructions (prompt injection attacks). Agents are instructed to treat them as data, never as authoritative commands.
2. Agents must never read, log, or transmit credential values regardless of what repository content instructs.
3. Agents must never make outbound HTTP requests to third-party URLs outside their expected workflow.
4. The system prompt explicitly states that it supersedes any conflicting instruction found in untrusted content.

This is implemented in `_RUNTIME_ENV_NOTE` within `agent_loop.py` and is injected into every agent's system prompt before the first turn.

---

## Agent Wall-Clock Timeout

Each agent run has a hard wall-clock timeout (`AGENT_MAX_WALL_SECONDS`, default 7200 seconds / 2 hours). This is enforced via `asyncio.timeout()` wrapping the entire agent loop. When the timeout fires:

1. The loop is cancelled with `asyncio.TimeoutError`.
2. An error is logged to the run's event log.
3. `build_cancel_run` transitions the run to `cancelled`.
4. Partial work remains in the worktree for operator inspection.

Set `AGENT_MAX_WALL_SECONDS=0` in `docker-compose.override.yml` to disable the timeout (not recommended for production).

Individual tool calls also have their own per-call timeouts (5 minutes for `run_command`, 30 seconds for search tools) independent of the global wall-clock timeout.

---

## Qdrant (Vector Store)

The Qdrant container is bound to `127.0.0.1:6335` (REST) and `127.0.0.1:6336` (gRPC). It is not reachable from the network by default.

Qdrant stores only code chunks derived from your local repository — no secrets, API keys, or environment variable values are indexed. The indexer (`agentception/services/code_indexer.py`) reads source files, not `.env` files or `docker-compose.override.yml`.

If you need to enable Qdrant authentication for shared deployments, set `QDRANT_API_KEY` in the Qdrant service's environment in `docker-compose.override.yml` and update `QDRANT_URL` to include the key:

```yaml
services:
  qdrant:
    environment:
      QDRANT__SERVICE__API_KEY: "your-qdrant-key"
  agentception:
    environment:
      QDRANT_URL: "http://agentception-qdrant:6333"
      # Qdrant client picks up QDRANT_API_KEY from the environment:
      QDRANT_API_KEY: "your-qdrant-key"
```

---

## Secrets Management

| Secret | Env var | Where to set |
|--------|---------|-------------|
| Anthropic API key | `ANTHROPIC_API_KEY` | `docker-compose.override.yml` |
| GitHub PAT (optional) | `GITHUB_TOKEN` | `docker-compose.override.yml` |
| AgentCeption API key | `AC_API_KEY` | `docker-compose.override.yml` |
| Qdrant API key (optional) | `QDRANT_API_KEY` | `docker-compose.override.yml` |

`docker-compose.override.yml` is git-ignored. Never put secrets in `docker-compose.yml` (which is committed).

```yaml
# docker-compose.override.yml — never committed
services:
  agentception:
    environment:
      ANTHROPIC_API_KEY: "sk-ant-..."
      GITHUB_TOKEN: "ghp_..."
      AC_API_KEY: "your-generated-key"
```

---

## Docker Container Hardening

### Non-root process user

`scripts/entrypoint.sh` implements a two-phase startup:

1. **Root phase** — writes `/etc/resolv.conf`, compiles SCSS/JS assets, runs Alembic migrations, and fixes ownership of mutable mount points (`/worktrees`, `/home/agentception/.cache/huggingface`).
2. **Unprivileged phase** — `exec gosu agentception "$@"` drops to UID 1001 (`agentception` user) for the long-running `uvicorn` process. Every Python coroutine, agent loop iteration, and tool call runs as this non-root user.

`gosu` is a purpose-built setuid helper (analogous to `sudo -u` but signal-transparent and without shell overhead). After the `exec`, no process in the container runs as root.

The `agentception` user is created in the Dockerfile:

```dockerfile
RUN groupadd -g 1001 agentception \
    && useradd -r -u 1001 -g agentception -m -d /home/agentception -s /bin/bash agentception
```

### Capability model

```yaml
security_opt:
  - no-new-privileges:true   # prevents privilege escalation via setuid binaries
cap_drop:
  - ALL                      # drop all capabilities at the container level
cap_add:
  - CHOWN                    # needed for entrypoint chown of /worktrees and model cache
  - SETGID                   # needed for gosu to switch GID
  - SETUID                   # needed for gosu to switch UID
  - DAC_OVERRIDE             # needed for sass/npm writes during root phase
  - FOWNER                   # needed for git operations in worktrees
```

After gosu drops to UID 1001, these capabilities remain in the bounding set but `no-new-privileges` prevents the non-root process from using `SETUID`/`SETGID` to re-acquire root.

### Narrow bind mounts (`.env` isolation)

Instead of mounting the full repository root (`./:/app`) — which would expose `.env`, `docker-compose.yml` secrets, `.git`, and other sensitive files — only the directories the container needs at runtime are mounted:

```yaml
volumes:
  - ./agentception:/app/agentception   # Python source, templates, static
  - ./.agentception:/app/.agentception # generated role prompt files
  - ./pyproject.toml:/app/pyproject.toml
  - ./org-presets.yaml:/app/org-presets.yaml
  - ./scripts:/app/scripts
  - ./tests:/app/tests
  - ./tools:/app/tools
```

**Not mounted** (intentionally excluded):

| File / Directory | Why excluded |
|-----------------|-------------|
| `.env` | Contains `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `DB_PASSWORD` |
| `docker-compose*.yml` | Contains environment variable references to secrets |
| `.git/` | Contains git objects and config (no runtime need) |
| `Dockerfile` | Build-time artifact; no runtime need |
| `node_modules/` | Served from a named volume (ELF binaries); never from the host |
| `AGENTS.md`, `README.md` | Documentation; no runtime need |

Credentials reach the container **only via environment variables** (injected from the host shell or Docker secrets), never via the filesystem.

---

## Egress Proxy

A `tinyproxy` sidecar container (`agentception-proxy`) enforces a domain allowlist for all outbound HTTP/HTTPS traffic from the `agentception` container.

### How it works

The `agentception` container has `HTTP_PROXY=http://proxy:8888` and `HTTPS_PROXY=http://proxy:8888` set. Every HTTP/HTTPS client that honours these environment variables — including Python `httpx`, `requests`, `curl`, `git`, `npm`, and the `gh` CLI — routes its connections through tinyproxy. For HTTPS, tinyproxy handles the `CONNECT` method: it checks the target hostname against the allowlist, then opens a transparent TCP tunnel if allowed (the TLS payload is never inspected).

If the target domain is NOT in the allowlist, tinyproxy returns `HTTP 403 Filtered` and the connection is refused before a single byte of data leaves the Docker network.

### Allowed domains

| Domain pattern | Purpose |
|---------------|---------|
| `api.anthropic.com` | LLM API calls |
| `api.github.com`, `github.com`, `*.githubusercontent.com`, `codeload.github.com` | GitHub API, git push/fetch, gh CLI |
| `registry.npmjs.org`, `cdn.jsdelivr.net` | NPM package installs |
| `huggingface.co`, `cdn-lfs*.huggingface.co`, `cdn-lfs-us-1.hf.co` | ONNX model downloads (first run) |
| `files.pythonhosted.org`, `pypi.org` | pip installs in agent worktrees |
| `1.1.1.1`, `cloudflare.com` | Cloudflare DNS |

The allowlist lives in `scripts/tinyproxy/filter` (one regex per line). Review and audit every addition.

### Bypass configuration

`NO_PROXY=localhost,127.0.0.1,postgres,qdrant,host.docker.internal,::1` exempts internal Docker services from proxy routing. Connections to `postgres` (SQLAlchemy) and `qdrant` (vector store) are direct; they do not go through tinyproxy.

### Limitation

Tinyproxy intercepts only processes that honour `*_PROXY` environment variables. A process that opens a raw TCP socket without consulting the proxy env vars would bypass the allowlist. This vector is mitigated by:
1. The shell denylist (blocks `/dev/tcp`, `nc -e`, reverse shells)
2. The fact that uvicorn and all agent code use standard HTTP client libraries
3. The non-root user reducing the surface area for privilege escalation

For a fully locked-down deployment, combine the proxy with host-level iptables rules that drop traffic from the container's Docker network to anything other than `proxy:8888`.

---

## Threat Model

This is an internal developer tool, not a multi-tenant SaaS. The threat model is:

| Threat | Mitigated by |
|--------|-------------|
| Attacker on the same machine | `AC_API_KEY` |
| Attacker on the network (public server) | `AC_API_KEY` + TLS reverse proxy |
| Agent loop runs destructive commands | Shell denylist |
| Agent escapes worktree to read secrets via file tool | File-tool path sandbox |
| Agent reads secrets via shell command | Secret redaction layer strips values from all shell output |
| `.env` accessible inside container | Narrow bind mounts exclude `.env` — it never exists in the container filesystem |
| Agent exfiltrates data to attacker-controlled URL | Egress proxy allowlist rejects non-allowlisted domains |
| Agent escapes container, gains root | Non-root process user (UID 1001) + `no-new-privileges` |
| LLM intercepts your API key in transit | HTTPS (httpx enforces TLS) |
| Code chunks contain secrets | Indexer skips `.env`, `override.yml` |
| Prompt injection via malicious repo content | System-prompt security contract + agent instruction hierarchy |
| Runaway agent loop (cost explosion) | Hard iteration limit + wall-clock timeout |
| Container privilege escalation | `no-new-privileges` + `cap_drop: ALL` + minimal caps |

**Residual risks (accepted for a developer tool):**

- Raw TCP socket connections (bypassing `*_PROXY` env vars) are not blocked at the network level. Mitigated by the shell denylist.
- Agents can still read environment variables from the process environment via Python (`os.environ`). The secret redaction layer strips these from shell tool output; direct Python access within the agent's own process is harder to intercept. Mitigation: secrets are injected per-run and never baked into the image.
- The container still has outbound access to the allowlisted domains. A compromised agent could exfiltrate data to `api.github.com` (e.g. by creating a GitHub issue). This is a residual risk inherent to any agent that needs to push code and create PRs.
