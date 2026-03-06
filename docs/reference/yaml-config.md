# YAML Configuration Reference

AgentCeption's agent prompt system is driven by three YAML files in `scripts/gen_prompts/`. Edit these, then run `generate.py` to regenerate all prompt files.

```
scripts/gen_prompts/
  config.yaml        ← Pipeline config: repo, phases, labels, codebases
  team.yaml          ← Agent org chart: which cognitive arch each role gets
  role-taxonomy.yaml ← Full role catalog: org hierarchy, spawnable flags
```

---

## `config.yaml` — Pipeline Configuration

**Source:** `scripts/gen_prompts/config.yaml`

After editing, regenerate:
```bash
docker compose exec agentception python3 scripts/gen_prompts/generate.py
# Then sync labels to GitHub (if labels changed):
bash scripts/gen_prompts/sync_labels.sh
```

### `repo`

```yaml
repo:
  gh_slug: "cgcardona/agentception"   # GitHub org/repo — never derived from local path
  name: "agentception"                # used in worktree subfolder names
```

| Key | Description |
|-----|-------------|
| `gh_slug` | GitHub `owner/repo`. This is the canonical repo for all GitHub API calls. **Never derived from local path.** |
| `name` | Short name used in worktree directory naming. |

---

### `pipeline`

```yaml
pipeline:
  claim_label: "agent:wip"
  max_pool_size: 0
  phases:
    - "ac-workflow/0-foundation"
    - "ac-workflow/1-generation"
```

| Key | Description |
|-----|-------------|
| `claim_label` | GitHub label applied to issues when an agent claims them. Remove to release. Default: `"agent:wip"`. |
| `max_pool_size` | Unused — agents are spawned without a concurrency cap. Set to `0`. |
| `phases` | **Strict phase order** for the active initiative. The CTO and chain-spawn logic iterate this list top-to-bottom. Add/remove phases here, then update the matching `labels.phases` section and re-run `generate.py`. |

> **Phase naming convention:** Use `{initiative}/{N}-{semantic-slug}` where N is the 0-based index. Example: `ac-workflow/0-foundation`, `ac-workflow/1-generation`. The numeric prefix makes lexicographic sort a correct fallback; `phase_order` in the DB is canonical for filed plans.

---

### `codebases`

Defines how agents run mypy and tests for each supported codebase. The `active` key tells AgentCeption which codebase is currently being worked on — this drives the `IS_AC` routing in agent prompts.

```yaml
codebases:
  active: "agentception"

  agentception:
    container: "agentception"
    mypy: 'docker compose exec agentception sh -c "PYTHONPATH=/worktrees/$WTNAME mypy /worktrees/$WTNAME/agentception/"'
    test_dir: "agentception/tests"
    test_glob: "agentception/tests/test_*.py"
    label_prefix: "ac-"
```

| Key | Description |
|-----|-------------|
| `active` | Key of the codebase agents are currently working on. |
| `container` | Docker service name to `exec` into. |
| `mypy` | Full mypy command. `$WTNAME` is substituted with the worktree slug. |
| `test_dir` | Directory containing tests. |
| `test_glob` | Glob pattern for test files. |
| `label_prefix` | GitHub label prefix for this codebase's phase labels. |

---

### `labels`

Single source of truth for all GitHub labels. Running `sync_labels.sh` creates/updates them.

```yaml
labels:
  claim:
    name: "agent:wip"
    color: "0075ca"
    description: "Claimed by a pipeline agent — do not assign manually"

  project:
    name: "ac-workflow"
    color: "7c3aed"
    description: "AgentCeption workflow initiative"

  phases:
    - name: "ac-workflow/0-foundation"
      color: "d63939"
      description: "Phase 0 — foundation work"
    - name: "ac-workflow/1-generation"
      color: "6741d9"
      description: "Phase 1 — generation"

  utility:
    - name: "bug"
      color: "d73a4a"
      description: "Something isn't working"
    - name: "enhancement"
      color: "a2eeef"
      description: "New feature or request"
```

| Section | Description |
|---------|-------------|
| `claim` | The `agent:wip` claim label — applied/removed at runtime by agents. |
| `project` | Top-level initiative label (e.g. `ac-workflow`). Update when switching projects. |
| `phases` | Phase labels — must match `pipeline.phases` in name and order. |
| `utility` | Standard triage/status labels (bug, enhancement, documentation, etc.). |

> **Colors** are 6-digit hex without the leading `#`.

---

## `team.yaml` — Agent Cognitive Architecture

**Source:** `scripts/gen_prompts/team.yaml`

Defines which cognitive architecture is assigned to each role. The `cognitive_arch` field from each role flows directly into `.agent-task` files and the LLM context.

### Structure

```yaml
org:
  c_suite:
    cto:
      figures: [von_neumann]
      archetype: the_architect
      skills: []
      cognitive_arch: "von_neumann"

  vps:
    engineering_manager:
      figures: [dijkstra]
      archetype: the_scholar
      skills: [python, fastapi]
      cognitive_arch: "dijkstra:python:fastapi"

  engineering:
    python_developer:
      figures: [turing]
      archetype: the_pragmatist
      skills: [python, fastapi, postgresql]
      cognitive_arch: "turing:python:fastapi:postgresql"

    pr_reviewer:
      figures: [knuth]
      archetype: the_scholar
      skills: [python]
      cognitive_arch: "knuth:python"
```

### `cognitive_arch` string format

```
figures:skill1:skill2:...

figures — comma-separated figure or archetype IDs
skills  — colon-separated skill domain IDs

Examples:
  "turing:python"                      — single figure + one skill
  "dijkstra:python:fastapi"            — single figure + two skills
  "lovelace,shannon:htmx:d3:python"   — two-figure blend + three skills
  "the_architect:python:fastapi"       — archetype + two skills
```

Figure IDs come from `scripts/gen_prompts/cognitive_archetypes/figures/`.
Skill domain IDs come from `scripts/gen_prompts/cognitive_archetypes/skill_domains/`.
Archetype IDs come from `scripts/gen_prompts/cognitive_archetypes/archetypes/`.

### `atom_overrides`

Optional. Override default behavioral atoms for a specific role:

```yaml
cto:
  figures: [von_neumann]
  atom_overrides:
    cognitive_rhythm: burst   # think in bursts, not incrementally
  cognitive_arch: "von_neumann"
```

Atom IDs come from `scripts/gen_prompts/cognitive_archetypes/atoms/`.

---

## `role-taxonomy.yaml` — Full Role Catalog

**Source:** `scripts/gen_prompts/role-taxonomy.yaml`

Defines the complete org chart: every role at every tier, spawnable flags, and compatible cognitive architecture figures.

### Structure

```yaml
levels:
  - id: c_suite
    label: "C-Suite"
    description: "Executive leaders..."
    roles:
      - slug: cto
        label: "CTO"
        title: "Chief Technology Officer"
        category: executive
        description: "..."
        spawnable: false
        compatible_figures:
          - von_neumann
          - turing
          - dijkstra

  - id: vps
    label: "VPs"
    roles:
      - slug: engineering-manager
        label: "Engineering Manager"
        category: coordinator
        spawnable: true
        compatible_figures:
          - dijkstra
          - knuth

  - id: engineering
    label: "Engineering"
    roles:
      - slug: python-developer
        label: "Python Developer"
        category: leaf
        spawnable: true
        compatible_figures:
          - turing
          - lovelace
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | Unique identifier — used in `.agent-task` files and API calls |
| `label` | string | Human display name |
| `title` | string | Full job title |
| `category` | string | `executive`, `coordinator`, or `leaf` |
| `spawnable` | bool | `true` = can be dispatched via the Ship board's Launch button |
| `compatible_figures` | list | Which cognitive architecture figures can be used for this role |

### How it's used

- The org chart browser at `/org-chart` renders this taxonomy visually
- `GET /api/org/taxonomy` returns it as JSON for the frontend
- `GET /api/dispatch/context` filters roles by label scope using this file
- Agent prompts reference role slugs to resolve their role file (`~/.agentception/roles/{slug}.md`)

---

## Generating prompt files

After editing any of these YAML files:

```bash
# Regenerate all .agentception/roles/*.md and prompt templates
docker compose exec agentception python3 scripts/gen_prompts/generate.py

# Sync GitHub labels (only needed after changing labels section)
bash scripts/gen_prompts/sync_labels.sh

# Verify cognitive arch resolution for a specific role
docker compose exec agentception python3 scripts/gen_prompts/resolve_arch.py turing:python
```

The `generate.py` script renders every `.j2` file in `scripts/gen_prompts/templates/` using the YAML context. Files under `templates/snippets/` are skipped (they are included by other templates, not rendered directly).

> **Never edit generated files directly.** The `.agentception/roles/*.md` files are generated — any manual edits will be overwritten on the next `generate.py` run. Edit the source templates instead.

---

## Switching to a new initiative / project

1. Update `config.yaml`:
   - Change `pipeline.phases` to the new phase list
   - Update `labels.project` to the new initiative label
   - Update `labels.phases` to match
   - Update `codebases.active` if the codebase changed

2. Run `generate.py` to regenerate prompt files.

3. Run `sync_labels.sh` to create the new labels in GitHub.

4. Use the Plan page to generate a PlanSpec for the new initiative, then file issues. The `initiative_phases` rows written at filing time are the canonical phase order for the board — `config.yaml` phases drive the agent prompts, not the board display order.
