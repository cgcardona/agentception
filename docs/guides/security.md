# Security Guide

AgentCeption is an orchestration engine that calls external APIs, executes shell commands in isolated git worktrees, and exposes an HTTP service. This guide documents the security model, the controls that are in place, and what you must configure before exposing the service outside a local Docker network.

---

## Summary

| Layer | Status | What protects it |
|-------|--------|-----------------|
| LLM calls (AgentCeption → OpenRouter/Anthropic) | **Secure by default** | HTTPS; TLS enforced by httpx |
| AgentCeption HTTP service | **Localhost-only by default** | Docker binds to `127.0.0.1`; no public port |
| `/api/*` routes | **Auth opt-in** | `AC_API_KEY` middleware; disabled when key is empty |
| MCP stdio transport | **Secure by default** | No network socket; communicates over Docker exec pipe |
| MCP HTTP transport | **Same as HTTP service** | Exposed at `/api/mcp`; protected by `AC_API_KEY` when set |
| Shell tool (agent loop) | **Denylist enforced** | Destructive patterns (`rm -rf /`, `sudo`, fork bombs) are blocked |
| Qdrant (vector store) | **Localhost-only by default** | Docker binds to `127.0.0.1:6335` |

---

## LLM Communication (OpenRouter / Anthropic)

All LLM calls are made by `agentception/services/llm.py` using `httpx` to:

```
https://openrouter.ai/api/v1/chat/completions
```

`httpx` enforces TLS by default. There is no HTTP fallback. Your API key travels inside the `Authorization: Bearer <key>` header over the encrypted channel. No LLM traffic ever leaves HTTPS.

**What you must do:** Set `OPENROUTER_API_KEY` in `docker-compose.override.yml`. The key is never logged.

---

## AgentCeption HTTP Service

The service listens on port `10003` bound to `127.0.0.1` by default (configured in `docker-compose.yml`). It is not reachable from the network without explicit port-forwarding or a reverse proxy. For a local developer machine, this is the only protection required.

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
      "url": "http://localhost:10003/api/mcp",
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

The HTTP MCP transport is served at `/api/mcp` — a path under `/api/`, so it is protected by `ApiKeyMiddleware` when `AC_API_KEY` is set. Configure the key in Cursor's `mcp.json` as shown above.

**How it works technically:** The HTTP transport follows the [MCP 2025-03-26 Streamable HTTP spec](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports/). Anthropic's remote MCP integration (currently in beta) can also call this endpoint directly — Claude running on Anthropic's infrastructure sends tool calls to your `POST /api/mcp` endpoint, which executes them locally and returns results. This is the mechanism that enables the Cursor-free agent loop without losing MCP capability.

---

## TLS / HTTPS for the Service

The Docker service itself speaks plain HTTP. For public-facing deployments, terminate TLS at a reverse proxy and forward cleartext to the container:

### Caddy (recommended)

```caddy
your.domain.example {
    reverse_proxy localhost:10003
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
        proxy_pass http://127.0.0.1:10003;
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

## Shell Tool Denylist (Agent Loop)

When the Cursor-free agent loop executes shell commands via the `run_command` tool (`agentception/tools/shell_tools.py`), a denylist blocks patterns that could cause irreversible system damage:

| Blocked pattern | Why |
|----------------|-----|
| `rm -rf /` and variants | Recursive deletion from root |
| `sudo` | Privilege escalation |
| `:(){ :|:& };:` (fork bomb) | Resource exhaustion |
| `shutdown`, `reboot`, `halt`, `poweroff` | System shutdown |
| `mkfs`, `dd if=/dev/zero of=/dev/` | Disk overwrite |

The denylist is enforced by regex in `_BLOCKED_PATTERNS` in `shell_tools.py`. Blocked commands return `{"ok": false, "error": "blocked: ..."}` and are never executed.

**Note:** The denylist is a safety backstop, not a sandbox. Agents run with the same filesystem permissions as the container user. For high-security deployments, run agent worktrees in a container with a read-only root filesystem and a writable overlay for the worktree directory only.

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
| OpenRouter API key | `OPENROUTER_API_KEY` | `docker-compose.override.yml` |
| GitHub PAT (optional) | `GITHUB_TOKEN` | `docker-compose.override.yml` |
| AgentCeption API key | `AC_API_KEY` | `docker-compose.override.yml` |
| Qdrant API key (optional) | `QDRANT_API_KEY` | `docker-compose.override.yml` |

`docker-compose.override.yml` is git-ignored. Never put secrets in `docker-compose.yml` (which is committed).

```yaml
# docker-compose.override.yml — never committed
services:
  agentception:
    environment:
      OPENROUTER_API_KEY: "sk-or-..."
      GITHUB_TOKEN: "ghp_..."
      AC_API_KEY: "your-generated-key"
```

---

## Threat Model

This is an internal developer tool, not a multi-tenant SaaS. The threat model is:

| Threat | Mitigated by |
|--------|-------------|
| Attacker on the same machine | `AC_API_KEY` |
| Attacker on the network (public server) | `AC_API_KEY` + TLS reverse proxy |
| Agent loop runs destructive commands | Shell denylist |
| LLM intercepts your API key in transit | HTTPS (httpx enforces TLS) |
| Code chunks contain secrets | Indexer skips `.env`, `override.yml`; see note above |
| Prompt injection via malicious repo content | Model judgment; no systemic mitigation |

Prompt injection (malicious content in indexed files influencing agent behavior) is the hardest threat to mitigate at the infrastructure level. The best defense is reviewing agent steps in the Build dashboard before they complete.
