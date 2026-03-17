# AgentCeption — MCP Integration

AgentCeption exposes a best-in-class MCP (Model Context Protocol) server so any MCP-aware client — IDEs, agent loops, CI/CD pipelines — can invoke tools, read resources, and fetch prompts directly.

## The two interfaces — HTTP and MCP

We believe every production AI application needs both:

| Interface | Who uses it | How |
|-----------|-------------|-----|
| **HTTP REST** | Humans via browser, CI/CD scripts, integration tests | `GET`/`POST` to `/api/*` |
| **MCP** | AI agents (IDEs, Claude, custom loops) | JSON-RPC 2.0 over stdio or HTTP |

AgentCeption implements both. The HTTP REST API is the service backbone; the MCP server is the agent interface on top of it. The two are complementary — the same planning pipeline, issue graph, and agent dispatch are accessible through both surfaces.

## Transports

Two transports are available — both speak the same JSON-RPC 2.0 protocol:

| Transport | Entry point | Best for |
|-----------|-------------|----------|
| **stdio** | `docker compose exec -T agentception python -m agentception.mcp.stdio_server` | Local MCP clients (e.g. Cursor, Claude Desktop, any stdio-capable client) |
| **HTTP** | `POST http://localhost:1337/api/mcp` | Agent loops, CI/CD, curl, remote MCP clients, the AgentCeption dashboard |

The HTTP transport follows the [MCP 2025-11-25 Streamable HTTP spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports/): single or batch JSON-RPC request bodies, JSON responses. Notifications (requests without `id`) return `202 Accepted`.

**HTTP endpoint summary:**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/mcp` | Send a JSON-RPC 2.0 request or response |
| `GET` | `/api/mcp` | Open an SSE stream to receive server-initiated messages (e.g. `elicitation/create`) |
| `DELETE` | `/api/mcp` | Terminate a session |

`GET /api/mcp` without `Accept: text/event-stream` returns `405 Method Not Allowed` — the correct signal per the spec so clients distinguish Streamable HTTP from the deprecated 2024-11-05 HTTP+SSE transport. With `Accept: text/event-stream` and a valid `MCP-Session-Id`, it opens a persistent SSE stream that delivers server-initiated requests down to the client.

## Security

See the full [Security Guide](security.md) for threat model, TLS configuration, and Qdrant security.

**Quick summary:**

| Transport | Protection |
|-----------|-----------|
| stdio | No network socket; communicates over a Docker exec pipe — safe by default |
| HTTP (`/api/mcp`) | Protected by `ApiKeyMiddleware` when `AC_API_KEY` is set |

The HTTP endpoint also enforces two security requirements from the MCP 2025-11-25 spec:

- **Origin validation** — If the `Origin` header is present and the host is not `localhost` or `127.0.0.1`, the server returns `403 Forbidden`. This blocks DNS rebinding attacks. Programmatic API clients (agent loops, CI) never send an `Origin` header and are unaffected.
- **Protocol version validation** — If the `MCP-Protocol-Version` header is present but names a version the server does not support, it returns `400 Bad Request`. Absent headers are accepted (backwards compatible with `2025-03-26`).

When `AC_API_KEY` is configured, the HTTP MCP client must include the key:

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

LLM calls from AgentCeption to Anthropic always use HTTPS — there is no plaintext LLM traffic.

## Client configuration (stdio)

Add an `agentception` entry to your MCP client's configuration file (e.g. `mcp.json` for Cursor or the equivalent for your MCP client):

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose", "-f", "AGENTCEPTION_REPO_ROOT/docker-compose.yml",
        "exec", "-T", "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ],
      "cwd": "AGENTCEPTION_REPO_ROOT"
    }
  }
}
```

Replace `AGENTCEPTION_REPO_ROOT` with the absolute path to your local clone (e.g. `/Users/you/dev/agentception`).

## Running alongside other MCP servers

If you also run other MCP servers (e.g. a music composition backend), add them as independent top-level keys — they do not interfere with each other:

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose", "-f", "AGENTCEPTION_REPO_ROOT/docker-compose.yml",
        "exec", "-T", "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ],
      "cwd": "AGENTCEPTION_REPO_ROOT"
    },
    "other-service": {
      "command": "docker",
      "args": ["compose", "-f", "OTHER_REPO_ROOT/docker-compose.yml", "exec", "-T", "other-service", "python", "-m", "other.mcp.stdio_server"],
      "cwd": "OTHER_REPO_ROOT"
    }
  }
}
```

## Prerequisites

- AgentCeption containers must be running: `docker compose up -d`
- Verify the MCP server responds: `docker compose exec agentception python -m agentception.mcp.stdio_server`

## Three kinds of MCP endpoints

AgentCeption exposes all three MCP endpoint types:

| Kind | Purpose | How to call |
|------|---------|-------------|
| **Tools** | Actions with side effects (mutate state, post comments, start agents) | `CallMcpTool(server="user-agentception", toolName=..., arguments={...})` |
| **Resources** | Pure reads — stateless, cacheable, side-effect-free | `FetchMcpResource(server="user-agentception", uri="ac://...")` |
| **Prompts** | Agent role files and briefing templates | `prompts/get(name="role/python-developer")` or `prompts/list` |

### Resource URI catalogue

| URI | What it returns |
|-----|----------------|
| `ac://runs/active` | All live runs (pending_launch, implementing, reviewing, blocked) |
| `ac://runs/pending` | Runs queued for Dispatcher launch |
| `ac://runs/{run_id}` | Metadata for one run |
| `ac://runs/{run_id}/children` | Child runs spawned by this run |
| `ac://runs/{run_id}/events` | Structured event log; append `?after_id=N` to paginate |
| `ac://runs/{run_id}/context` | Raw task context TOML text for one run |
| `ac://batches/{batch_id}/tree` | All runs in a batch |
| `ac://system/dispatcher` | Dispatcher counters and active batch_id |
| `ac://system/health` | DB reachability and per-status counts |
| `ac://system/config` | Pipeline label config (canonical label names) |
| `ac://plan/schema` | PlanSpec JSON Schema |
| `ac://plan/labels` | GitHub label catalogue |
| `ac://plan/figures/{role}` | Cognitive-arch figures for a role slug |
| `ac://arch/figures` | All cognitive architecture figures |
| `ac://arch/archetypes` | All cognitive architecture archetypes |
| `ac://arch/figures/{figure_id}` | One cognitive architecture figure by ID |
| `ac://arch/archetypes/{archetype_id}` | One cognitive architecture archetype by ID |
| `ac://arch/skills/{skill_id}` | One skill domain by ID |
| `ac://arch/atoms/{atom_id}` | One cognitive atom dimension by ID |
| `ac://roles/list` | All available role slugs |
| `ac://roles/{slug}` | Full role definition Markdown for a slug |

### Prompt catalogue

`prompts/list` returns every compiled role and agent prompt.  `prompts/get(name=...)` returns the full Markdown content as a `user` message.

Naming conventions:
- `role/<slug>` — role definition (e.g. `role/python-developer`, `role/cto`)
- `agent/<name>` — agent prompt (e.g. `agent/dispatcher`, `agent/engineer`, `agent/reviewer`)

## MCP Auto-Approval

Auto-approval is tiered by risk — resources (all reads) and observability tools are
auto-approved; tools that reach outside the service boundary (filing GitHub issues,
starting agents, advancing phase gates) always require an explicit human confirmation.

```json
{
  "mcpServers": {
    "agentception": {
      "url": "http://localhost:1337/api/mcp",
      "autoApprove": [
        "plan_validate_spec",
        "plan_validate_manifest",
        "log_run_step",
        "log_run_error"
      ]
    }
  }
}
```

### Approval tiers

| Tier | Endpoints | Rationale |
|------|-----------|-----------|
| **Auto — resources** | All `ac://` URIs | Pure reads — no external effects, always safe. |
| **Auto — prompts** | All `role/*` and `agent/*` | Static file reads — no effects. |
| **Auto — tools** | `plan_validate_spec`, `plan_validate_manifest` | In-memory validation only. |
| **Auto — tools** | `log_run_step`, `log_run_error` | Append-only DB writes — no external effects. |
| **Prompt** | `build_claim_run`, `build_complete_run`, `build_cancel_run` | Pipeline state transitions in the DB — recoverable but worth confirming. |
| **Prompt** | `github_add_label`, `github_remove_label`, `github_add_comment` | External GitHub API mutations. |
| **Always prompt** | `build_spawn_adhoc_child`, `plan_advance_phase` | Create real GitHub issues, git worktrees, and live agents — irreversible side effects. |

**What this means for you:**

- Resource reads (`FetchMcpResource`), prompt fetches, and observability tool calls happen without interruption.
- `build_spawn_adhoc_child` and `plan_advance_phase` always require explicit confirmation — a mis-fire creates real GitHub issues and running agent processes that are hard to undo.
- The HTTP endpoint is available at `http://localhost:1337/api/mcp` once containers are running.

## Available tools, resources, and prompts

| Module | What it registers |
|--------|-------------------|
| `agentception/mcp/server.py` | Tool catalogue (`TOOLS`), `list_prompts()`, all JSON-RPC handlers |
| `agentception/mcp/resources.py` | Resource + template catalogue, `read_resource()` dispatcher |
| `agentception/mcp/prompts.py` | Prompt catalogue, `get_prompt()` dispatcher |

Any MCP-aware client enumerates all three automatically once the server entry is configured.

## Elicitation — human-in-the-loop for running agents

AgentCeption implements the [MCP 2025-11-25 elicitation protocol](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation) to let any running agent pause and ask the human operator for structured input — a decision, a credential, a deployment target — without aborting the run.

### How it works

```
Agent calls request_human_input(message, fields)
        ↓
MCP server puts elicitation/create in the session's outbound queue
        ↓ (SSE stream)
Mission Control dashboard receives the event
        ↓
Human sees a modal form, fills it in, clicks Submit
        ↓
Dashboard POSTs the JSON-RPC response to POST /api/mcp
        ↓
MCP server resolves the asyncio Future
        ↓
Agent receives {action: "accept", content: {...}} and continues
```

### Session lifecycle

1. The dashboard opens Mission Control (`/ship`) — the page auto-connects as an MCP client:
   - POSTs `initialize` with `capabilities.elicitation.form` declared.
   - Stores the `MCP-Session-Id` returned in the response header.
   - Opens `GET /api/mcp` with `Accept: text/event-stream` and the session header.
2. When an agent calls `request_human_input`, the server finds the connected session and puts an `elicitation/create` request in its outbound queue.
3. The SSE stream delivers it to the browser within milliseconds.
4. The dashboard renders a form modal. Human fills it in and submits.
5. Dashboard POSTs the JSON-RPC response. Server resolves the agent's blocking `await`.
6. On page unload, the dashboard DELETEs the session.

### The `request_human_input` tool

```json
{
  "name": "request_human_input",
  "message": "Which environment should I deploy to?",
  "fields": [
    {
      "name": "environment",
      "type": "string",
      "title": "Environment",
      "enum": ["dev", "staging", "prod"],
      "required": true
    },
    {
      "name": "confirm_migration",
      "type": "boolean",
      "title": "Run DB migration",
      "default": false
    }
  ],
  "run_id": "issue-938",
  "timeout_seconds": 300
}
```

**Return values:**

| `action` | When | `content` |
|----------|------|-----------|
| `accept` | Human submitted the form | `{"environment": "staging", "confirm_migration": true}` |
| `decline` | Human clicked Decline | _(absent)_ |
| `cancel` | Human dismissed the modal | _(absent)_ |
| `timeout` | No response within `timeout_seconds` | _(absent)_ |
| `no_client` | No dashboard session connected | _(absent)_ |

### When to use it

- Deployment gate: "Deploy to prod?" — requires human approval.
- Credential input: "Enter the API key for this service."
- Architectural decision: "I found two valid approaches — which do you prefer?"
- Emergency stop: "I'm about to delete 200 issues. Are you sure?"

The tool is designed to be a **last resort**, not a crutch. Agents should make autonomous decisions when they have enough context. Reserve `request_human_input` for genuine unknowns where guessing wrong costs real time or money.

### No-client graceful degradation

When no browser session with elicitation capability is connected (no Mission Control tab open), the tool returns immediately with `action: "no_client"`. Agents must handle this case — either abort gracefully, fall back to a safe default, or log an error and cancel the run with `build_cancel_run`.

## Related guides

- [Standalone Agent Loop](agent-loop.md) — run agents without an IDE, using the MCP HTTP transport as the tool execution bridge
- [Security Guide](security.md) — API key auth, TLS, denylist, and threat model
