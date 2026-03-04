# Cognitive Architecture Enrichment — Agent Task Spec

## Mission

Enrich assigned cognitive architecture figure YAML files to match the quality bar set by
`scripts/gen_prompts/cognitive_archetypes/figures/gabriel_cardona.yaml` — the canonical reference.

You are working inside the Maestro repository (`$REPO`). All YAML files live under:
```
scripts/gen_prompts/cognitive_archetypes/figures/
```

---

## Quality Bar — the gabriel_cardona.yaml standard

Every figure must have:

### 1. `heuristic` field (top-level, new)
One governing sentence — the figure's single sharpest distillation of how they operate.
It should be *concrete and specific to that person*, not a generic wisdom quote.
Place this as a new top-level key after `skill_domains`.

```yaml
heuristic: "Define the architecture so clearly that the correct implementation becomes obvious."
```

Good heuristics are:
- Behaviorally specific: what does this person actually *do*, not what they *believe*?
- Grounded in that figure's documented track record or writing
- 12–30 words — no longer

### 2. `failure_modes` field (top-level, new)
A YAML list of 2–4 specific failure modes this figure is known for — real blindspots, not
generic weaknesses. Each item should name the failure AND give the active compensation.

```yaml
failure_modes:
  - "Over-architects instead of shipping; compensate by setting a spike timebox before
     any design session and shipping the smallest coherent slice."
  - "Dismisses social/human factors in favor of technical purity; compensate by explicitly
     asking 'who is the user and what is their mental model?' before finalizing any API."
  - "Scope creep under the banner of correctness; compensate by writing the exit criterion
     before starting, not after."
```

Good failure modes are:
- Specific to this person's documented tendencies (real patterns, not invented)
- Paired with a concrete active compensation — not just a warning
- Concise: one sentence each

### 3. `prompt_injection.prefix` — depth and specificity
The prefix must be **specific to this figure's real career, decisions, and documented thinking**.
It should NOT be generic archetype prose. Minimum 5 substantial paragraphs.

Each paragraph should answer one of these framings:
- What is this person's default cognitive posture when encountering a new problem?
- What concrete career event/project/writing exemplifies their approach?
- What do they optimize for that most people ignore?
- What are they explicitly *not* — what would they refuse to do or think?
- How does their approach manifest at code/design review level?

Avoid clichés and slogans. Write as if briefing a very senior engineer on how to
actually *think like* this person, not just quote them.

### 4. `prompt_injection.suffix` — 5–8 specific behavioral checkpoints
The suffix is a before-submission checklist. Each question must be:
- Specific to this figure's values (not generic engineering quality)
- Behavioral (what did I *do* or *not do*?), not attitudinal
- Phrased in the first person

Bad (generic): "Is this code well-tested?"
Good (Jobs-specific): "Have I said no to at least one thing this session to protect the
one thing that matters? Is there anything I can remove that makes the result strictly better?"

---

## YAML Schema (complete example structure)

```yaml
# Historical Figure: Name
id: figure_id_snake_case
display_name: "Display Name"
layer: figure
extends: archetype_id          # e.g. the_visionary, the_architect, the_hacker
description: |
  2–5 sentence bio grounding the figure in their actual career and documented thinking.
  Focus on what makes their cognitive style distinctive and traceable.

overrides:
  epistemic_style: value        # See atoms/epistemic_style.yaml for valid values
  creativity_level: value
  quality_bar: value
  scope_instinct: value
  collaboration_posture: value
  communication_style: value
  cognitive_rhythm: value       # optional
  error_posture: value          # optional
  uncertainty_handling: value   # optional
  mental_model: value           # optional

skill_domains:
  primary: [domain1, domain2]
  secondary: [domain3, domain4]

heuristic: "One governing sentence, behaviorally specific."

failure_modes:
  - "Mode 1; compensate by doing X."
  - "Mode 2; compensate by doing Y."
  - "Mode 3; compensate by doing Z."

prompt_injection:
  prefix: |
    ## Cognitive Architecture: Display Name

    [5+ specific, concrete paragraphs as described above]

  suffix: |
    Before submitting:
    - [5–8 specific behavioral checkpoints]
```

---

## Atom Values Reference

Valid values for each `overrides` dimension are in:
`scripts/gen_prompts/cognitive_archetypes/atoms/<dimension>.yaml`

When in doubt, inspect the atom files directly. Do not invent values.

---

## Process

1. `cd $REPO`
2. For each figure in your assigned batch:
   a. Read the existing YAML.
   b. Research the figure's documented thinking, career, writing, interviews.
      (You have extensive training data — use it. Do not hallucinate or invent facts.)
   c. Write or extend `heuristic`, `failure_modes`, `prefix`, and `suffix` to the spec.
   d. Verify atom `overrides` values against the atom YAML files.
   e. Write the improved YAML back.
3. After finishing all assigned figures, run:
   ```bash
   python scripts/gen_prompts/resolve_arch.py --figure <id> --mode implementer
   ```
   for one figure per batch to verify the assembly pipeline works (output to stdout is fine).
4. `git add` your changed files, then `git commit -m "feat(cognitive-arch): enrich <batch_name> figures"`.
   Do NOT push — commit only to your worktree branch.

---

## Non-negotiable constraints

- Do NOT invent biographical facts. Only assert what you are confident is documented.
- Do NOT change `id`, `layer`, `extends` (unless clearly wrong), or `skill_domains` primary/secondary.
- Do NOT add skill domains that don't exist in `scripts/gen_prompts/cognitive_archetypes/skill_domains/`.
- Keep all atom `overrides` values within valid options from the atom YAML files.
- Preserve YAML formatting — use `|` block scalars for multi-line text.
- The `prefix` must start with `## Cognitive Architecture: <display_name>`.

---

## Definition of Done

- [ ] Every assigned figure has `heuristic` (top-level)
- [ ] Every assigned figure has `failure_modes` (top-level list, 2–4 items)
- [ ] Every prefix is ≥ 5 substantial paragraphs, figure-specific (not archetype boilerplate)
- [ ] Every suffix has 5–8 behavioral checkpoints
- [ ] `resolve_arch.py` produces output for at least one figure in your batch without error
- [ ] All changes committed to the worktree branch
