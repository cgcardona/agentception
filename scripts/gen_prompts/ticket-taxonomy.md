# AgentCeption Ticket Taxonomy

> Every open issue mapped to its phase, tech stack, and optimal `COGNITIVE_ARCH`.
> This document drives the auto-selection logic in `engineering-coordinator.md.j2`.
> Update when new issues are added or the skill domain library grows.

---

## Tech Stack Analysis

The AgentCeption dashboard is a **FastAPI + Jinja2 + HTMX** application.
Issues break into five distinct technical stacks, now using **atomic skill domains**
(one technology per skill file, up to 3 combined per agent):

| Stack | Count | Skill Domain(s) |
|-------|-------|----------------|
| Pure Python (async, data processing, REST) | 12 | `python` |
| FastAPI routes + dependency injection | 8 | `fastapi:python` |
| **HTMX partial updates** | **13** | **`htmx:jinja2:alpine`** |
| D3.js (force-directed graph) | 1 | `d3:javascript` |
| Monaco editor (in-browser code editor) | 1 | `monaco` |
| DevOps (Docker, Compose) | 1 | `devops` |
| PostgreSQL / Alembic migrations | 2 | `postgresql:python` |
| Documentation only | 1 | `python` |

**Key insight:** HTMX+Jinja2 is the dominant pattern (45% of all tickets).
With the new atomic skill format, each HTMX ticket gets `htmx:jinja2:alpine`
instead of the deprecated `htmx_jinja2` compound domain — three focused skill
files that can each be loaded independently for mixed-stack tickets.

---

## Full Issue Map

### Phase 0 — Scaffold (sequential, foundational)

| # | Title | Primary Stack | Signal Keywords | `COGNITIVE_ARCH` | Rationale |
|---|-------|--------------|-----------------|------------------|-----------|
| #648 | Docker runtime — Dockerfile, compose service | DevOps | docker, dockerfile, compose, HOME-relative | `ritchie:devops` | Minimal tools that compose cleanly. Ritchie's Unix philosophy maps directly to Docker service design. |
| #614 | poller.py — asyncio background task, SSE broadcast | Python | asyncio, SSE, broadcast, PipelineState, subscribe | `shannon:python` | Shannon thinks in information flows and channels. A poller that merges sources and broadcasts is exactly a Shannon problem: latency, throughput, subscriber fanout. |
| #615 | Pipeline overview UI — live tree, status badges, GitHub board | HTMX+Jinja2 | htmx, sse-swap, hx-ext, template, overview.html | `lovelace:htmx:jinja2:alpine` | Lovelace sees the machine behind the machine. The overview page IS the meta-view of the agent pipeline — she would design it to reveal the system's structure, not just display data. |
| #616 | Agent inspector UI — transcript viewer, DB context display | HTMX+Jinja2 | agents/{id}, transcript, agent.html, detail page | `feynman:htmx:jinja2` | Feynman's job is to make the invisible visible. An agent inspector that surfaces what agents are actually doing is a Feynman problem: expose the internals clearly. |

### Phase 1 — Controls

| # | Title | Primary Stack | Signal Keywords | `COGNITIVE_ARCH` | Rationale |
|---|-------|--------------|-----------------|------------------|-----------|
| #617 | Kill endpoint — remove worktree + clear agent:wip | Python | kill, worktree, rm -rf, label, correctness | `the_guardian:python` | Kill is irreversible. The Guardian's fail-loud, deductive rigor is exactly right — every edge case (concurrent kill, already-dead agent, missing worktree) must be handled explicitly. |
| #618 | Pause/resume pipeline sentinel + UI toggle | Python+HTMX | pause, resume, sentinel, .pipeline-pause, toggle | `the_operator:htmx:jinja2` | The Operator keeps the system running reliably. Pause/resume is operational infrastructure — it needs retry-first, probabilistic reasoning, and terse correctness. |
| #619 | Manual spawn endpoint + issue picker UI | HTMX+Jinja2 | spawn, issue picker, form, POST /api/control/spawn | `hopper:htmx:jinja2:alpine` | Hopper built tools that let humans do more. The manual spawn UI is a direct-control tool — she would make it fast, tactile, and clear about what it's doing. |

### Phase 2 — Telemetry

| # | Title | Primary Stack | Signal Keywords | `COGNITIVE_ARCH` | Rationale |
|---|-------|--------------|-----------------|------------------|-----------|
| #620 | Wave aggregator — group by BATCH_ID, compute timing | Python | wave, BATCH_ID, aggregate, mtime, timing | `von_neumann:python` | Von Neumann bursts through the entire problem space. Wave aggregation requires holding many correlated data points simultaneously and synthesizing them — his burst+comprehensive style is optimal. |
| #621 | Cost estimator — token proxy + Claude pricing | Python | cost, token, estimate, pricing, message_count | `hamming:python` | Hamming's heuristic: "work on the important problem." Cost estimation IS the important problem for pipeline economics. He would find the simplest proxy that gives actionable signal. |
| #622 | Telemetry UI — CSS timeline bar chart + wave table | HTMX+Jinja2 | telemetry, CSS bar chart, wave summary, table | `the_mentor:htmx:jinja2` | The Mentor writes code that teaches. A telemetry dashboard should make complex timing data intuitively legible — the Mentor's visual, expository style produces the most useful dashboards. |

### Phase 3 — Roles (Role Studio)

| # | Title | Primary Stack | Signal Keywords | `COGNITIVE_ARCH` | Rationale |
|---|-------|--------------|-----------------|------------------|-----------|
| #623 | Role file reader/writer API — list, read, write, git history | Python | API, REST, /api/roles, read, write, git log | `the_architect:fastapi:python` | The Architect designs before building. The role file API is load-bearing infrastructure — it must have a clean interface with no path traversal, validated slugs, and a stable contract for the editor above it. |
| #624 | Monaco editor in browser for role files | Monaco+HTMX | monaco, editor, CDN, vs/loader, markdown, yaml | `lovelace:monaco:htmx` | Lovelace sees the machine behind the machine AND the system that creates machines. A tool for editing cognitive architectures in the browser is deeply recursive — she is the right figure to design it. |
| #625 | Diff preview + Save & Commit flow | Python+HTMX | diff, unified diff, HEAD, git commit, confirm | `dijkstra:python:htmx` | Dijkstra: correctness is not optional. A diff-before-commit flow is about preventing irreversible mistakes — his deductive, fail-loud style ensures every save path is provably safe. |
| #626 | pipeline-config.json — CTO/VP read from file | Python | pipeline-config.json, max_pool_size, phases, read | `the_architect:python` | Another load-bearing design. The pipeline config schema must be forward-compatible, validated, and have a clear migration story. Architect mode. |
| #627 | Pipeline config UI — sliders for VP count and pool size | HTMX+Jinja2 | sliders, config, pipeline-config.json, one-click | `the_pragmatist:htmx:jinja2:alpine` | Pragmatist for a configuration UI: it needs to work cleanly and ship without over-engineering. Simple sliders, immediate feedback, done. |

### Phase 4 — Intelligence

| # | Title | Primary Stack | Signal Keywords | `COGNITIVE_ARCH` | Rationale |
|---|-------|--------------|-----------------|------------------|-----------|
| #628 | Dependency DAG builder — parse "Depends on #NNN" | Python | DAG, parse, Depends on, directed, acyclic, graph | `turing:python` | Turing builds formal machines to answer questions. The DAG builder is a formal parsing + graph construction problem — he would define the grammar first, then implement it. |
| #629 | DAG visualization — D3 force-directed graph | D3.js | D3, force-directed, SVG, node, edge, phase color | `lovelace:d3:javascript` | Lovelace would be astounded that you can render a live graph of thinking machines in a browser. She would design it beautifully and make the structure self-evident. |
| #630 | Out-of-order PR guard — detect ordering violations | Python | out-of-order, phase label, alert, ordering, guard | `the_guardian:python` | Guardian is the immune system. Out-of-order detection IS guardian behavior — detect violations, surface loudly, block progression until fixed. |
| #631 | Stale claim detector + one-click auto-fix | Python+HTMX | stale claim, agent:wip, no worktree, one-click fix | `the_operator:python:htmx` | The Operator keeps the lights on. Stale claim detection is operational hygiene — retry-first, empirical, and pragmatic about auto-remediation. |
| #632 | Ticket analyzer endpoint — parse issue for parallelism/deps | Python | analyze, parse issue body, parallelism, dependency | `mccarthy:python` | McCarthy: formalize first, solve within the formalism. Ticket analysis is a natural language parsing problem that should be expressed as a formal classifier. |
| #633 | Ticket analyzer UI panel — inline analysis on board | HTMX+Jinja2 | analyze button, dropdown, issue card, inline | `the_pragmatist:htmx:jinja2` | The analysis results panel is a UI concern. Pragmatist: wire up the endpoint, display results clearly, ship. |

### Phase 5 — Scaling

| # | Title | Primary Stack | Signal Keywords | `COGNITIVE_ARCH` | Rationale |
|---|-------|--------------|-----------------|------------------|-----------|
| #634 | Auto-scaling advisor engine | Python | scaling, advisor, queue depth, PR backlog, heuristic | `hamming:python` | Hamming again: what is the important metric? Queue depth is the lever. He would find the minimal set of signals that give actionable scaling advice. |
| #635 | Scaling recommendation UI + one-click apply | HTMX+Jinja2 | recommendation, banner, one-click, apply, dismiss | `the_mentor:htmx:jinja2` | Mentor: the recommendation UI should teach the operator why the scaling advice is being given, not just what to click. |
| #636 | Role version tracking schema — role-versions.json | Python | role-versions, version, track, A/B, schema | `the_guardian:python` | Version tracking is correctness infrastructure. Guardian ensures the schema is sound, the writes are atomic, and the history is never corrupted. |
| #637 | A/B mode in Eng VP — alternate role files by BATCH_ID | Python | A/B, variant, even/odd BATCH_ID, role file | `hopper:python` | Hopper: build the experiment, observe, iterate. A/B testing IS her methodology. She would instrument it clearly and make the results easy to read. |
| #638 | A/B results dashboard — compare role versions | HTMX+Jinja2 | A/B results, comparison table, PR outcomes, mypy | `shannon:htmx:jinja2` | Shannon thinks in information theory. An A/B dashboard is fundamentally about signal vs noise — which role variant produces better outcomes? He would design the comparison to reveal the signal. |

### Phase 6 — Generalization

| # | Title | Primary Stack | Signal Keywords | `COGNITIVE_ARCH` | Rationale |
|---|-------|--------------|-----------------|------------------|-----------|
| #639 | Multi-repo config schema + project switcher UI | Python+HTMX | multi-repo, config schema, project, switcher | `the_architect:python:htmx` | Generalization requires the cleanest possible interface. Architect mode — design the schema that supports any repo before building the first one. |
| #640 | Template export/import — .tar.gz packaging | Python | export, import, tar.gz, template, config | `ritchie:python` | Ritchie: the smallest tool that does the job. A tar.gz exporter is a Unix pipe problem — compose existing tools cleanly. |
| #641 | README, extraction prep, standalone repo scaffold | Documentation | README, pyproject.toml, extraction, standalone | `feynman:python` | Feynman: if you cannot explain it simply, you don't understand it. A README that lets a stranger onboard without asking questions is a Feynman problem. |

---

## Heuristic Rules (implemented in engineering-coordinator)

The engineering coordinator runs these checks against each issue's body + labels
to auto-select `COGNITIVE_ARCH`. Rules are checked in priority order — first match wins.
The new format emits colon-separated skills (up to 3), replacing the deprecated `+` format.

### Skill Domain Detection (new atomic format)

```bash
ISSUE_BODY="$(gh issue view $NUM --repo $GH_REPO --json body -q .body)"

SKILLS=""
if echo "$ISSUE_BODY" | grep -qiE "monaco|vs/loader|editor.*cdn|cdn.*editor"; then
  SKILLS="monaco"
elif echo "$ISSUE_BODY" | grep -qiE "d3\.js|force-directed|d3\.forceSimulation|d3\.select"; then
  SKILLS="d3:javascript"
elif echo "$ISSUE_BODY" | grep -qiE "htmx|hx-|sse-connect|hx-ext"; then
  SKILLS="htmx"
  echo "$ISSUE_BODY" | grep -qiE "jinja2|\.html|TemplateResponse|{% block" && SKILLS="${SKILLS}:jinja2"
  echo "$ISSUE_BODY" | grep -qiE "alpine|x-data|x-show" && SKILLS="${SKILLS}:alpine"
elif echo "$ISSUE_BODY" | grep -qiE "jinja2|TemplateResponse|{% block"; then
  SKILLS="jinja2"
elif echo "$ISSUE_BODY" | grep -qiE "postgres|alembic|migration|sqlalchemy"; then
  SKILLS="postgresql:python"
elif echo "$ISSUE_BODY" | grep -qiE "dockerfile|docker compose|FROM python|container.*port"; then
  SKILLS="devops"
elif echo "$ISSUE_BODY" | grep -qiE "midi|audio|music"; then
  SKILLS="midi:python"
elif echo "$ISSUE_BODY" | grep -qiE "llm|embedding|rag|anthropic|claude.*model"; then
  SKILLS="llm:python"
elif echo "$ISSUE_BODY" | grep -qiE "APIRouter|FastAPI|Depends|response_model"; then
  SKILLS="fastapi:python"
else
  SKILLS="python"
fi
```

### Figure/Archetype Detection

```bash
if echo "$ISSUE_BODY" | grep -qiE "parse.*body|depends on|DAG|directed.acyclic|formal"; then
  FIGURE="turing"
elif echo "$ISSUE_BODY" | grep -qiE "kill|guard|stale.*claim|out-of-order|correctness|invariant"; then
  FIGURE="the_guardian"
elif echo "$ISSUE_BODY" | grep -qiE "asyncio|SSE|broadcast|subscribe|fanout|information flow"; then
  FIGURE="shannon"
elif echo "$ISSUE_BODY" | grep -qiE "readme|explain|tutorial|document|onboard"; then
  FIGURE="feynman"
elif echo "$ISSUE_BODY" | grep -qiE "docker|compose|FROM|entrypoint|service.*port"; then
  FIGURE="ritchie"
elif echo "$ISSUE_BODY" | grep -qiE "scaling|advisor|heuristic|queue.*depth|important.*problem"; then
  FIGURE="hamming"
elif echo "$ISSUE_BODY" | grep -qiE "wave|aggregate|batch_id|synthesize|cross.*domain|burst"; then
  FIGURE="von_neumann"
elif echo "$ISSUE_BODY" | grep -qiE "diff|version|schema|load-bearing|interface.*design|config.*schema"; then
  FIGURE="the_architect"
elif echo "$ISSUE_BODY" | grep -qiE "visualization|render|graph.*beautiful|D3|force.*directed|SVG"; then
  FIGURE="lovelace"
elif echo "$ISSUE_BODY" | grep -qiE "spawn|tool.*for|manual.*control|direct.*access"; then
  FIGURE="hopper"
elif echo "$ISSUE_BODY" | grep -qiE "classify|analyze|formal.*model|natural.*language|parse.*intent"; then
  FIGURE="mccarthy"
elif echo "$ISSUE_BODY" | grep -qiE "A/B|experiment|variant|observe.*iterate"; then
  FIGURE="hopper"
elif echo "$ISSUE_BODY" | grep -qiE "inspector|detail.*page|transcript.*viewer|make.*visible"; then
  FIGURE="feynman"
elif echo "$ISSUE_BODY" | grep -qiE "overview.*page|live.*tree|dashboard.*main|meta.*view"; then
  FIGURE="lovelace"
elif echo "$ISSUE_BODY" | grep -qiE "recommendation|mentor.*style|comparison.*table|teach"; then
  FIGURE="the_mentor"
elif echo "$ISSUE_BODY" | grep -qiE "pause|resume|sentinel|operational|keep.*running"; then
  FIGURE="the_operator"
else
  FIGURE="the_pragmatist"
fi

COGNITIVE_ARCH="${FIGURE}:${SKILLS}"
```

---

## Skill Domain Coverage by Phase

```
Phase 0:  devops  python  htmx:jinja2:alpine  htmx:jinja2
Phase 1:  python  htmx:jinja2  htmx:jinja2:alpine
Phase 2:  python  python  htmx:jinja2
Phase 3:  fastapi:python  monaco:htmx  python:htmx  python  htmx:jinja2:alpine
Phase 4:  python  d3:javascript  python  python:htmx  python  htmx:jinja2
Phase 5:  python  htmx:jinja2  python  python  htmx:jinja2
Phase 6:  python:htmx  python  python
```

**13 of 29 tickets need HTMX skill domain.** With atomic skills, HTMX issues
are now further subdivided: pure HTMX + Jinja2, HTMX + Alpine.js, or all three.
Mixed-stack tickets (e.g. `#625`: Diff preview with both Python logic and HTMX UI)
now get `python:htmx` instead of being forced into a single monolithic category.

**Format:** `figure:skill1:skill2` (atomic, colon-separated, up to 3 skills)
