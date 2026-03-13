# Knowtation Integration — AgentCeption Org Brain

This guide documents how **Knowtation** (personal/team knowledge vault) and **AgentCeption** (multi-agent orchestration) work together: agents use the vault for context before and during tasks, and write back plan and phase summaries so the vault acts as the org brain.

## Why integrate

| Knowtation | AgentCeption |
|------------|--------------|
| Capture, index, search, export notes and specs | Brain dump → PlanSpec → issues → agent org → PRs |
| Marketing-style agent tooling (notes, context) | CTO → coordinators → engineers in worktrees |

**Fit:** AgentCeption’s agents **read** the vault for context (specs, project notes, prior summaries) and **write** back plans and phase summaries so future runs and humans can search and reuse that knowledge.

## Intended flows

1. **Before/during tasks:** Agents run `knowtation search <query>` or use Knowtation MCP tools to pull relevant notes (spec, project conventions, past phase summaries) into context before implementing.
2. **After Create Issues or after each phase:** A bridge script (or future hook) writes a phase/plan summary into the vault at a conventional path so it can be found later (e.g. `vault/projects/<repo>/plans/<batch_id>-summary.md`).
3. **Brain-dump input:** Vault content (e.g. a spec or project brief) can be pasted into the planning UI as the initial brain dump; the LLM turns it into a PlanSpec.

## Integration options

### K1 — CLI in agent environment (recommended first)

- Install the [Knowtation CLI](https://github.com/aaronrene/knowtation) in the agent environment (container or host where agents run).
- Set `KNOWTATION_VAULT_PATH` to the vault root.
- Agents use:
  - `knowtation search <query>` — find relevant notes.
  - `knowtation get-note <path>` — read a note by path.
  - `knowtation write <path> --stdin` — write content (e.g. phase summary) into the vault.
- Optional bridge: after Create Issues or after a phase completes, run a script that fetches the initiative/phase summary and pipes it to `knowtation write ...` (see [Bridge script](#bridge-script-write-phase-summary)).

### K2 — Knowtation MCP server

- Run a Knowtation MCP server alongside AgentCeption (same host or container).
- Add it to `~/.cursor/mcp.json` so Cursor/Claude and AgentCeption agents can call vault tools (`search`, `get_note`, `list_notes`, `write`).
- Document the Knowtation server in this guide and in [MCP guide](mcp.md) so agents know to use it for vault access.
- One core shared between CLI and MCP keeps behavior consistent.

### K3 (optional) — Conventions and provenance

- **Paths:** e.g. `vault/projects/<owner>/<repo>/plans/<batch_id>-summary.md`, `.../phases/<phase_label>-summary.md`.
- **Frontmatter:** `source: agentception`, `repo`, `initiative`, `batch_id`, `phase`, `created_at` for search and provenance.
- **Provenance:** Tag agent-written notes so humans and agents can filter by source.

## Bridge script: write phase summary

A minimal integration is to write a **plan or phase summary** into the vault after Create Issues (or after each phase). The repo provides a small script that:

1. Accepts summary content on stdin (or fetches it from the AgentCeption API if you pass `repo`, `initiative`, `batch_id`).
2. Writes it to a conventional path via the Knowtation CLI, or prints the path and command if the CLI is not installed.

**Script:** `scripts/knowtation_write_phase_summary.sh`

**Usage (with Knowtation CLI installed):**

```bash
# After Create Issues: pipe a summary into the vault.
# Path convention: vault/projects/<repo>/plans/<batch_id>-summary.md
echo "$SUMMARY_MARKDOWN" | scripts/knowtation_write_phase_summary.sh "owner/repo" "my-initiative" "batch-abc123"

# Or feed from a file.
cat plan-summary.md | scripts/knowtation_write_phase_summary.sh "owner/repo" "my-initiative" "batch-abc123"
```

**Getting the summary from AgentCeption:**

- Use the shareable plan API (e.g. `GET /api/plan/initiative/<repo>/<initiative>/<batch_id>/summary` if available) or the internal `get_initiative_summary()` and render to Markdown.
- Alternatively, generate a short summary in your own script from the PlanSpec and issue list, then pipe it into the bridge script.

**If Knowtation CLI is not installed:** The script prints the target path and the exact `knowtation write` command so you can run it once the CLI is available.

## Running Knowtation MCP alongside AgentCeption

In `~/.cursor/mcp.json` you can run both servers:

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
    "knowtation": {
      "command": "knowtation",
      "args": ["mcp"],
      "cwd": "KNOWTATION_VAULT_PATH_OR_REPO_ROOT"
    }
  }
}
```

Adjust `knowtation` entry per [Knowtation’s MCP docs](https://github.com/aaronrene/knowtation); use the actual command and `cwd` for the Knowtation server.

## References

- [Knowtation](https://github.com/aaronrene/knowtation) — vault CLI and MCP.
- [AgentCeption MCP](mcp.md) — tools and resources.
- [HACKATHON.md](../../HACKATHON.md) — hackathon context and K1/K2/K3 phase plan.
