# Cognitive Architecture Reference

Every agent dispatched by AgentCeption has a **cognitive architecture** — a composed identity injected into its system prompt that shapes how it reasons, prioritizes, and produces output. You are deploying reasoners with specific cognitive profiles, not generic LLM calls.

---

## The Four Layers

```
Layer 3: FIGURES        Turing, von Neumann, Dijkstra, Feynman, Hopper, ...  (77 figures)
              ↑ extends
Layer 2: ARCHETYPES     the_architect, the_scholar, the_visionary, ...       (8 archetypes)
              ↑ composed from
Layer 1: SKILL DOMAINS  python, fastapi, htmx, postgresql, devops, llm, ... (43 domains)
              ↑ orthogonal
Layer 0: ATOMS          epistemic_style, cognitive_rhythm, creativity_level, ...
```

**Figures extend archetypes.** Archetypes are composed from atoms. Skill domains are orthogonal (they add technical expertise, not personality). The `resolve_arch.py` engine walks the inheritance chain and assembles a single coherent prompt injection.

---

## Layer 0 — Atoms

Atoms are primitive cognitive genes. Each has one active value at a time. Active values contribute `prompt_fragment` text to the final prompt.

**Source:** `scripts/gen_prompts/cognitive_archetypes/atoms/`

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
| `burst` | Intense, explosive output then synthesis. Prefers full problem in working memory. |
| `exploratory` | Wide scan before narrow execution. Maps the territory first. |

### `uncertainty_handling` — How the unknown is managed

| Value | Meaning |
|-------|---------|
| `probabilistic` | Assigns confidence levels. Acts on expected value, not certainty. |
| `conservative` | Prefers known-good solutions. Avoids untested territory. |
| `aggressive` | Ships into uncertainty. Learns from breakage. |
| `paralytic` | High information requirement before action. Escalates before guessing. |

### `collaboration_posture` — How the agent relates to others

| Value | Meaning |
|-------|---------|
| `autonomous` | Figures it out alone. Only asks when fully blocked. |
| `consultative` | Polls others before committing to direction. |
| `directive` | Tells others what to do. Comfortable with authority. |
| `collaborative` | Works alongside. High trust in teammates' judgment. |

### `creativity_level` — How novel solutions are generated

| Value | Meaning |
|-------|---------|
| `conservative` | Prefers established patterns. "Don't fix what isn't broken." |
| `incremental` | Small improvements to existing approaches. |
| `inventive` | Generates genuinely new approaches. High tolerance for uncertainty. |
| `radical` | Questions the problem itself. Invents new paradigms. |

### `quality_bar` — What "done" means

| Value | Meaning |
|-------|---------|
| `mvp` | Works. Ships fast. Polish later. |
| `pragmatic` | Correct and maintainable. Practical tradeoffs. |
| `craftsperson` | Excellence in implementation, not just correctness. |
| `perfectionist` | Will not ship until it is right. High cost, high output quality. |

### `scope_instinct` — How the agent handles scope

| Value | Meaning |
|-------|---------|
| `minimal` | Does exactly what was asked. No scope creep. |
| `expansive` | Sees the broader context and acts on it. |
| `focused` | Narrow execution. Ignores peripheral concerns. |

---

## Layer 1 — Skill Domains

Skill domains add technical expertise to any figure or archetype. They are orthogonal — you can combine any skill with any figure.

**Source:** `scripts/gen_prompts/cognitive_archetypes/skill_domains/`

**Available skill domain IDs (43 total):**

```
alpine        aws           blockchain    cpp           cryptography
csharp        d3            devops        django        docker
elasticsearch fastapi       go            graphql       htmx
java          javascript    jinja2        kafka         kotlin
kubernetes    llm           llm_engineering  midi       monaco
nextjs        nodejs        postgresql    python        pytorch
rag           rails         react         redis         ruby
rust          security      sql           swift         swift_ui
terraform     testing       typescript
```

---

## Layer 2 — Archetypes

Archetypes are abstract thinking styles. They define a coherent atom configuration without being tied to any historical figure.

**Source:** `scripts/gen_prompts/cognitive_archetypes/archetypes/`

| Archetype ID | Character |
|-------------|-----------|
| `the_architect` | Systems thinker. Minimal, composable designs. Resists coupling. |
| `the_scholar` | Deep research orientation. Documents everything. Cites sources. |
| `the_visionary` | Forward-looking. Comfortable with ambiguity. Shapes culture. |
| `the_guardian` | Security and correctness first. Never ships what it can't defend. |
| `the_hacker` | Speed and ingenuity. Finds unexpected solutions. Ships fast. |
| `the_mentor` | Communicative. Explains reasoning. Thinks about the next person. |
| `the_operator` | Process and reliability. Operational excellence. Low drama. |
| `the_pragmatist` | Gets it done. Practical tradeoffs. No perfectionism paralysis. |

---

## Layer 3 — Figures

Historical thinkers and builders. Each figure `extends` an archetype and overrides specific atoms with their unique cognitive profile.

**Source:** `scripts/gen_prompts/cognitive_archetypes/figures/`

**Available figures (77 total):**

```
anders_hejlsberg   andrej_karpathy    andy_grove         avie_tevanian
barbara_liskov     bill_gates         bjarne_stroustrup  brendan_eich
bruce_schneier     carl_sagan         da_vinci           darwin
david_chaum        demis_hassabis     dhh                dijkstra
don_norman         einstein           elon_musk          emin_gun_sirer
fabrice_bellard    fei_fei_li         feynman            gabriel_cardona
gavin_wood         geoffrey_hinton    graydon_hoare      guido_van_rossum
hal_finney         hamming            hopper             ilya_sutskever
james_gosling      jeff_bezos         jeff_dean          joe_armstrong
john_carmack       ken_thompson       kent_beck          knuth
leslie_lamport     linus_pauling      linus_torvalds     lovelace
margaret_hamilton  marie_curie        martin_fowler      matz
mccarthy           michael_fagan      nassim_taleb       newton
nick_szabo         nikola_tesla       patrick_collison   paul_graham
peter_drucker      rich_hickey        ritchie            rob_pike
ryan_dahl          sam_altman         satoshi_nakamoto   satya_nadella
scott_forstall     shannon            steve_jobs         sun_tzu
tim_berners_lee    turing             vint_cerf          vitalik_buterin
von_neumann        w_edwards_deming   werner_vogels      wozniak
yann_lecun
```

### Selected figure profiles

| Figure | Extends | Key traits | Best for |
|--------|---------|-----------|---------|
| `turing` | `the_architect` | Deductive, deep_focus, perfectionist, minimal | Core algorithm and type system work |
| `von_neumann` | `the_architect` | Burst cognitive rhythm, systems mental model | CTO / orchestration roles |
| `dijkstra` | `the_scholar` | Correctness-obsessed, epistemic precision | Code review, formal verification |
| `hopper` | `the_pragmatist` | Empirical, collaborative, mvp quality bar | Shipping under constraints |
| `feynman` | `the_scholar` | Analogical, inventive, deep explanations | Documentation, teaching roles |
| `knuth` | `the_scholar` | Perfectionist, deep_focus, craftsperson | Algorithm implementation |
| `steve_jobs` | `the_visionary` | Taste-driven, radical creativity | Product strategy, CEO roles |
| `don_norman` | `the_architect` | User-centered, design thinking | UX/UI design roles |
| `bruce_schneier` | `the_guardian` | Threat-model first, conservative | Security roles, CISO |
| `linus_torvalds` | `the_hacker` | Decisive, high quality bar, direct communication | Systems programming, kernel-style work |
| `martin_fowler` | `the_mentor` | Refactoring-oriented, patterns, communicative | Architecture review |
| `kent_beck` | `the_pragmatist` | TDD, iterative, collaborative | Engineering coordination |

---

## Blending figures

You can blend two figures to get a composite cognitive profile:

```
"lovelace,shannon:htmx:d3:python"
 ^      ^         ^
 │      │         └─ skill domains (colon-separated)
 │      └─ second figure
 └─ first figure
```

The resolver walks both figures' inheritance chains, merges their atom overrides (last figure wins on conflicts), and injects a blended prompt.

**Use blends when:**
- The task spans two cognitive domains (e.g. systems + UX → `turing,don_norman`)
- You want a figure's domain expertise but a different archetype's posture
- A single figure's atom profile is too extreme for the task

---

## The `resolve_arch.py` engine

```bash
# Resolve and print the full prompt injection for a figure + skills
docker compose exec agentception python3 scripts/gen_prompts/resolve_arch.py turing:python:fastapi

# Print the fingerprint table (atoms + their active values)
docker compose exec agentception python3 scripts/gen_prompts/resolve_arch.py --fingerprint turing:python
```

The engine:
1. Looks up the figure YAML in `cognitive_archetypes/figures/`
2. Resolves the `extends` chain up to the archetype
3. Builds the full atom set (archetype defaults → figure overrides → `atom_overrides` in `team.yaml`)
4. Looks up each skill domain in `cognitive_archetypes/skill_domains/`
5. Renders a `prompt_injection` Markdown block

This block is injected at the top of every generated role file.

---

## Adding a new figure

1. Create `scripts/gen_prompts/cognitive_archetypes/figures/{your_figure}.yaml`:

```yaml
id: your_figure
display_name: "Your Figure"
layer: figure
extends: the_architect    # or any other archetype

description: |
  Who this person was and what they contributed. Be specific about
  their actual body of work — the LLM uses this to reason in their style.

overrides:
  epistemic_style: deductive       # Override specific atoms
  cognitive_rhythm: deep_focus
  quality_bar: perfectionist
  creativity_level: inventive
```

2. Add the figure to the `compatible_figures` list for any role in `role-taxonomy.yaml`.

3. Optionally use it in `team.yaml` for a specific role:

```yaml
python_developer:
  figures: [your_figure]
  skills: [python, fastapi]
  cognitive_arch: "your_figure:python:fastapi"
```

4. Re-run `generate.py` to regenerate all role files.

---

## Adding a new skill domain

1. Create `scripts/gen_prompts/cognitive_archetypes/skill_domains/{skill_id}.yaml`:

```yaml
id: your_skill
display_name: "Your Technology"
category: language  # or: framework, platform, discipline, protocol

description: |
  What this skill domain covers. Be specific about the ecosystem,
  common patterns, and what expertise in this domain looks like.

key_concepts:
  - concept_one
  - concept_two

prompt_fragment: |
  You are an expert in {skill}. You understand {key_concepts}.
  When working in this domain, you {key_behaviors}.
```

2. Use the new skill ID in `team.yaml` for any role that should have it.

---

## Adding a new archetype

1. Create `scripts/gen_prompts/cognitive_archetypes/archetypes/{archetype_id}.yaml`:

```yaml
id: your_archetype
display_name: "The Name"
layer: archetype

description: |
  What thinking style this archetype represents.

defaults:
  epistemic_style: empirical
  cognitive_rhythm: iterative
  uncertainty_handling: probabilistic
  collaboration_posture: collaborative
  creativity_level: incremental
  quality_bar: pragmatic
  scope_instinct: focused

prompt_injection: |
  Core behavioral description injected into the agent's system prompt.
```

2. Reference it in any figure YAML's `extends:` field.

---

## Viewing cognitive architectures in the UI

- **`/cognitive-arch`** — Catalog of all available architectures (figures + archetypes)
- **`/cognitive-arch/{arch_id}`** — Detail view for a specific architecture: full atom fingerprint, prompt injection, skill domains, and which roles use it
- **`/agents/{id}`** — An individual agent's assigned cognitive architecture

---

## Cognitive arch in the planning pipeline (Phase 1A/1B)

Cognitive architecture assignments are determined at **plan time**, not at dispatch time. The LLM planner assigns an arch to every issue and to each orchestration tier during Phase 1A. The user can edit any assignment in the Phase 1B YAML editor before filing.

### Resolution priority at dispatch time

When an agent is spawned, `_resolve_cognitive_arch()` in `agentception/services/cognitive_arch.py` applies the following priority:

1. **`<!-- ac:cognitive_arch: figure:skills -->`** — HTML comment embedded in the GitHub issue body at issue-creation time (highest priority). Set by the LLM planner; used verbatim with no heuristics.
2. **`skills_hint`** — Explicit skill list passed by the caller from `PlanIssue.skills`. Combined with `ROLE_DEFAULT_FIGURE` lookup.
3. **`<!-- ac:skills: ... -->`** — Skills comment embedded in the issue body.
4. **Keyword scan** — Last-resort fallback for issues created before Phase 1A arch assignment was introduced.

### PlanSpec fields

```yaml
initiative: my-feature

# Orchestration tier assignments — populated by the LLM planner, editable in Phase 1B.
coordinator_arch:
  cto: jeff_dean:llm:python
  engineering-coordinator: hamming:fastapi:python
  qa-coordinator: w_edwards_deming:testing

phases:
  - label: 0-foundation
    description: Scaffold DB and core models
    issues:
      - id: my-feature-p0-001
        title: Add SQLAlchemy models
        skills: [postgresql, python]
        cognitive_arch: leslie_lamport:postgresql:python   # per-issue, baked into the ticket
        body: |
          ...
```

- `coordinator_arch` — maps role slugs to arch strings for orchestration agents (CTO, engineering-coordinator, qa-coordinator, and any future C-level or coordinator variant). Keys are open-ended; new coordinator types require no schema changes.
- `cognitive_arch` (per issue) — the fully-resolved arch string baked into the issue body at filing time. Leaf engineers read this from the issue and load the matching persona.

### MCP resource: `ac://plan/figures/{role}`

Agents and the Phase 1A LLM planner can read this resource to get the filtered figure catalog for any role:

```
FetchMcpResource(server="user-agentception", uri="ac://plan/figures/python-developer")
→ {role: str, figures: [{id, display_name, description}, ...]}
```

The catalog is filtered to `compatible_figures` from `role-taxonomy.yaml`, so the caller only sees figures that are semantically appropriate for that role. Works for any role slug — `"cto"`, `"qa-coordinator"`, `"python-developer"`, etc.
