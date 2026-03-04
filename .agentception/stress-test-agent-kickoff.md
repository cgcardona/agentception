# Stress Test Sub-Agent Kickoff

> This is the kickoff prompt used by the coordinator in `stress-test-parallelism.md`.
> It is the only prompt each sub-agent receives. It deliberately does nothing useful.

---

You are a stress test sub-agent. Your only purpose is to measure timing. Follow these exact steps and do nothing else. Do not read any application code. Do not run mypy or pytest. Do not open any PRs or issues.

**Step 1 — Record your start time (nanosecond precision):**
```bash
date +%s.%N > .start
echo "STRESS AGENT START: $(cat .start)"
```

**Step 2 — Read your agent ID from your task file:**
```bash
AGENT_ID=$(grep '^id' .agent-task | cut -d'"' -f2)
echo "STRESS AGENT ID: $AGENT_ID"
```

**Step 3 — Sleep exactly 3 seconds:**
```bash
sleep 3
```

**Step 4 — Record your end time:**
```bash
date +%s.%N > .end
echo "STRESS AGENT END: $(cat .end)"
```

**Step 5 — Write the done marker:**
```bash
echo "$AGENT_ID" > .done
echo "STRESS AGENT DONE: $AGENT_ID"
```

**Step 6 — Print your final summary:**
```
STRESS TEST COMPLETE
  agent_id : <your id from .agent-task>
  start    : <contents of .start>
  end      : <contents of .end>
  duration : ~3 seconds
```

That is all. Stop here.
