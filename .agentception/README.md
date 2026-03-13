# .agentception/

Runtime configuration directory for the AgentCeption orchestration system.

## Structure

```
.agentception/
├── roles/                          — Agent role definition files (markdown). Each file defines a
│                                     coordinator or worker: decision hierarchy, quality bar, and
│                                     embedded kickoff prompts. All files are generated from
│                                     scripts/gen_prompts/templates/roles/*.md.j2 — never edit
│                                     the .md files directly; edit the .j2 source instead.
├── agent-engineer.md               — Engineering worker agent prompt (implement a GitHub issue).
├── agent-reviewer.md               — PR reviewer agent prompt (review and merge a pull request).
├── agent-conductor.md              — Agent conductor prompt (coordinate multi-step workflows).
├── agent-command-policy.md         — Policy for which shell commands agents may run.
├── agent-task-spec.md              — DB-backed RunContextRow field reference for agents.
├── cognitive-arch-enrichment-spec.md — Spec for enriching issues with cognitive-arch tags.
├── conflict-rules.md               — Rules for detecting and resolving concurrent agent conflicts.
├── pipeline-config.json            — Active project configuration (gh_repo, pool_size,
│                                     coordinator_limits, approval gates, etc.).
├── pipeline-howto.md               — Operator guide: phase-gate, dependency, and label conventions.
└── README.md                       — This file.
```

## Role taxonomy

Roles fall into two categories that map to tree nodes and leaf nodes:

| Category | Description | Examples |
|----------|-------------|---------|
| **Coordinator** | Receives delegated scope, delegates to sub-coordinators or workers | `engineering-coordinator`, `cto`, `ceo` |
| **Worker** | Performs leaf-level work, produces an artifact | `developer`, `reviewer`, `content-writer` |

All roles in `roles/` are served over MCP as `role/<slug>` prompts and included in template archives. C-suite roles (`ceo`, `cto`, etc.) are coordinators at the top of the tree.

## Path resolution

The canonical path to this directory is exposed via `settings.ac_dir` in
`agentception/config.py`:

```python
@property
def ac_dir(self) -> Path:
    return self.repo_dir / ".agentception"
```

`repo_dir` defaults to `Path.cwd()` and is overridden by the `REPO_DIR`
environment variable or by the active project entry in `pipeline-config.json`.

**All code that needs a path inside `.agentception/` must use `settings.ac_dir`.**
Never construct a path using `__file__`, `os.getcwd()`, or a hardcoded string.

## In Docker

`docker-compose.yml` bind-mounts this directory into the container so the
development copy on disk is visible at the path the app expects:

```yaml
services:
  agentception:
    volumes:
      - ./.agentception:/app/.agentception
    environment:
      REPO_DIR: /app
```

With `REPO_DIR=/app`, `settings.ac_dir` resolves to `/app/.agentception`,
which matches the bind-mount target.

## Development vs. production

| Context | `REPO_DIR` | `settings.ac_dir` |
|---------|---------------|-------------------|
| Docker container | `/app` | `/app/.agentception` |
| Host (default) | `$PWD` (repo root) | `<repo>/.agentception` |
| Override | Set `REPO_DIR` | `$REPO_DIR/.agentception` |

The live runtime copy that persists across restarts lives at
`$HOME/.agentception` on the developer's machine. The repo copy here is the
version-controlled source of truth; Docker bind-mounts it so changes are
visible inside the container without a rebuild.

## Editing roles

1. Edit the `.j2` source in `scripts/gen_prompts/templates/roles/`
2. Run `docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py`
3. Commit both the template and the regenerated `.md` together
4. Run `generate.py --check` before opening a PR to confirm no drift
