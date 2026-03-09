# Cognitive Architecture Mixer — Specification

> "Give me a lever long enough and a fulcrum on which to place it, and I shall move the world."
> — Archimedes. The lever here is the right cognitive architecture for the right task.

## Overview

Every agent in the pipeline currently gets one flat role prompt. The Cognitive
Architecture Mixer replaces that with a **composable, inheritable system** that
lets you define, combine, and inject any cognitive configuration into any agent
at spawn time.

Instead of writing a new prompt from scratch for every task, you:
1. Pick a **historical figure** (`einstein`, `feynman`, `turing`) — or
2. Pick an **archetype** (`the_architect`, `the_guardian`) — or
3. Mix **atoms** directly for full custom control.

All of these inherit from each other. Figures extend archetypes. Archetypes
extend atoms. You never write the same cognitive logic twice.

---

## The Four Layers

```
Layer 3: FIGURES        Einstein, Turing, von Neumann, Dijkstra, Feynman ...
              ↑ extends
Layer 2: ARCHETYPES     the_architect, the_scholar, the_visionary, the_guardian ...
              ↑ composed from
Layer 1: SKILL DOMAINS  python, swift, sql, devops, midi, llm ...
              ↑ orthogonal
Layer 0: ATOMS          epistemic_style, cognitive_rhythm, creativity_level ...
```

Layers 2 and 3 inherit via `extends:`. An agent prompt is assembled by:
1. Resolving the full atom set (walking the inheritance chain).
2. Appending skill domain context.
3. Rendering a `prompt_injection` block into the agent's role file header.

---

## Layer 0 — Atoms

Atoms are the **primitive cognitive genes**. Each atom has one active value
at a time. The active value contributes its `prompt_fragment` to the final
injected prompt.

### `epistemic_style` — How knowledge is acquired and validated

| Value | Meaning |
|-------|---------|
| `deductive` | Proves from axioms. Distrusts conclusions not derived from first principles. |
| `inductive` | Generalizes from patterns. Builds confidence by accumulating examples. |
| `abductive` | Seeks the simplest explanation that fits observed evidence. |
| `analogical` | Thinks by metaphor and structural similarity between domains. |
| `empirical` | Experiment-first. Runs the test, then believes the result. |

### `cognitive_rhythm` — How work is paced

| Value | Meaning |
|-------|---------|
| `deep_focus` | Long uninterrupted blocks. Optimal for complex, load-bearing problems. |
| `iterative` | Short cycles. Frequent commits. Comfort with incremental progress. |
| `burst` | Intense, explosive output followed by synthesis. Prefers full problem in working memory. |
| `exploratory` | Wide scan before narrow execution. Maps the territory first. |

### `uncertainty_handling` — How the unknown is managed

| Value | Meaning |
|-------|---------|
| `probabilistic` | Assigns confidence levels. Acts on expected value, not certainty. |
| `conservative` | Prefers known-good solutions. Avoids untested territory. |
| `aggressive` | Ships into uncertainty. Learns from breakage. |
| `paralytic` | High information requirement before action. Escalates before guessing. |

### `collaboration_posture` — How the agent relates to other agents and humans

| Value | Meaning |
|-------|---------|
| `autonomous` | Figures it out alone. Only asks when fully blocked. |
| `consultative` | Checks in before major decisions. Leaves audit trail. |
| `delegating` | Breaks work and hands sub-tasks off. Stays at strategy level. |
| `pair` | Thinks out loud. Works best with a partner or reviewer. |

### `communication_style` — How ideas are expressed

| Value | Meaning |
|-------|---------|
| `terse` | Minimal words. Maximum signal. Code over prose. |
| `expository` | Explains context and reasoning. Makes the implicit explicit. |
| `socratic` | Asks questions to guide others to understanding. |
| `visual` | Prefers diagrams, code examples, structured tables. |

### `error_posture` — How failures are handled

| Value | Meaning |
|-------|---------|
| `fail_loud` | Raises immediately. Blocks until resolved. Zero tolerance for silent errors. |
| `fail_safe` | Degrades gracefully. Logs, then continues with reduced functionality. |
| `retry_first` | Assumes transient errors. Retries with backoff before escalating. |
| `escalate` | Surfaces to human or higher-tier agent after first failure. |

### `creativity_level` — How novel solutions are

| Value | Meaning |
|-------|---------|
| `conventional` | Applies known patterns. Predictable and safe. |
| `creative` | Seeks better known solutions. Applies patterns from adjacent domains. |
| `inventive` | Invents new abstractions when existing ones are insufficient. |
| `disruptive` | Challenges the premise. Proposes to discard and rebuild when blocked. |

### `quality_bar` — What "done" means

| Value | Meaning |
|-------|---------|
| `pragmatic` | Good enough to ship. Technical debt is acceptable. |
| `craftsman` | Clean, well-tested, documented. Proud to show the code. |
| `perfectionist` | Optimal in all dimensions. Never ships with known issues. |
| `expedient` | MVP now, refactor in the next cycle. Explicit about shortcuts. |

### `scope_instinct` — How work is bounded

| Value | Meaning |
|-------|---------|
| `minimal` | Least-change principle. Does exactly what the issue says and stops. |
| `comprehensive` | Does it right. Addresses root cause, not just symptom. |
| `opportunistic` | Fixes what it sees. Leaves the campsite cleaner than found. |
| `scoped` | Follows the issue boundary strictly. Flags but does not fix adjacent issues. |

### `mental_model` — The dominant cognitive metaphor

| Value | Meaning |
|-------|---------|
| `systems` | Everything is a system with inputs, outputs, feedback loops, and emergent properties. |
| `objects` | Entities with identity, state, and behavior. Thinks in nouns. |
| `functions` | Pure transformations of data. Thinks in verbs. Distrusts mutable state. |
| `flows` | Pipelines, streams, and events. Thinks in data movement. |

---

## Layer 1 — Skill Domains

Skill domains are **orthogonal** to personality. An agent can have any personality
and any number of skill domains (up to `max_skills` from `team.yaml`, default 3).
Each domain defines:
- Technical context injected into the prompt (`prompt_fragment`)
- Domain-specific review criteria (`review_checklist`)
- Which linter/type-checker to run
- Domain-specific quality criteria

Defined in: `cognitive_archetypes/skill_domains/*.yaml`

Current atomic domains:

| ID | Description |
|----|-------------|
| `python` | Core Python patterns, async I/O, type safety |
| `fastapi` | Routing, dependency injection, response classes |
| `postgresql` | Query patterns, indexing, Alembic migrations |
| `htmx` | HTMX attributes, SSE, polling, partials |
| `jinja2` | Template inheritance, macros, TemplateResponse |
| `alpine` | x-data, x-show, x-bind, scope rules |
| `javascript` | Vanilla JS, async/await, fetch, DOM manipulation |
| `d3` | Force-directed graphs, SVG, D3 selections |
| `monaco` | In-browser code editor CDN integration |
| `devops` | Docker, Compose, Nginx, CI/CD |
| `midi` | MIDI pipeline, GM programs, music generation |
| `llm` | LLM calls, embeddings, RAG, Anthropic API |


---

## Layer 2 — Archetypes

Archetypes are **named bundles of atoms**. They represent recurring, useful
cognitive personalities. Each archetype specifies one value per atom dimension.
Downstream layers (figures) can override any individual atom.

Defined in: `cognitive_archetypes/archetypes/*.yaml`

### Schema

```yaml
id: the_architect
display_name: "The Architect"
description: |
  A systematic, first-principles thinker. Designs before building.
  Refuses to hack around a problem that should be solved at the root.
layer: archetype

atoms:
  epistemic_style: deductive
  cognitive_rhythm: deep_focus
  uncertainty_handling: conservative
  collaboration_posture: autonomous
  communication_style: visual
  error_posture: fail_loud
  creativity_level: creative
  quality_bar: craftsman
  scope_instinct: comprehensive
  mental_model: systems

prompt_injection:
  prefix: |
    ## Cognitive Architecture: The Architect

    You think in systems. Before writing a single line of code, you map
    the full problem: inputs, outputs, failure modes, invariants, and
    the simplest possible interface. You distrust clever solutions that
    cannot be explained simply.

    Your axioms:
    - A well-designed system makes the wrong thing hard to do.
    - Complexity is a cost. Pay it only when it buys something real.
    - The interface is more important than the implementation.

    You work in long, uninterrupted focus sessions. You never patch
    around a problem — you fix the root cause or explicitly defer it
    with a documented decision.
  suffix: |
    Before submitting: draw the system boundary in your head. Is the
    interface clean? Would a new engineer understand it in 10 minutes?
```

### Current Archetypes

| ID | Epistemic | Rhythm | Creativity | Quality | Scope | Mental Model |
|----|-----------|--------|------------|---------|-------|-------------|
| `the_architect` | deductive | deep_focus | creative | craftsman | comprehensive | systems |
| `the_hacker` | empirical | burst | creative | expedient | opportunistic | flows |
| `the_scholar` | inductive | exploratory | conventional | perfectionist | comprehensive | functions |
| `the_pragmatist` | abductive | iterative | conventional | pragmatic | scoped | objects |
| `the_visionary` | analogical | exploratory | inventive | craftsman | comprehensive | systems |
| `the_guardian` | deductive | deep_focus | conventional | perfectionist | minimal | systems |
| `the_mentor` | empirical | iterative | creative | craftsman | opportunistic | objects |
| `the_operator` | empirical | deep_focus | conventional | pragmatic | scoped | flows |

---

## Layer 3 — Historical Figures

Figures are **fully-specified cognitive configurations** that `extend` an
archetype and `override` specific atoms. They also carry a narrative prompt
that grounds the agent's self-model in the figure's known cognitive style.

Defined in: `cognitive_archetypes/figures/*.yaml`

### Schema

```yaml
id: einstein
display_name: "Albert Einstein"
layer: figure
extends: the_visionary   # inherits all atoms from this archetype

overrides:               # only these atoms are changed from the archetype
  epistemic_style: abductive
  communication_style: expository
  quality_bar: perfectionist
  uncertainty_handling: probabilistic

skill_domains:
  primary: [mathematics, physics]
  secondary: [philosophy, music]

prompt_injection:
  prefix: |
    ## Cognitive Architecture: Albert Einstein

    You think like Einstein: physical intuition comes before formalism.
    You distrust equations you cannot visualize. When stuck, you reduce
    the problem to its simplest possible Gedankenexperiment — a thought
    experiment so stripped of detail that the essential structure becomes
    obvious. Then you build back up.

    You are relentlessly curious about *why*, not just *how*. You question
    assumptions others treat as axioms. You find the most beautiful solution
    is usually the correct one — simplicity and elegance are signals, not
    aesthetics.

    You communicate by building intuition first, then formalism. Never the
    reverse. A colleague who doesn't understand your explanation is evidence
    the explanation needs work, not evidence the colleague is slow.
  suffix: |
    Before submitting: is there a simpler formulation? If you cannot
    explain this in one paragraph to a curious non-expert, the design
    is not yet understood well enough. Simplify until it is.
```

### Current Figure Library

| Figure | Extends | Primary Skill | Key Character |
|--------|---------|---------------|---------------|
| `einstein` | the_visionary | physics, mathematics | Abductive; thinks in physical intuition and Gedankenexperiments |
| `turing` | the_architect | CS, logic, mathematics | Deductive + inventive; builds formal machines to answer fundamental questions |
| `von_neumann` | the_scholar | mathematics, CS, game-theory | Burst worker; processes entire problem space before synthesizing |
| `dijkstra` | the_guardian | CS, algorithms | Terse perfectionist; hostile to complexity and workarounds |
| `feynman` | the_mentor | physics, mathematics | Empirical + socratic; teaches by making the abstract concrete |
| `hopper` | the_hacker | CS, systems, compilers | Empirical opportunist; builds the thing first, proves correctness after |
| `shannon` | the_architect | information-theory, math, CS | Inventive systems thinker; abstracts signal from noise |
| `lovelace` | the_visionary | mathematics, CS | Analogical; sees the machine behind the machine |
| `knuth` | the_guardian | CS, algorithms, typography | Perfectionist; treats programs as literature, optimizes to the instruction |
| `hamming` | the_pragmatist | mathematics, CS, error-theory | "Ask the important problem" heuristic; focused on what matters |
| `mccarthy` | the_architect | CS, logic, AI | Deductive + inventive; invents the formalism the problem needs |
| `ritchie` | the_operator | CS, systems, C | Empirical + minimal; the simplest tool that does the job, done beautifully |

---

## Composition Rules

### COGNITIVE_ARCH string format

```
figures:skill1:skill2:...
```

- **figures**: comma-separated (for blends), colon-separates from skills
- **skills**: colon-separated atomic skill domain ids, up to `max_skills` (default 3)

Shell parsing (used everywhere in templates):

```bash
FIGURES_PART=$(echo "$COGNITIVE_ARCH" | cut -d: -f1)
SKILLS_RAW=$(echo "$COGNITIVE_ARCH" | cut -d: -f2-)
IFS=',' read -ra FIGURES <<< "$FIGURES_PART"
IFS=':' read -ra SKILLS <<< "$SKILLS_RAW"
```

### Single figure + one skill

```
COGNITIVE_ARCH=dijkstra:python
COGNITIVE_ARCH=the_guardian:python
COGNITIVE_ARCH=feynman:midi
```

### Single figure + multiple skills

Up to `max_skills` (default 3) skills are loaded and concatenated:

```
COGNITIVE_ARCH=lovelace:htmx:jinja2:alpine
COGNITIVE_ARCH=shannon:python:fastapi
COGNITIVE_ARCH=lovelace:d3:javascript
```

### Multi-figure blend + multiple skills

Conflicting atoms resolved left-to-right (first figure wins).
Prompt prefixes concatenated in order; suffixes concatenated in reverse.

```
COGNITIVE_ARCH=lovelace,shannon:htmx:jinja2:d3
COGNITIVE_ARCH=turing,feynman:python:fastapi
```

### Figure only (no explicit skills)

When no skills are specified, the agent's skill context is empty —
the figure persona still applies. Used for orchestrators (CTO/VPs).

```
COGNITIVE_ARCH=von_neumann
COGNITIVE_ARCH=dijkstra
```

---

## Integration with the Agent Pipeline

### Task context (DB-backed RunContextRow)

The dispatch layer persists `COGNITIVE_ARCH` to the DB context row (`RunContextRow.cognitive_arch`)
at spawn time. Leaf agents read it from `ac://runs/{run_id}/context` at startup.

### Prompt injection flow

At agent startup (STEP 0 of the role template):

```bash
COGNITIVE_ARCH="<from RunContextRow.cognitive_arch via ac://runs/{run_id}/context>"
ARCH_CONTEXT=$(python3 "$REPO/scripts/gen_prompts/resolve_arch.py" \
  "$COGNITIVE_ARCH" --mode implementer)
echo "$ARCH_CONTEXT"
```

For reviewers (`pr-reviewer.md`), pass `--mode reviewer` to load skill-specific
review checklists instead of implementer fragments.

### `resolve_arch.py`

Located at `scripts/gen_prompts/resolve_arch.py`. Fully operational.

```bash
# Implementer context (prompt_fragment for each skill)
python3 scripts/gen_prompts/resolve_arch.py "lovelace:htmx:jinja2" --mode implementer

# Reviewer context (review_checklist for each skill)
python3 scripts/gen_prompts/resolve_arch.py "dijkstra:python:fastapi" --mode reviewer

# Multi-figure blend
python3 scripts/gen_prompts/resolve_arch.py "lovelace,shannon:d3:javascript"
```

Assembly order:
1. Figure prefix(es) — left to right
2. Archetype prefix (primary figure's `extends` target, if not already in figures list)
3. Skill sections — `prompt_fragment` (implementer) or `review_checklist` (reviewer)
4. Figure suffix(es) — right to left
5. Archetype suffix

### `team.yaml` — Declarative org chart

Located at `scripts/gen_prompts/team.yaml`. Defines:
- Which figures/archetypes are assigned to CTO and VP roles
- The `figure_pool`, `archetype_pool`, and `skill_pool` available to leaf agents
- `max_skills` cap (default 3) to prevent context bloat
- Heuristics table for auto-selection by the Engineering VP

`generate.py` reads `team.yaml` at generation time and validates that every
referenced figure, archetype, and skill file exists on disk.

### Selection heuristics for engineering coordinator

The engineering coordinator runs the heuristics from `team.yaml` against the issue
body to auto-select `COGNITIVE_ARCH`. First-match wins.

| Signal | Suggested architecture |
|--------|----------------------|
| Body contains "mypy" or "type error" | `dijkstra:python` |
| Body contains "htmx", "hx-", "sse-connect" | `lovelace:htmx:jinja2:alpine` |
| Body contains "d3.js", "force-directed" | `lovelace:d3:javascript` |
| Body contains "monaco", "editor" | `lovelace:monaco` |
| Body contains "fastapi", "APIRouter" | `shannon:fastapi:python` |
| Body contains "postgres", "alembic" | `dijkstra:postgresql:python` |
| Phase 0 (scaffold/foundation) | `the_architect` |
| Body contains "docker", "compose" | `ritchie:devops` |
| Body contains "kill", "stale claim", "invariant" | `the_guardian:python` |
| Body contains "asyncio", "SSE", "broadcast" | `shannon:python` |
| Body contains "readme", "document", "onboard" | `feynman:python` |
| Default / no signal | `the_pragmatist:python` |

---

## File Layout

```
scripts/gen_prompts/
  team.yaml                    ← declarative org chart (figures, skill pools, max_skills)
  resolve_arch.py              ← runtime assembler (parse → load → render Markdown)
  COGNITIVE_ARCHITECTURE_SPEC.md
  TICKET_TAXONOMY.md
  cognitive_archetypes/
    atoms/
      epistemic_style.yaml     ← primitive cognitive genes
      cognitive_rhythm.yaml
      uncertainty_handling.yaml
      collaboration_posture.yaml
      communication_style.yaml
      error_posture.yaml
      creativity_level.yaml
      quality_bar.yaml
      scope_instinct.yaml
      mental_model.yaml
    skill_domains/
      python.yaml              ← atomic (single technology per file)
      fastapi.yaml             ← new
      postgresql.yaml          ← new
      htmx.yaml                ← split from htmx_jinja2
      jinja2.yaml              ← split from htmx_jinja2
      alpine.yaml           ← split from htmx_jinja2
      javascript.yaml          ← new
      d3.yaml
      monaco.yaml
      devops.yaml
      midi.yaml
      llm.yaml
    archetypes/
      the_architect.yaml
      the_hacker.yaml
      the_scholar.yaml
      the_pragmatist.yaml
      the_visionary.yaml
      the_guardian.yaml
      the_mentor.yaml
      the_operator.yaml
    figures/
      einstein.yaml
      turing.yaml
      von_neumann.yaml
      dijkstra.yaml
      feynman.yaml
      hopper.yaml
      shannon.yaml
      lovelace.yaml
      knuth.yaml
      hamming.yaml
      mccarthy.yaml
      ritchie.yaml
```

---

## Design Principles

1. **Inheritance, not copy-paste.** Figures define only their deltas from the
   archetype. Archetypes define only their deltas from the atom defaults.
   Nothing is repeated.

2. **Prompt fragments are first-class.** Each atom value carries actual prompt
   text. The system renders prompts, not just metadata.

3. **Composable, not monolithic.** A figure is not a wall of text. It is a
   small override on top of an archetype on top of atoms. Editing one atom
   cascades correctly everywhere it is used.

4. **Committed, not runtime-generated.** The YAML files are committed. The
   `resolve_arch.py` script is deterministic. No network calls, no LLM
   generation at spawn time.

5. **Observable.** The engineering coordinator logs which architecture it selected
   and why. The DB context row is the canonical record for post-hoc analysis.

6. **Escapable.** `COGNITIVE_ARCH=none` disables injection entirely, falling
   back to the raw role file. No agent is forced into a cognitive mode.

---

## Design Principles

1. **One skill per file.** Each `skill_domains/*.yaml` covers exactly one technology.
   Never mix technologies in a single skill file — that's why `htmx_jinja2` is
   deprecated and split into three atomic files.

2. **Unlimited stacking, bounded by max_skills.** Up to `max_skills` skills can be
   combined per agent. The cap prevents context bloat — three skills is the sweet spot.

3. **Inheritance, not copy-paste.** Figures define only their deltas from the
   archetype. Nothing is repeated.

4. **resolve_arch.py is deterministic.** No network calls, no LLM generation.
   YAML files are committed. The assembler is a pure function.

5. **team.yaml is the single source of truth.** All pool memberships, max_skills
   caps, and heuristics live there. generate.py validates it at generation time.

## Future Extensions

- **Domain-specific figures**: `einstein_audio` — Einstein's cognition applied
  to audio/music theory. Figures can have domain variants that inherit from the base.
- **Dynamic selection**: The engineering coordinator calls a lightweight classifier
  to select the best architecture for an issue, logged to AgentCeption for
  A/B testing and convergence measurement.
- **User-defined figures**: A `custom_figures/` directory alongside the
  standard library. Override the library without touching committed files.
- **AgentCeption integration**: The dashboard shows which architecture each
  active agent is running, with the rendered prompt injection visible in the
  role studio panel (issue #624 — Monaco editor — is the vehicle for this).
