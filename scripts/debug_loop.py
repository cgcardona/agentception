"""Step-by-step agent loop debugger — drives the loop itself, turn by turn.

Usage (inside the container):

    docker compose exec agentception python3 /app/scripts/debug_loop.py

The script creates its own isolated worktree (launch=False), runs the full
agent loop, and prints every LLM call and every tool result in real time.
Edit TASK_DESCRIPTION below to change what the agent is asked to do.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "gen_prompts"))
sys.path.insert(0, "/app")


TASK_DESCRIPTION = """
Fix agentception/db/queries.py — issue #274.

## Context

`get_conductor_history` in `agentception/db/queries.py` (around line 1149)
derives each conductor wave's status by calling `worktree.exists()` — a
filesystem check. This must be replaced with a DB status query so the
function has zero filesystem access.

## Current problem code (around line 1188)

```python
status="active" if worktree.exists() else "completed",
```

## What to do

1. Read `agentception/db/queries.py` around `get_conductor_history` to
   understand the full function. Also read the `ACAgentRun` model in
   `agentception/db/models.py` to confirm the `status` and `wave_id` fields.

2. Restructure the DB query to also fetch the latest `ACAgentRun.status`
   for each wave in the same session. The cleanest approach is a single
   query that LEFT JOINs or uses a subquery to get the most recent
   `ac_agent_runs.status` per `wave_id`. Keep the session open for the
   entire operation.

3. Map DB statuses to the display value:
   - `status IN ('implementing', 'review')` → `"active"`
   - anything else (completed, failed, cancelled, None) → `"completed"`
   Add a comment above the mapping:
   `# Replaced filesystem worktree check — status is the authoritative signal.`

4. Do NOT remove the `worktree` and `host_worktree` path fields from
   `ConductorHistoryRow` — those path strings are still used by the UI.
   Only the `.exists()` call goes away.

5. Remove any `import os` or standalone `from pathlib import Path` lines
   that become unused after the change. `Path` may still be needed for
   constructing path strings — check before removing.

6. Run `mypy agentception/db/queries.py` — must pass with zero errors.

7. Update `agentception/tests/test_agentception_run_conductor.py`:
   - Rename the existing test
     `test_get_conductor_history_status_resolved_from_worktree_dir` to
     `test_get_conductor_history_status_resolved_from_db`.
   - Replace worktree directory setup/teardown with a mock DB row
     whose `status` is `"implementing"` — assert result is `"active"`.
   - Add a regression test
     `test_get_conductor_history_no_fs_access` that asserts
     `Path.exists` is never called (patch it and assert no-call).

8. Create branch `fix/274-conductor-history-no-fs`, commit all changes,
   push, open a PR against `dev` referencing issue #274 in the body.
   Do NOT run pytest (CI is not required here).
"""

MAX_TURNS = 30
TURN_DELAY_SECS = 10  # fixed pause between turns — keeps cadence readable and under rate limit


def _hr(label: str) -> None:
    width = 72
    print(f"\n{'─' * width}", flush=True)
    print(f"  {label}", flush=True)
    print(f"{'─' * width}", flush=True)


def _dump(label: str, value: object, max_chars: int = 800) -> None:
    text = str(value) if not isinstance(value, str) else value
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n  … [{len(str(value)) - max_chars} chars omitted]"
    print(f"\n{label}:\n{textwrap.indent(text, '  ')}", flush=True)


async def main() -> None:
    from agentception.db.engine import init_db
    await init_db()

    from agentception.services.run_factory import create_and_launch_run
    from agentception.services.agent_loop import (
        _build_system_prompt,
        _build_tool_definitions,
        _dispatch_tool_calls,
        _load_task,
        _load_role_prompt,
        _fetch_task_briefing,
        _truncate_tool_results,
    )
    from agentception.services.github_mcp_client import GitHubMCPClient
    from agentception.services.llm import call_anthropic_with_tools

    # ════════════════════════════════════════════════════════════════════════
    # STEP 0 — Create a fresh run (worktree + DB row) WITHOUT starting the loop
    # ════════════════════════════════════════════════════════════════════════
    _hr("STEP 0 — Create run (launch=False)")

    info = await create_and_launch_run(
        role="developer",
        task_description=TASK_DESCRIPTION,
        launch=False,
    )
    run_id: str = info["run_id"]
    worktree_path = Path(info["worktree_path"])
    print(f"  ✅ run_id        = {run_id}")
    print(f"     worktree_path = {worktree_path}")
    print(f"     cognitive_arch= {info['cognitive_arch']}")

    # ════════════════════════════════════════════════════════════════════════
    # STEP 1 — Load task context from the DB
    # ════════════════════════════════════════════════════════════════════════
    _hr("STEP 1 — Load task context from DB")

    task = await _load_task(run_id, worktree_path)
    if task is None:
        print(f"  ❌ No DB row found for run_id={run_id!r}")
        return

    print(f"  ✅ Task loaded — role={task.role}  arch={task.cognitive_arch}")

    # ════════════════════════════════════════════════════════════════════════
    # STEP 2 — Build the system prompt
    # ════════════════════════════════════════════════════════════════════════
    _hr("STEP 2 — Build system prompt")

    role_prompt = _load_role_prompt(task.role)
    system_prompt = _build_system_prompt(role_prompt, task.cognitive_arch or "")
    print(f"  System prompt: {len(system_prompt)} chars total")

    # ════════════════════════════════════════════════════════════════════════
    # STEP 3 — Start GitHub MCP client + build combined tool catalogue
    # ════════════════════════════════════════════════════════════════════════
    _hr("STEP 3 — Start GitHub MCP + build tool catalogue")

    github_client = GitHubMCPClient()
    github_tool_names: frozenset[str] = frozenset()
    try:
        github_tools = await github_client.list_tools()
        github_tool_names = frozenset(t["function"]["name"] for t in github_tools)
        print(f"  ✅ GitHub MCP: {len(github_tools)} tools")
    except RuntimeError as exc:
        github_tools = []
        print(f"  ⚠️  GitHub MCP unavailable: {exc}")

    tool_defs = _build_tool_definitions(extra_tools=github_tools)
    print(f"  Total tools: {len(tool_defs)}")

    # ════════════════════════════════════════════════════════════════════════
    # STEP 4 — Fetch the task briefing (first user message)
    # ════════════════════════════════════════════════════════════════════════
    _hr("STEP 4 — Fetch task briefing")

    initial_message = await _fetch_task_briefing(run_id, task, worktree_path)
    print(f"  Briefing: {len(initial_message)} chars")

    messages: list[dict[str, object]] = [{"role": "user", "content": initial_message}]

    # ════════════════════════════════════════════════════════════════════════
    # TURNS 1-N — run the loop in real time
    # ════════════════════════════════════════════════════════════════════════
    for turn in range(1, MAX_TURNS + 1):
        _hr(f"LLM Turn {turn}  (history: {len(messages)} msgs)")
        print("  Calling Anthropic …", flush=True)

        try:
            response = await call_anthropic_with_tools(
                messages,
                system=system_prompt,
                tools=tool_defs,
            )
        except Exception as exc:
            print(f"  ❌ LLM error: {exc}")
            break

        input_tokens = response.get("input_tokens", 0)
        cache_written = response.get("cache_creation_input_tokens", 0)
        cache_read = response.get("cache_read_input_tokens", 0)
        cache_note = ""
        if cache_written:
            cache_note = f"  (✍ wrote {cache_written} to cache)"
        elif cache_read:
            cache_note = f"  (⚡ read {cache_read} from cache — ~10% cost)"
        else:
            cache_note = "  (⚠️  no cache hit)"
        print(f"  stop_reason   = {response['stop_reason']}")
        print(f"  input tokens  = {input_tokens}{cache_note}")
        print(f"  tool_calls    = {len(response['tool_calls'])}")

        # Fixed inter-turn delay — keeps cadence steady and observable.
        # After Turn 1 writes the system prompt to cache, subsequent turns
        # only send ~1-2k uncached tokens, so 10s between calls puts us at
        # ~12k tokens/min — well under the 30k/min ceiling with no bursting.
        if response["stop_reason"] != "stop":
            print(f"\n  ⏳ waiting {TURN_DELAY_SECS}s before next turn …", flush=True)
            await asyncio.sleep(TURN_DELAY_SECS)

        if response["content"]:
            _dump("Model text", response["content"])

        assistant_msg: dict[str, object] = {"role": "assistant", "content": response["content"]}
        if response["tool_calls"]:
            assistant_msg["tool_calls"] = list(response["tool_calls"])
        messages.append(assistant_msg)

        if response["stop_reason"] == "stop":
            _hr(f"✅ COMPLETE on turn {turn}")
            print(f"  Total LLM turns: {turn}")
            break

        # ── execute tool calls ───────────────────────────────────────────
        print(f"\n  ── {len(response['tool_calls'])} tool call(s) ──", flush=True)
        for i, tc in enumerate(response["tool_calls"], 1):
            name = tc["function"]["name"]
            args: dict[str, object] = json.loads(tc["function"]["arguments"])
            arg_preview = ", ".join(
                f"{k}={str(v)[:60]!r}" for k, v in list(args.items())[:3]
            )
            print(f"  [{i}] {name}({arg_preview})", flush=True)

        tool_results = await _dispatch_tool_calls(
            response["tool_calls"],
            worktree_path,
            run_id,
            github_client=github_client,
            github_tool_names=github_tool_names,
        )

        for i, tr in enumerate(tool_results, 1):
            content = tr.get("content", "")
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    display = json.dumps(parsed, indent=2)
                except Exception:
                    display = content
            else:
                display = str(content)
            _dump(f"  Result [{i}]", display)

        messages.extend(tool_results)
        messages = _truncate_tool_results(messages)

    else:
        _hr(f"⚠️  Hit {MAX_TURNS}-turn ceiling without stop")

    print(flush=True)
    await github_client.close()


if __name__ == "__main__":
    asyncio.run(main())
