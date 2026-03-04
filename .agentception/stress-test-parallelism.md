# Parallelism Stress Test — Coordinator Prompt

> **Purpose:** Empirically determine Cursor's actual Task concurrency ceiling.
> No application code is written. All output is ephemeral under `/tmp/stress-test/`.
> Run this, observe the timing report, then we have ground truth.

---

## How to run

Paste the coordinator prompt below into a fresh Cursor agent session.
The agent will:

1. Create 10 worktrees at `/tmp/stress-test/agent-{01..10}`
2. Write a trivial `.agent-task` into each
3. Fire all 10 Tasks **simultaneously in a single message**
4. Each sub-agent records its start/end timestamps, sleeps 3 s, then writes a `.done` file
5. Coordinator polls until all `.done` files exist (or 90 s timeout)
6. Reads timestamps, computes overlap, prints the parallelism report

The sleep is 3 seconds deliberately: if all 10 agents start within 3 s of each other,
they overlap — true parallelism. If they start sequentially with 3+ s gaps, they are
being queued.

---

## Coordinator Prompt

```
You are running a Cursor Task parallelism stress test.
Your only job is to set up the experiment, launch agents, collect results, and
print a report. Do NOT write any application code. Do NOT commit anything.

────────────────────────────────────────────────────────────
STEP 1 — Setup (run these shell commands NOW)
────────────────────────────────────────────────────────────

Run the following as a single shell script:

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE=/tmp/stress-test
rm -rf "$BASE"
mkdir -p "$BASE"

for i in $(seq -w 1 10); do
  DIR="$BASE/agent-$i"
  mkdir -p "$DIR"
  cat > "$DIR/.agent-task" <<TOML
[task]
id = "stress-$i"
label = "Stress test agent $i"
sleep_seconds = 3

[output]
start_file = "$DIR/.start"
end_file   = "$DIR/.end"
done_file  = "$DIR/.done"
TOML
done

echo "Setup complete — 10 task directories created under $BASE"
ls "$BASE"
```

Confirm the 10 directories exist before proceeding.

────────────────────────────────────────────────────────────
STEP 2 — Launch all 10 agents simultaneously
────────────────────────────────────────────────────────────

Fire ALL of the following Task calls in a SINGLE response (not one at a time).
This is the critical measurement: they must all be dispatched at once.

The kickoff prompt for every agent is identical — see KICKOFF_PROMPT below.

Launch:
  Task(worktree="/tmp/stress-test/agent-01", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-02", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-03", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-04", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-05", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-06", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-07", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-08", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-09", prompt=KICKOFF_PROMPT)
  Task(worktree="/tmp/stress-test/agent-10", prompt=KICKOFF_PROMPT)

KICKOFF_PROMPT = """
You are a stress test sub-agent. Follow these exact steps and do nothing else.

1. Read your task file:
   cat /tmp/stress-test/agent-*/agent-task   # (or read the .agent-task in your working directory)

2. Record your start time with nanosecond precision:
   date +%s.%N > .start

3. Sleep for exactly 3 seconds:
   sleep 3

4. Record your end time:
   date +%s.%N > .end

5. Write your done marker (include your agent ID from the .agent-task [task].id field):
   echo "$(cat .agent-task | grep '^id' | cut -d'\"' -f2)" > .done

6. Print: "STRESS TEST AGENT <id> COMPLETE. start=$(cat .start) end=$(cat .end)"

That is all. Do not read any code. Do not run mypy or pytest. Do not open PRs.
"""

────────────────────────────────────────────────────────────
STEP 3 — Poll for completion
────────────────────────────────────────────────────────────

After launching all tasks, poll every 5 seconds until all 10 `.done` files exist
OR 90 seconds have elapsed. Run this shell loop:

```bash
BASE=/tmp/stress-test
DEADLINE=$(($(date +%s) + 90))
while true; do
  DONE=$(find "$BASE" -name ".done" | wc -l | tr -d ' ')
  echo "$(date +%H:%M:%S) — $DONE/10 done"
  if [ "$DONE" -eq 10 ]; then
    echo "All 10 agents completed."
    break
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "TIMEOUT — only $DONE/10 completed within 90 s."
    break
  fi
  sleep 5
done
```

────────────────────────────────────────────────────────────
STEP 4 — Print the parallelism report
────────────────────────────────────────────────────────────

Run this analysis script and print the full output:

```bash
#!/usr/bin/env python3
"""Parallelism analyser — reads .start/.end files and prints overlap report."""
import os, glob, json
from pathlib import Path

BASE = Path("/tmp/stress-test")

agents = []
for d in sorted(BASE.glob("agent-*")):
    start_f = d / ".start"
    end_f   = d / ".end"
    done_f  = d / ".done"
    if not start_f.exists():
        agents.append({"id": d.name, "status": "DID_NOT_START"})
        continue
    start = float(start_f.read_text().strip())
    end   = float(end_f.read_text().strip()) if end_f.exists() else None
    done  = done_f.exists()
    agents.append({"id": d.name, "start": start, "end": end, "done": done})

# Sort by start time
started = [a for a in agents if "start" in a]
started.sort(key=lambda a: a["start"])

if not started:
    print("No agents started.")
    exit()

t0 = started[0]["start"]  # reference epoch

print("\n══════════════════════════════════════════")
print("  CURSOR TASK PARALLELISM — STRESS REPORT")
print("══════════════════════════════════════════\n")
print(f"{'Agent':<12} {'Start (s)':<12} {'End (s)':<12} {'Duration':<10} Status")
print("-" * 60)
for a in started:
    rel_start = a["start"] - t0
    rel_end   = (a["end"] - t0) if a.get("end") else None
    duration  = (a["end"] - a["start"]) if a.get("end") else None
    status    = "✅ done" if a.get("done") else ("⏳ running" if a.get("end") is None else "❌ no .done")
    end_str   = f"{rel_end:.2f}" if rel_end is not None else "—"
    dur_str   = f"{duration:.2f}s" if duration is not None else "—"
    print(f"{a['id']:<12} +{rel_start:<11.2f} +{end_str:<11} {dur_str:<10} {status}")

# Overlap analysis
print("\n── Overlap analysis ────────────────────────────────")
start_times = [a["start"] - t0 for a in started]
spread = max(start_times) - min(start_times)
print(f"  First agent started at: +0.00 s (reference)")
print(f"  Last  agent started at: +{spread:.2f} s")
print(f"  Start spread (first→last): {spread:.2f} s")
print()

if spread < 3.0:
    print("  VERDICT: TRUE PARALLELISM — all agents started within the 3 s sleep window.")
    print(f"  All {len(started)} agents ran concurrently.")
elif spread < 6.0:
    # Some queuing happened
    wave_size = sum(1 for t in start_times if t < 3.0)
    print(f"  VERDICT: PARTIAL PARALLELISM — first wave of ~{wave_size} agents ran concurrently,")
    print(f"  remaining agents queued and started after the first wave completed.")
else:
    print("  VERDICT: SERIALIZED — agents appear to have run one-at-a-time or in very small batches.")

# Count agents that definitely overlapped
overlapping = sum(1 for t in start_times if t < 3.0)  # started before first agent finished
print(f"\n  Peak concurrent agents (started within first 3 s): {overlapping}")

# Did any not start at all?
not_started = [a for a in agents if "start" not in a]
if not_started:
    print(f"\n  ⚠️  {len(not_started)} agent(s) never wrote a .start file:")
    for a in not_started:
        print(f"     {a['id']} — {a['status']}")

print("\n══════════════════════════════════════════\n")
```

────────────────────────────────────────────────────────────
STEP 5 — Cleanup
────────────────────────────────────────────────────────────

```bash
rm -rf /tmp/stress-test
echo "Cleaned up."
```

────────────────────────────────────────────────────────────
REPORT BACK
────────────────────────────────────────────────────────────

After printing the report, answer:
1. How many of the 10 Tasks actually fired (had a .start file)?
2. What was the start spread (first to last)?
3. What is the verdict — true parallelism, partial, or serialized?
4. Did Cursor show you all 10 sub-agent conversations simultaneously, or did some queue?
5. Any error messages from Task calls that failed to launch?

This is the ground truth. Report all of it exactly.
```

---

## What the report tells us

| Spread | Interpretation |
|--------|---------------|
| < 3 s | All N agents started before the first one finished → **true parallelism**, no ceiling at N |
| 3–6 s | First batch ran, rest queued → ceiling is approximately the first-batch size |
| > 6 s | Serialized → Cursor is running Tasks one-at-a-time here |

## Running a second pass at N=20

If N=10 shows true parallelism, repeat with N=20 to find the actual ceiling.
Copy the coordinator prompt, change `seq -w 1 10` to `seq -w 1 20`, add 10 more
Task calls in Step 2, and change the `DONE -eq 10` to `DONE -eq 20`.

## What to do with the result

Once we have ground truth, update the architecture doc accordingly.
If the ceiling is higher than 4 (which seems likely given you've seen 5+ run),
we can remove the artificial token-bucket throttling and let Cursor's own
scheduler be the constraint.
