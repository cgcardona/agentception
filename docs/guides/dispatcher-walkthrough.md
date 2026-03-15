# Dispatcher walkthrough — run agents from an MCP client (step-by-step)

This guide gets you from “Mission Control Idle” to agents running on the Ship board by configuring an MCP client to act as the **Dispatcher** (this doc uses Cursor as the example): it claims pending runs and starts the agent loop. Follow every step in order.

---

## What you’re doing

1. **AgentCeption** is already running in Docker; you used the Plan/Ship flow and clicked **Launch**, so at least one run is in `pending_launch`.
2. **Cursor** will run as the Dispatcher: it calls MCP tools to list pending runs and claim each one. Claiming a run starts the agent loop inside the container; the Ship board then shows activity.
3. You only need to **configure MCP once** and **allowlist the tools once**. After that, “run the Dispatcher” in Cursor whenever you have pending launches.

---

## Prerequisites

- Docker stack is up: `docker compose up -d` (you already did this).
- You have already created a plan and used **Launch** from the Plan or Ship UI (so there is at least one run in `pending_launch`). If you’re not sure, you can still do the MCP setup; the Dispatcher will simply see zero pending runs.

---

## Step 1 — Open your MCP config file

On macOS/Linux the file is:

```text
~/.cursor/mcp.json
```

That’s `$HOME/.cursor/mcp.json`, e.g. `/Users/aaronrenecarvajal/.cursor/mcp.json`.

- If the file doesn’t exist, create it.
- If it exists and already has other servers, you’ll add an `agentception` entry alongside them (see Step 2).

---

## Step 2 — Add the AgentCeption MCP server

Paste the block below into `~/.cursor/mcp.json`. If the file already has a `"mcpServers"` object, add only the `"agentception"` entry inside it and keep your other servers.

**Use your real repo path.** Replace `/Users/aaronrenecarvajal/agentception` with the path where your AgentCeption repo lives (no trailing slash).

```json
{
  "mcpServers": {
    "agentception": {
      "command": "docker",
      "args": [
        "compose",
        "-f", "/Users/aaronrenecarvajal/agentception/docker-compose.yml",
        "exec", "-T",
        "agentception",
        "python", "-m", "agentception.mcp.stdio_server"
      ],
      "cwd": "/Users/aaronrenecarvajal/agentception"
    }
  }
}
```

Save the file.

---

## Step 3 — Restart Cursor

Fully quit Cursor (Cmd+Q on macOS) and open it again so it reloads `mcp.json`. Re-open your AgentCeption workspace.

---

## Step 4 — Confirm the stack is running

In a terminal, from your AgentCeption repo:

```bash
cd /Users/aaronrenecarvajal/agentception
docker compose ps
```

All three services (agentception, agentception-postgres, agentception-qdrant) should be `running`. If not:

```bash
docker compose up -d
```

---

## Step 5 — Run the Dispatcher in Cursor

1. In Cursor, open the **Chat** panel (or start a new Composer session).
2. Paste this instruction (you can tweak the wording; the important part is to use the AgentCeption MCP to list and claim pending runs):

   ```text
   Use the AgentCeption MCP server. Read the resource ac://runs/pending to list pending runs. If the result has any pending runs, for each run call build_claim_run with that run’s run_id. Do not skip any pending run. After each build_claim_run, the agent loop for that run will start on the server.
   ```

3. Send the message.

4. **First time only:** Cursor will ask you to approve MCP tool calls. For each tool:
   - Prefer **“Allowlist MCP Tool”** (or equivalent) so future Dispatcher runs don’t prompt again.
   - If you only see “Run”, click it; you can allowlist later when prompted again.

   Endpoints you’ll use:
   - `ac://runs/pending` resource — lists runs waiting to be claimed (read via `resources/read`)
   - `build_claim_run` tool — claims one run (pass `run_id` from the list)

   If Cursor keeps asking every time even after allowlisting, see the [Cursor bug workaround](#if-cursor-keeps-asking-after-allowlist) at the end of this guide.

5. Wait until the model has finished. It should read `ac://runs/pending` once, then call `build_claim_run` once per pending run.

---

## Step 6 — Check the Ship board

1. In your browser, open the Ship page (e.g. `http://localhost:10003/ship/...` for your project/initiative).
2. Refresh the page.
3. You should see:
   - The run(s) no longer stuck in “Mission Control Idle”
   - Activity in ACTIVE (or the appropriate column) and/or live logs if you open an issue.

If nothing moved, see [Troubleshooting](#troubleshooting) below.

---

## Next times

- Whenever you **Launch** from the Plan/Ship flow, new runs are created in `pending_launch`.
- In Cursor, run the Dispatcher again with the same instruction (Step 5). Once tools are allowlisted, it should run without extra prompts and claim all pending runs.

You don’t need to edit `mcp.json` or restart Cursor again unless you change the repo path or add another MCP server.

---

## Troubleshooting

**“No pending runs” / Ship still idle**

- Make sure you actually clicked **Launch** (or equivalent) in the Plan or Ship UI so that `POST /api/dispatch/label` (or the label-dispatch flow) ran.
- Check container logs: `docker compose logs agentception --tail 100`. Look for `pending_launch` or errors around dispatch.

**Cursor says the MCP server failed or doesn’t list tools**

- Confirm `docker compose ps` shows the stack running.
- From the repo root run:  
  `docker compose exec -T agentception python -m agentception.mcp.stdio_server`  
  If that errors, fix the container first (e.g. `docker compose up -d`, check `.env`).
- Ensure the path in `mcp.json` is the **absolute** path to your AgentCeption repo and that `cwd` matches it.
- Restart Cursor again after any `mcp.json` change.

**Endpoints have different names**

- Use the resource `ac://runs/pending` to list pending runs and the tool `build_claim_run` to claim each. Use the names Cursor shows in the MCP panel; the instruction in Step 5 still applies (list pending, then claim each by `run_id`).

---

## If Cursor keeps asking after allowlist

Some Cursor versions don’t persist MCP allowlists. If you allowlist the tools but Cursor still prompts on every run:

1. Quit Cursor completely (Cmd+Q).
2. Run the script mentioned in [Setup Guide — Step N](setup.md#if-prompts-keep-appearing-after-allowlisting-cursor-bug) (it patches Cursor’s internal DB so allowlist/auto-continue is respected).
3. Restart Cursor and run the Dispatcher again.

---

## Summary

| Step | Action |
|------|--------|
| 1 | Open or create `~/.cursor/mcp.json` |
| 2 | Add `agentception` server with your repo path and `cwd` |
| 3 | Restart Cursor |
| 4 | Ensure `docker compose up -d` and containers are running |
| 5 | In Chat, ask the model to use AgentCeption MCP: read `ac://runs/pending`, then for each run call `build_claim_run` with that `run_id`; allowlist tools when prompted |
| 6 | Refresh the Ship board and confirm runs are active |

After that, “run the Dispatcher” in Cursor whenever you have new pending launches from the Plan/Ship flow.
