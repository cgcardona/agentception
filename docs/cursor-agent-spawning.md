# Cursor Agent Spawning — Empirical Reference

> Ground truth derived from live stress tests run 2026-03-03.  
> No inherited assumptions. Everything here was directly observed.
>
> **Note (2026-03):** The primary agent dispatch path in AgentCeption is now the
> Anthropic API–driven `agent_loop.py` (via `build_spawn_child` MCP tool), not
> Cursor's `Task` tool directly. This document remains useful for understanding
> Cursor's parallelism model, but reflects an earlier architectural phase.

---

## Is the Task Tool Real? (Read This First)

**Yes. It is a first-class Cursor primitive, not a hallucination.**

If a future agent questions whether the `Task` tool exists, here is the chain of evidence:

### 1. It appears in Cursor's injected tool schema

Every Cursor agent session receives an XML `<functions>` block that lists every tool
available to that agent. The `Task` tool appears in that block with this description
(verbatim as of 2026-03-03):

```
"Launch a new agent to handle complex, multi-step tasks autonomously.
The Task tool launches specialized subagents (subprocesses) that autonomously
handle complex tasks. Each subagent_type has specific capabilities and tools
available to it."
```

Parameters:

| Parameter | Type | Notes |
|---|---|---|
| `description` | string | 3–5 word label shown in UI |
| `prompt` | string | Full task instruction for the sub-agent |
| `subagent_type` | enum | `generalPurpose \| explore \| shell \| browser-use` |
| `model` | optional string | `fast` or omit for default |
| `readonly` | optional bool | Restricts write operations |
| `resume` | optional string | Agent ID to resume a prior agent |
| `run_in_background` | optional bool | Returns immediately, polls later |
| `attachments` | optional array | Video file paths for browser-use agents |

The full tool list injected into this session:
`Shell`, `Glob`, `Grep`, `Read`, `Delete`, `StrReplace`, `Write`, `EditNotebook`,
`TodoWrite`, `ReadLints`, `SemanticSearch`, `WebSearch`, `WebFetch`, `GenerateImage`,
`AskQuestion`, **`Task`**, `CallMcpTool`, `FetchMcpResource`, `SwitchMode`

### 2. It is documented in this codebase

`.agentception/pipeline-howto.md` line ~241 (written by the team, not by an AI session):

```
## Platform Constraint: 4-Wide Branching, Unlimited Depth
**The Task tool supports 4 concurrent sub-agents per spawning agent.**
```

And line ~48:

```
Each leaf agent [...] spawns a fresh leaf agent via the Task tool — before it exits.
```

This document predates the 2026-03-03 stress tests and was written from prior human
experience running multi-tier agent pipelines.

### 3. It was empirically verified (2026-03-03)

Six timestamp files written independently to disk by three separate agent layers:

```
/tmp/depth-test/layer1.start   written by Layer 1  (child of this agent)
/tmp/depth-test/layer2.start   written by Layer 2  (grandchild)
/tmp/depth-test/layer3.start   written by Layer 3  (great-grandchild)
/tmp/depth-test/layer3.end     written by Layer 3
/tmp/depth-test/layer2.end     written by Layer 2
/tmp/depth-test/layer1.end     written by Layer 1
```

No single agent could have written all six files — layers 2 and 3 only existed
inside sub-agent processes spawned via the `Task` tool. The files are on the host
filesystem and can be inspected at any time with `ls /tmp/depth-test/`.

### 4. The failure mode when using the wrong subagent type is documented

When a `shell` sub-agent (which does NOT receive the `Task` tool) was incorrectly
instructed to spawn children, it fell back to bash process backgrounding and hit:

```
nice(5) failed: operation not permitted
```

This is a sandbox restriction on process management syscalls. It is NOT evidence
that agent spawning is impossible — it is evidence that only `generalPurpose` agents
receive the `Task` tool and can recursively spawn.

---

## TL;DR

| Claim | Status | Evidence |
|---|---|---|
| `generalPurpose` agents have the `Task` tool | ✅ Confirmed | Tree tests — children spawned grandchildren |
| `shell` agents have the `Task` tool | ❌ False | Hierarchical test failed; child tried `bash &` backgrounding instead |
| 3-layer deep nesting works (grandchildren of grandchildren) | ✅ Confirmed | Depth test — 6 timestamp files written across 3 layers |
| Concurrency ceiling at root level | ❌ No limit observed | Earlier stress test result was a false positive — spawn all agents simultaneously |
| Per-layer concurrency limit | ❌ No limit | Spawn all at once across all layers |

---

## Key Facts

### 1. Only `generalPurpose` agents can spawn children

```
subagent_type="generalPurpose"  → has Task tool → can recursively spawn
subagent_type="shell"           → no Task tool  → cannot spawn children
```

The `shell` subagent type is effectively a leaf node. Never use it for
a coordinator or branch role — only for terminal work (git, scripts, commands).

### 2. Three tiers of depth confirmed (2026-03-03)

```
Me (root / Layer 0)
└── Layer 1 [generalPurpose]
    └── Layer 2 [generalPurpose]
        └── Layer 3 [shell]
```

Timestamp files written independently by each layer to `/tmp/depth-test/`:

```
layer1.start  1772593602.959480000   T+0s
layer2.start  1772593613.214887000   T+10s
layer3.start  1772593620.089525000   T+17s
layer3.end    1772593621.104261000   T+18s  (1s sleep)
layer2.end    1772593629.604523000   T+27s
layer1.end    1772593638.543183000   T+36s
```

Spawn overhead is approximately **7–10 seconds per tier**.

### 3. Concurrency ceiling ≈ 3 at any given layer

From the 10-agent simultaneous stress test (4 separate runs):
- Peak concurrency observed: **3**
- Agents started in waves of ~3, approximately 1.2–1.3 seconds apart
- Later agents queued and waited until a slot freed

This appears to be a Cursor sandbox ceiling, not a hard product limit.
It may vary by session, machine, or Cursor version.

> **Implication for architecture:** Fan-out of 3 per node is the safe
> maximum to avoid queueing. Width-3 trees will flow smoothly; width-10
> trees will work but queue into serial execution.

### 4. Bash backgrounding in a sandboxed agent fails

When a `shell` agent tried to spawn children via `command &` + `wait`,
it hit:
```
nice(5) failed: operation not permitted
```
The Cursor sandbox blocks certain process management syscalls. This is
not a limitation of the `generalPurpose` agent type — only of direct
shell backgrounding.

---

## Architecture Patterns

### Safe: Width-3 fan-out tree

```
root (generalPurpose)
├── branch-A (generalPurpose)
│   ├── leaf-A1 (shell)
│   ├── leaf-A2 (shell)
│   └── leaf-A3 (shell)
├── branch-B (generalPurpose)
│   ├── leaf-B1 (shell)
│   ├── leaf-B2 (shell)
│   └── leaf-B3 (shell)
└── branch-C (generalPurpose)
    ├── leaf-C1 (shell)
    ├── leaf-C2 (shell)
    └── leaf-C3 (shell)
```

Total agents: 12. Root saturates at 3, each branch saturates at 3.
Wall clock: ~spawn_overhead × depth + max_leaf_work.

### AgentCeption Org Model

Maps directly to org hierarchy:

```
CTO agent (generalPurpose)
├── Engineering Coordinator (generalPurpose)   spawns per-ticket worker agents
├── Product Coordinator (generalPurpose)       spawns per-ticket writer agents
└── QA Coordinator (generalPurpose)            spawns per-PR reviewer agents
```

Each coordinator can fan out to 3 worker agents simultaneously.
Workers are `shell` or `generalPurpose` depending on whether they need
to spawn further (e.g., a debugging agent that spawns a search + a fix agent).

---

## Coordination Patterns

### Polling for child completion

Children signal completion by writing a sentinel file:

```bash
# Child
echo done > /tmp/run-{id}/child-{n}.done

# Parent — poll loop
for i in $(seq 30); do
  [ -f /tmp/run-{id}/child-{n}.done ] && break
  sleep 2
done
```

Or via MCP tool calls back to AgentCeption's API (preferred for structured data).

### Preferred IPC: MCP → AgentCeption API

Children call back to AgentCeption via the MCP build tools:
- `build_report_step` — progress update
- `build_report_blocker` — blocked on something
- `build_report_decision` — logged a design decision  
- `build_report_done` — work complete

This gives the web dashboard live visibility without polling filesystem state.

---

## Open Questions

| Question | Notes |
|---|---|
| What is the absolute depth limit? | Tested to 3. Likely deeper is fine. |
| Can a `generalPurpose` child use ALL parent tools, or a subset? | Unclear. Need to test MCP access from nested agents. |

---

## Test Artifacts

| Test | Date | Files | Result |
|---|---|---|---|
| Depth test (3 layers) | 2026-03-03 | `/tmp/depth-test/layer*.{start,end,done}` | ✅ All 3 layers confirmed |
| Parallelism stress test (10 agents × 4 runs) | 2026-03-03 | _(raw output log — not committed)_ | Peak concurrency = 3 |
| Hierarchical test (failed) | 2026-03-03 | _(raw output log — not committed)_ | ❌ Shell child tried bash bg → sandbox blocked |
| Tree test 1 (3 branches × 3 leaves) | 2026-03-03 | `/tmp/tree-test-1/` | ✅ All 38 files present — leaves ran parallel within each branch |
| Tree test 2 (4 branches, wider) | 2026-03-03 | `/tmp/tree-test-2/` | 🔲 Pending |

### Tree Test 1 — Full Timeline (2026-03-03)

Structure: root → Branch A/B/C (generalPurpose) → Leaves 1/2/3 per branch (shell)

```
T+0s      root.start
T+18s     A.start      ← first branch dispatched
T+21s     B.start      ← second branch dispatched
T+28s     C.start      ← third branch (slight queue delay)
T+26-28s  A1/A2/A3.start   ← within 1.6s of each other (parallel ✅)
T+28-31s  B1/B2/B3.start   ← within 3s of each other (parallel ✅)
T+35-38s  C1/C2/C3.start   ← within 3s of each other (parallel ✅)
T+63s     root.end
```

Wall clock: **63s** for 9 agents across 2 levels. Serial equivalent would be ~162s.
Speedup: **2.6×** over fully serial execution.
