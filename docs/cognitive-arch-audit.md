# Cognitive Architecture Propagation Audit

This document audits every spawn site in the AgentCeption codebase and records
whether the `cognitive_architecture` field is correctly forwarded from parent to
child at each tier of the agent tree.

## Background

Each node in the agent tree receives a `cognitive_arch` string (e.g.
`feynman:llm:python`) that shapes its reasoning persona. The field must flow
unchanged from the root coordinator to every sub-coordinator and leaf agent it
spawns. Without explicit forwarding, each tier re-resolves from scratch, losing
the persona chosen at root dispatch time.

## Spawn sites

| File | Function / call | Forwarded? | Notes |
|------|----------------|------------|-------|
| `agentception/services/spawn_child.py` | `spawn_child()` — main spawning service | ✅ Fixed in [#176] | Added `cognitive_arch: str \| None = None` parameter. When set, skips `_resolve_cognitive_arch()` and uses the provided value verbatim. |
| `agentception/mcp/build_tools.py` | `build_spawn_child()` — MCP tool callable | ✅ Fixed in [#176] | Added `cognitive_arch: str = ""` parameter, passed through to `spawn_child()`. |
| `agentception/mcp/server.py` | `call_tool_async()` — MCP JSON-RPC dispatcher | ✅ Fixed in [#176] | Extracts `cognitive_arch` from tool arguments and passes it to `build_spawn_child()`. MCP schema updated to advertise the new field. |
| `agentception/services/task_builders.py` | `_build_coordinator_task()` | N/A — initial dispatch only | Builds the root coordinator task at UI dispatch time. The arch is resolved once here from `ROLE_DEFAULT_FIGURE`; there is no parent to forward from. |
| `agentception/services/task_builders.py` | `_build_conductor_task()` | N/A — initial dispatch only | Same as above — root entry point. |
| `agentception/services/task_builders.py` | `_build_agent_task()` | N/A — initial dispatch only | Builds leaf tasks dispatched directly from the UI (not via a coordinator). Arch is resolved at creation time. |

## How coordinators must use the fixed API

Whenever a coordinator spawns a child — whether another coordinator or a leaf
engineer — it **must** pass its own `cognitive_arch` to `build_spawn_child`:

```python
result = await build_spawn_child(
    parent_run_id=self.run_id,
    role="python-developer",
    tier="engineer",
    scope_type="issue",
    scope_value=str(issue_number),
    gh_repo=gh_repo,
    cognitive_arch=self.cognitive_arch,  # always forward — never omit
)
```

Omitting `cognitive_arch` causes the child to fall back to keyword extraction
from `issue_body`, which may produce a different persona than the root intended.

## Test coverage

`tests/test_spawn_child.py` contains three tests added in this fix:

| Test | What it verifies |
|------|-----------------|
| `test_spawn_child_forwards_cognitive_arch_without_resolving` | `_resolve_cognitive_arch` is never called when `cognitive_arch` is provided |
| `test_spawn_child_resolves_arch_when_not_provided` | Fallback resolution still works when `cognitive_arch` is omitted |
| `test_cognitive_arch_propagates_to_leaf` | End-to-end: root arch arrives unchanged on the leaf after two spawn hops |

## Fixed in

PR #176 — "Propagate cognitive architecture field through all coordinator spawn calls"
