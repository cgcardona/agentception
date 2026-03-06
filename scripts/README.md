# scripts/

Cognitive architecture scripts for the AgentCeption orchestration system.

## resolve_arch.py

Assembles a full agent context block from a cognitive architecture string.

```bash
# Usage: figure:skill1:skill2 (colon-separated)
python3 scripts/gen_prompts/resolve_arch.py "feynman:python"
python3 scripts/gen_prompts/resolve_arch.py "ritchie:devops" --mode implementer
python3 scripts/gen_prompts/resolve_arch.py "knuth:python" --mode reviewer
python3 scripts/gen_prompts/resolve_arch.py "dijkstra:postgresql:python" --fingerprint \
  --role python-developer --session abc123 --batch batch-01
```

Output is ready-to-read Markdown. Consumed by agent kickoff prompts.

## gen_cognitive_arch_tasks.py

Generates `.agent-task` files from a list of figure batches for cognitive
architecture enrichment work.

```bash
# Generate task files (default output: /tmp/cog-arch-tasks/)
python3 scripts/gen_cognitive_arch_tasks.py generate

# Specify repo path and output directory
python3 scripts/gen_cognitive_arch_tasks.py generate \
  --repo /path/to/agentception --out-dir ~/.agentception/tasks

# Clean up completed task files and record to DB (requires DATABASE_URL)
DATABASE_URL=postgresql://... python3 scripts/gen_cognitive_arch_tasks.py cleanup \
  --tasks-dir ~/.agentception/tasks --repo /path/to/agentception
```

Requires `DATABASE_URL` env var when recording task completions to the
AgentCeption Postgres instance.

## gen_prompts/generate.py

Regenerates all `.agentception/roles/*.md` and `.agentception/parallel-*.md` files from
`config.yaml` and the Jinja2 templates in `scripts/gen_prompts/templates/`.

```bash
# Run inside the agentception container (bind-mounts repo to /app):
docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py

# Or on the host (pyyaml and jinja2 must be installed):
python3 scripts/gen_prompts/generate.py

# Dry-run (shows what would change without writing):
python3 scripts/gen_prompts/generate.py --check
```

After editing `config.yaml` or any template, run this to regenerate. Then run
`bash scripts/gen_prompts/sync_labels.sh` if label definitions changed.

## YAML schema

Architecture components live in `scripts/gen_prompts/cognitive_archetypes/`:

| File pattern | Purpose |
|---|---|
| `figures/*.yaml` | Cognitive figure definitions (thinking style, strengths, blind spots) |
| `archetypes/*.yaml` | Named bundles of atoms (`the_architect`, `the_guardian`, etc.) |
| `skill_domains/*.yaml` | Technical skill fragments injected orthogonally to personality |
| `atoms/*.yaml` | Primitive cognitive dimensions (epistemic style, quality bar, etc.) |

Top-level support files:

| File | Purpose |
|---|---|
| `scripts/gen_prompts/config.yaml` | Single source of truth: repo slug, phases, codebase routing, label definitions |
| `scripts/gen_prompts/role-taxonomy.yaml` | Maps issue keywords to recommended cognitive architectures |
| `scripts/gen_prompts/team.yaml` | Skill keyword routing for automatic skill-domain selection |
| `scripts/gen_prompts/sync_labels.sh` | Auto-generated; run once to sync GitHub labels |
| `scripts/gen_prompts/templates/` | Jinja2 templates for all `.agentception/` prompt files |

## Architecture string format

```
figure[:skill1[:skill2[...]]]
```

Examples:

| String | Meaning |
|---|---|
| `feynman` | Feynman figure only |
| `feynman:python` | Feynman + Python skill domain |
| `dijkstra:postgresql:python` | Dijkstra + two skill domains |
| `the_architect:devops` | Archetype + DevOps skill domain |
