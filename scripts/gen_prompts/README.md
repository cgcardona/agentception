# Prompt-as-Code — Agent Prompt Generator

> **Commands can run inside the agentception Docker container or directly on the host.**

The agent prompt files (`.agentception/roles/*.md`) are
**generated** — never hand-edited. One config file (`config.yaml`) drives
everything: repo slug, phase label order, active codebase, GitHub label
definitions, and every value that varies between pipeline runs.

## Quick Start

```bash
# 1. Edit config or a template, then regenerate:
docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py

# 2. Review diffs:
git diff .agentception/roles/

# 3. Commit generated files:
git add .agentception/roles/ scripts/gen_prompts/sync_labels.sh
git commit -m "chore: regenerate prompts"

# 4. Sync GitHub labels (only needed when labels section changed):
bash scripts/gen_prompts/sync_labels.sh
```

> **Why Docker?**
> The generator writes directly to `.agentception/roles/` inside the container, which is
> bind-mounted from the host (`docker-compose.override.yml`). This guarantees
> the same Python version (3.11), the same Jinja2 version, and the same file
> permissions as the running pipeline. Running on the host bypasses all of
> that and risks subtle divergence.

## Directory Layout

```
scripts/gen_prompts/
  config.yaml                      ← edit this to reconfigure a run
  generate.py                      ← run this to regenerate .agentception/roles/ files
  sync_labels.sh                   ← auto-generated; run once to sync GitHub labels
  COGNITIVE_ARCHITECTURE_SPEC.md   ← full spec for the cognitive architecture mixer
  README.md                        ← this file
  cognitive_archetypes/            ← YAML component library (4-layer inheritance)
    atoms/                         ← Layer 0: primitive cognitive dimensions
      epistemic_style.yaml         #   how knowledge is acquired/validated
      cognitive_rhythm.yaml        #   pacing and work structure
      creativity_level.yaml        #   conventional → inventive → disruptive
      quality_bar.yaml             #   pragmatic → craftsman → perfectionist
      error_posture.yaml           #   fail_loud / fail_safe / retry_first / escalate
      communication_style.yaml     #   terse / expository / socratic / visual
      scope_instinct.yaml          #   minimal / comprehensive / opportunistic / scoped
      uncertainty_handling.yaml    #   probabilistic / conservative / aggressive
      collaboration_posture.yaml   #   autonomous / consultative / delegating / pair
      mental_model.yaml            #   systems / objects / functions / flows
    skill_domains/                 ← Layer 1: technical expertise (orthogonal to personality)
      python.yaml                  #   FastAPI, Pydantic v2, async, mypy strict
      midi.yaml              #   MIDI pipeline, GM, music generation
      devops.yaml                  #   Docker Compose, containers, service reliability
      llm.yaml                     #   LLM APIs, RAG, embedding, OpenRouter
    archetypes/                    ← Layer 2: named bundles of atoms (inheritable)
      the_architect.yaml           #   deductive + deep_focus + systems + craftsman
      the_guardian.yaml            #   deductive + fail_loud + perfectionist + minimal
      the_pragmatist.yaml          #   abductive + iterative + pragmatic + scoped
      the_visionary.yaml           #   analogical + exploratory + inventive + comprehensive
      the_scholar.yaml             #   inductive + exploratory + perfectionist + functions
      the_hacker.yaml              #   empirical + burst + creative + expedient + flows
      the_mentor.yaml              #   empirical + pair + socratic + craftsman + opportunistic
      the_operator.yaml            #   empirical + deep_focus + retry_first + pragmatic
    figures/                       ← Layer 3: historical figures (extend archetypes)
      einstein.yaml                #   → the_visionary  (abductive, gedankenexperiment)
      turing.yaml                  #   → the_architect  (formal machines, computability)
      von_neumann.yaml             #   → the_scholar    (burst, comprehensive, cross-domain)
      dijkstra.yaml                #   → the_guardian   (terse, correctness-by-construction)
      feynman.yaml                 #   → the_mentor     (empirical, socratic, great explainer)
      hopper.yaml                  #   → the_hacker     (builds tools that build tools)
      shannon.yaml                 #   → the_architect  (information theory, flows, entropy)
      lovelace.yaml                #   → the_visionary  (sees the machine behind the machine)
      knuth.yaml                   #   → the_guardian   (programs as literature, loop invariants)
      hamming.yaml                 #   → the_pragmatist (work on the important problems)
      mccarthy.yaml                #   → the_architect  (formalize first, solve within formalism)
      ritchie.yaml                 #   → the_operator   (minimal tools that compose cleanly)
  templates/                       ← Jinja2 templates for all .agentception/roles/ prompt files
    roles/
      cto.md.j2
      engineering-coordinator.md.j2
      qa-coordinator.md.j2
      pr-reviewer.md.j2
      developer.md.j2
      coordinator.md.j2
      database-architect.md.j2
    PARALLEL_BUGS_TO_ISSUES.md.j2
    PARALLEL_CONDUCTOR.md.j2
    PARALLEL_ISSUE_TO_PR.md.j2
    PARALLEL_PR_REVIEW.md.j2
```

## Config Variables

| Key | Purpose |
|-----|---------|
| `repo.gh_slug` | GitHub `org/repo` slug — used in `gh` CLI calls everywhere |
| `repo.name` | Repo name — used in worktree subfolder names |
| `pipeline.claim_label` | Label agents add to claim an issue (`agent:wip`) |
| `pipeline.max_pool_size` | Max concurrent leaf agents per VP run |
| `pipeline.phases` | **Ordered** list of phase labels — CTO iterates this strictly |
| `codebases.active` | Which codebase is being worked on right now |
| `codebases.<name>.container` | Docker container that runs mypy/tests for that codebase |
| `codebases.<name>.mypy` | Full mypy command for that codebase |
| `codebases.<name>.test_dir` | Directory containing tests |
| `codebases.<name>.label_prefix` | Issue label prefix (e.g. `agentception/`) |
| `labels.*` | Full label definitions — drives `sync_labels.sh` generation |

## Template Syntax

Templates are standard Jinja2 with one customisation: comment delimiters are
`{## ... ##}` instead of the default `{# ... #}`. This avoids conflicts with
shell array-length syntax (`${#ARRAY[@]}`) used throughout the prompt files.

Shell variables (`$HOME`, `$REPO`, `$WTNAME`, `${BATCH_ID:-none}`) are left
as-is — Jinja2 never touches bare `$` variables.

| Template variable | Expands to |
|-------------------|-----------|
| `{{ gh_repo }}` | `cgcardona/agentception` |
| `{{ claim_label }}` | `agent:wip` |
| `{{ phases_shell }}` | Shell `for label in phase-0 phase-1 ...; do` block |
| `{{ active_label_prefix }}` | `agentception/` |
| `{{ active_mypy }}` | Full mypy command for the active codebase |
| `{{ active_test_dir }}` | Test directory for the active codebase |
| `{{ active_container }}` | Docker container for the active codebase |

## Switching Projects

To move the pipeline to a different project's work:

```yaml
# config.yaml
pipeline:
  phases:
    - "ac-ui/0-critical-bugs"
    - "ac-ui/1-design-tokens"
    # ...

codebases:
  active: "agentception_ui"   # ← only change needed here
```

Then run the generator and commit. All 12 prompt files update atomically.

## Label Sync

`generate.py` always regenerates `sync_labels.sh`. Run it after any label
change to push definitions to GitHub:

```bash
bash scripts/gen_prompts/sync_labels.sh
```

The script is idempotent: it creates labels that don't exist and updates color
and description for labels that do.

## Adding a New Phase

1. Add the phase name to `pipeline.phases` in `config.yaml` (in order).
2. Add a matching entry to `labels.phases` with color and description.
3. Run the generator: `docker compose exec agentception python3 /app/scripts/gen_prompts/generate.py`
4. Run `bash scripts/gen_prompts/sync_labels.sh` to create the GitHub label.
5. Create GitHub issues with the new label.

## Cognitive Architecture Mixer

See `COGNITIVE_ARCHITECTURE_SPEC.md` for the full design.

### The 4-Layer Inheritance Model

```
Layer 3: FIGURES        Einstein, Turing, von Neumann, Dijkstra, Feynman ...
              ↑ extends
Layer 2: ARCHETYPES     the_architect, the_scholar, the_visionary, the_guardian ...
              ↑ composed from
Layer 1: SKILL DOMAINS  python, midi, devops, llm ...
              ↑ orthogonal
Layer 0: ATOMS          epistemic_style, cognitive_rhythm, creativity_level ...
```

**Atoms** are primitive cognitive genes — each has a small set of named values
(e.g. `epistemic_style: deductive | inductive | abductive | analogical | empirical`).
Each value carries a `prompt_fragment` — actual text that gets injected into the agent.

**Archetypes** bundle atoms into named characters (`the_architect`, `the_guardian`, etc.).

**Figures** extend archetypes and override specific atoms, carrying a narrative
`prompt_injection` written in the figure's voice.

### Usage in `.agent-task`

The engineering coordinator writes `COGNITIVE_ARCH` to `.agent-task` at spawn time:

```bash
ISSUE=671
WORKTREE="$HOME/.agentception/worktrees/agentception/issue-671"
ROLE_FILE="$HOME/.agentception/roles/developer.md"
ISSUE_LABEL="agentception/2-telemetry"
SPAWN_MODE=direct
COGNITIVE_ARCH=dijkstra+python        # figure + skill domain
```

### Selection Examples

| Task signal | Suggested `COGNITIVE_ARCH` |
|-------------|---------------------------|
| Type errors / mypy failures | `dijkstra+python` |
| New test coverage | `feynman+python` |
| Phase 0 scaffold / foundation | `the_architect` |
| Performance problem | `von_neumann` or `knuth` |
| API design / interface | `the_architect` or `shannon` |
| Bug investigation | `the_guardian` |
| Refactor / cleanup | `hopper` |
| Documentation | `feynman` |
| Default / no signal | `the_pragmatist+python` |

### Blend Multiple Figures

```bash
COGNITIVE_ARCH=turing,feynman         # Turing's rigor + Feynman's pedagogy
COGNITIVE_ARCH=von_neumann,hopper     # von Neumann's breadth + Hopper's pragmatism
```

When blending, conflicting atoms are resolved left-to-right (first listed wins).

### Adding a New Figure

1. Create `cognitive_archetypes/figures/<id>.yaml` extending any archetype.
2. Specify only the atoms that differ from the archetype in `overrides:`.
3. Write the `prompt_injection.prefix` in the figure's authentic voice.
4. Reference the figure by ID in `COGNITIVE_ARCH` in any `.agent-task`.

No generator run needed — figures are read at agent spawn time by `resolve_arch.py`
(the companion resolver script, to be implemented as a follow-on).

## Dependencies

`jinja2` and `pyyaml` are already installed in the `agentception` container
(transitive deps of FastAPI). No new pip packages needed.
