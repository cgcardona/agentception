# .agentception/

Runtime configuration directory for the AgentCeption orchestration system.

## Structure

```
.agentception/
├── roles/                          — Agent role definition files (markdown). Each file defines a
│                                     cognitive architecture: decision hierarchy, quality bar, and
│                                     embedded kickoff prompts used by the Dispatcher.
├── prompts/                        — Prompt templates used by the dispatch pipeline.
├── dispatcher.md                   — The AgentCeption Dispatcher agent prompt (drains the pending
│                                     launch queue and assigns issues to role-matched agents).
├── agent-task-spec.md              — Full spec for `.agent-task` files: fields, validation rules,
│                                     and lifecycle.
├── agent-command-policy.md         — Policy for which shell commands agents may run, with
│                                     justifications.
├── cognitive-arch-enrichment-spec.md — Spec for enriching issue bodies with cognitive-arch tags.
├── conflict-rules.md               — Rules for detecting and resolving agent-task conflicts.
├── multi-tier-agent-architecture.md — Full spec for the multi-tier agent tree (tiers, scopes,
│                                     GitHub query strategy).
├── pipeline-config.json            — Active project configuration (gh_repo, repo_dir,
│                                     worktrees_dir, active_project). Read at startup by
│                                     agentception/config.py to apply project-specific path
│                                     overrides.
├── pipeline-howto.md               — Operator guide: how to configure and operate the pipeline.
├── stress-test-agent-kickoff.md    — Kickoff prompt template for stress-test agents.
├── stress-test-parallelism.md      — Protocol for parallel stress-test runs.
└── README.md                       — This file.
```

## Path resolution

The canonical path to this directory is exposed via `settings.ac_dir` in
`agentception/config.py`:

```python
@property
def ac_dir(self) -> Path:
    return self.repo_dir / ".agentception"
```

`repo_dir` defaults to `Path.cwd()` and is overridden by the `AC_REPO_DIR`
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
      AC_REPO_DIR: /app
```

With `AC_REPO_DIR=/app`, `settings.ac_dir` resolves to `/app/.agentception`,
which matches the bind-mount target.

## Development vs. production

| Context | `AC_REPO_DIR` | `settings.ac_dir` |
|---------|---------------|-------------------|
| Docker container | `/app` | `/app/.agentception` |
| Host (default) | `$PWD` (repo root) | `<repo>/.agentception` |
| Override | Set `AC_REPO_DIR` | `$AC_REPO_DIR/.agentception` |

The live runtime copy that persists across restarts lives at
`$HOME/.agentception` on the developer's machine. The repo copy here is the
version-controlled source of truth; Docker bind-mounts it so changes are
visible inside the container without a rebuild.
