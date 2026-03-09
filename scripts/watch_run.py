#!/usr/bin/env python3
"""watch_run.py — pretty-print agentception agent logs for a specific run.

Usage:
    python scripts/watch_run.py <run_id>
    python scripts/watch_run.py adhoc-348aa0b753d4

Pipes `docker compose logs agentception --follow` and renders only the lines
relevant to <run_id> in a clean, colour-coded terminal format.

If no run_id is given, shows ALL agent activity (useful during dispatch to see
what just started).
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
WHITE = "\033[97m"
GREY = "\033[90m"

# ── Patterns (order matters — first match wins) ────────────────────────────────
# Each entry: (regex, handler_fn)
# handler_fn(match, run_id_filter) -> str | None  (None = skip line)

_RE_RUN_STEP = re.compile(
    r"log_run_step: issue=(?P<issue>\S+) step='(?P<step>.+?)'"
)
_RE_DISPATCH_TOOL = re.compile(
    r"dispatch_tool — run_id=(?P<run_id>\S+) tool=(?P<tool>\S+)"
)
_RE_SHELL_CMD = re.compile(
    r"run_command — '(?P<cmd>.+?)' \(cwd=(?P<cwd>[^)]+)\)"
)
_RE_SHELL_DONE = re.compile(
    r"run_command done — exit=(?P<exit>\d+) stdout=(?P<stdout>\d+) stderr=(?P<stderr>\d+)"
)
_RE_GIT_COMMIT = re.compile(
    r"git_commit_and_push — run_id=(?P<run_id>\S+)"
)
_RE_LLM_CALL = re.compile(
    r"LLM tool-use call — model=(?P<model>\S+) turns=(?P<turns>\d+) tools=(?P<tools>\d+)"
)
_RE_LLM_USAGE = re.compile(
    r"LLM usage — input=(?P<input>\d+) cache_written=(?P<cw>\d+) cache_read=(?P<cr>\d+)"
)
_RE_LLM_DONE = re.compile(
    r"LLM tool-use done — stop_reason=(?P<reason>\S+) content_chars=(?P<chars>\d+) tool_calls=(?P<calls>\d+)"
)
_RE_DELAY = re.compile(
    r"inter-turn delay — sleeping (?P<secs>[\d.]+)s"
)
_RE_ERROR = re.compile(r"❌\s+(?P<msg>.+)")
_RE_WARN = re.compile(r"⚠️\s*(?P<msg>.+)")
_RE_TEARDOWN = re.compile(r"teardown\[(?P<run_id>[^\]]+)\]: (?P<msg>.+)")
_RE_WORKTREE_CREATED = re.compile(r"worktree.*created.*run_id=(?P<run_id>\S+)")

# State carried across lines (mutable, thread-unsafe — single stream only)
_state: dict[str, object] = {
    "current_run_id": None,  # last seen run_id on dispatch_tool lines
    "turn": 0,
    "pending_tool": None,  # tool name waiting for its command detail
    "pending_cmd": None,  # command string waiting for its result
}


def _ts() -> str:
    return GREY + datetime.now().strftime("%H:%M:%S") + RESET


def _shorten_cmd(cmd: str, max_len: int = 80) -> str:
    cmd = cmd.strip()
    # Strip docker compose exec wrappers for readability
    cmd = re.sub(r"docker compose exec \S+ sh -c\s+", "", cmd)
    cmd = re.sub(r"docker compose exec \S+\s+", "", cmd)
    if len(cmd) > max_len:
        cmd = cmd[:max_len] + "…"
    return cmd


def _fmt_number(n: str) -> str:
    return f"{int(n):,}"


def process_line(raw: str, run_id_filter: str | None) -> str | None:
    """Parse one log line and return a pretty string, or None to suppress it."""
    # Docker log prefix: "agentception-app  | LEVEL  module.path  message"
    m = re.match(
        r"agentception-app\s+\|\s+(?P<level>\S+)\s+(?P<module>\S+)\s+(?P<msg>.+)",
        raw,
    )
    if not m:
        return None

    level = m.group("level")
    msg = m.group("msg")

    ts = _ts()

    # ── log_run_step — agent's self-reported progress ──────────────────────────
    sm = _RE_RUN_STEP.search(msg)
    if sm:
        step = sm.group("step")
        return f"{ts}  {CYAN}{BOLD}📋 {step}{RESET}"

    # ── dispatch_tool — what tool was called ───────────────────────────────────
    dm = _RE_DISPATCH_TOOL.search(msg)
    if dm:
        rid = dm.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        _state["current_run_id"] = rid
        _state["pending_tool"] = dm.group("tool")
        return None  # wait for the detail line

    # ── run_command — the actual shell command ─────────────────────────────────
    scm = _RE_SHELL_CMD.search(msg)
    if scm:
        tool = _state.get("pending_tool") or "run_command"
        cmd = _shorten_cmd(scm.group("cmd"))
        _state["pending_cmd"] = scm.group("cmd")
        _state["pending_tool"] = None
        return f"{ts}  {BLUE}🔧 {tool}{RESET}  {WHITE}{cmd}{RESET}"

    # ── run_command done — exit code ───────────────────────────────────────────
    sdm = _RE_SHELL_DONE.search(msg)
    if sdm:
        exit_code = int(sdm.group("exit"))
        stdout_bytes = _fmt_number(sdm.group("stdout"))
        if exit_code == 0:
            return f"{ts}  {GREEN}   ✅ exit=0{RESET}  {GREY}({stdout_bytes} bytes){RESET}"
        else:
            return f"{ts}  {RED}   ❌ exit={exit_code}{RESET}  {GREY}({stdout_bytes} bytes){RESET}"

    # ── LLM turn start ─────────────────────────────────────────────────────────
    lm = _RE_LLM_CALL.search(msg)
    if lm:
        _state["turn"] = int(lm.group("turns"))
        turn = lm.group("turns")
        return (
            f"\n{ts}  {MAGENTA}{BOLD}╔══ TURN {turn} ══════════════════════════════{RESET}"
        )

    # ── LLM usage ─────────────────────────────────────────────────────────────
    um = _RE_LLM_USAGE.search(msg)
    if um:
        inp = _fmt_number(um.group("input"))
        cw = _fmt_number(um.group("cw"))
        cr = _fmt_number(um.group("cr"))
        return (
            f"{ts}  {GREY}    tokens  in={inp}  cache_write={cw}  cache_read={cr}{RESET}"
        )

    # ── LLM done ──────────────────────────────────────────────────────────────
    ldm = _RE_LLM_DONE.search(msg)
    if ldm:
        reason = ldm.group("reason")
        calls = ldm.group("calls")
        chars = _fmt_number(ldm.group("chars"))
        if reason == "end_turn":
            tag = f"{GREEN}end_turn{RESET}"
        elif reason == "tool_calls":
            tag = f"{CYAN}tool_calls×{calls}{RESET}"
        else:
            tag = f"{YELLOW}{reason}{RESET}"
        return f"{ts}  {MAGENTA}╚══ stop={tag}  output={chars}ch{RESET}"

    # ── inter-turn delay ───────────────────────────────────────────────────────
    dlm = _RE_DELAY.search(msg)
    if dlm:
        secs = dlm.group("secs")
        return f"{ts}  {GREY}⏳ pacing {secs}s{RESET}"

    # ── errors ────────────────────────────────────────────────────────────────
    if level == "ERROR":
        em = _RE_ERROR.search(msg)
        text = em.group("msg") if em else msg
        return f"{ts}  {RED}{BOLD}❌ {text}{RESET}"

    # ── warnings that matter ───────────────────────────────────────────────────
    wm = _RE_WARN.search(msg)
    if wm and ("429" in msg or "stale" in msg.lower() or "reaper" in msg.lower()):
        return f"{ts}  {YELLOW}⚠️  {wm.group('msg')}{RESET}"

    # ── teardown ───────────────────────────────────────────────────────────────
    tdm = _RE_TEARDOWN.search(msg)
    if tdm:
        rid = tdm.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        tmsg = tdm.group("msg")
        return f"{ts}  {GREY}🧹 teardown[{rid}]: {tmsg}{RESET}"

    # Suppress everything else
    return None


def main() -> None:
    run_id_filter: str | None = sys.argv[1] if len(sys.argv) > 1 else None

    if run_id_filter:
        print(
            f"\n{BOLD}{CYAN}👁  Watching run: {run_id_filter}{RESET}\n"
            f"{GREY}    (Ctrl-C to stop){RESET}\n"
        )
    else:
        print(
            f"\n{BOLD}{CYAN}👁  Watching ALL agent activity{RESET}\n"
            f"{GREY}    Pass a run_id to filter: python scripts/watch_run.py <run_id>{RESET}\n"
            f"{GREY}    (Ctrl-C to stop){RESET}\n"
        )

    proc = subprocess.Popen(
        ["docker", "compose", "logs", "agentception", "--follow"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            # docker compose logs emits lines like:
            #   "agentception-app  | INFO  module  message"
            # Pass them straight through.
            out = process_line(line, run_id_filter)
            if out is not None:
                print(out)
    except KeyboardInterrupt:
        print(f"\n{GREY}stopped.{RESET}")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
