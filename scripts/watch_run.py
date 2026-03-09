#!/usr/bin/env -S python3 -u
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
ORANGE = "\033[38;5;208m"

# ── Patterns ───────────────────────────────────────────────────────────────────

_RE_RUN_STEP = re.compile(
    r"log_run_step: issue=(?P<issue>\S+) step='(?P<step>.+?)'"
)
_RE_ITERATION = re.compile(r"Iteration\s+(?P<n>\d+)/(?P<total>\d+)")

# dispatch_tool — run_id tag (agent_loop.py)
_RE_DISPATCH_TOOL = re.compile(
    r"dispatch_tool — run_id=(?P<run_id>\S+) tool=(?P<tool>\S+)"
)

# file_tools result lines (agentception.tools.file_tools)
_RE_READ_FILE = re.compile(
    r"read_file_lines — (?P<path>\S+) lines (?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+)"
)
_RE_REPLACE = re.compile(
    r"replace_in_file — (?P<path>\S+) \((?P<count>\d+) replacement"
)
_RE_INSERT = re.compile(
    r"insert_after_in_file — (?P<path>\S+) \(inserted at byte"
)
_RE_WRITE = re.compile(
    r"write_file — (?P<path>\S+?)(?:\s+\((?P<bytes>\d+) bytes\))?"
)
# shell commands (agent_loop.py run_command lines)
_RE_SHELL_CMD = re.compile(
    r"run_command — '(?P<cmd>.+?)' \(cwd=(?P<cwd>[^)]+)\)"
)
_RE_SHELL_DONE = re.compile(
    r"run_command done — exit=(?P<exit>\d+) stdout=(?P<stdout>\d+) stderr=(?P<stderr>\d+)"
)

# git
_RE_GIT_COMMIT = re.compile(
    r"git_commit_and_push — branch=(?P<branch>\S+)"
)

# GitHub MCP tool calls (github_client.py or mcp client)
_RE_GITHUB_TOOL = re.compile(
    r"github_mcp — tool=(?P<tool>\S+)"
)

# LLM lifecycle
_RE_LLM_CALL = re.compile(
    r"LLM tool-use call — model=(?P<model>\S+) turns=(?P<turns>\d+) tools=(?P<tools>\d+)"
)
_RE_LLM_USAGE = re.compile(
    r"LLM usage — input=(?P<input>\d+) cache_written=(?P<cw>\d+) cache_read=(?P<cr>\d+)"
)
_RE_LLM_DONE = re.compile(
    r"LLM tool-use done — stop_reason=(?P<reason>\S+) content_chars=(?P<chars>\d+) tool_calls=(?P<calls>\d+)"
)
_RE_LLM_REPLY = re.compile(r"LLM reply — chars=(?P<chars>\d+) text=(?P<text>.+)")
_RE_DELAY = re.compile(r"inter-turn delay — sleeping (?P<secs>[\d.]+)s")

# run start / teardown / indexing
_RE_RUN_START = re.compile(r"agent_loop start — run_id=(?P<run_id>\S+) issue=\S+ tools=(?P<tools>\d+)")
_RE_WORKTREE_INDEXED = re.compile(
    r"worktree indexed — run_id=(?P<run_id>\S+) collection=\S+ files=(?P<files>\d+) chunks=(?P<chunks>\d+)"
)
_RE_TEARDOWN = re.compile(r"teardown\[(?P<run_id>[^\]]+)\]: (?P<msg>.+)")
_RE_DISPATCHED = re.compile(
    r"adhoc run dispatched — run_id=(?P<run_id>\S+) role=(?P<role>\S+) arch=(?P<arch>\S+) context_files=(?P<ctx>\d+)"
)

_RE_ERROR = re.compile(r"❌\s+(?P<msg>.+)")
_RE_WARN = re.compile(r"⚠️\s*(?P<msg>.+)")


# ── Mutable state across log lines ────────────────────────────────────────────

class _State:
    current_run_id: str | None = None
    iteration: int = 0
    total: int = 50
    history_len: int = 0


_state = _State()


def _ts() -> str:
    return GREY + datetime.now().strftime("%H:%M:%S") + RESET


def _shorten_path(path: str) -> str:
    """Strip /worktrees/<run_id>/ prefix for readability."""
    path = re.sub(r"^/worktrees/[^/]+/", "", path)
    return path


def _shorten_cmd(cmd: str, max_len: int = 100) -> str:
    cmd = cmd.strip()
    cmd = re.sub(r"docker compose exec \S+ sh -c\s+", "", cmd)
    cmd = re.sub(r"docker compose exec \S+\s+", "", cmd)
    if len(cmd) > max_len:
        cmd = cmd[:max_len] + "…"
    return cmd


def _fmt_number(n: str) -> str:
    return f"{int(n):,}"


def _tool_icon(tool: str) -> str:
    icons: dict[str, str] = {
        "search_codebase": "🔍",
        "read_file_lines": "📄",
        "replace_in_file": "✏️ ",
        "insert_after_in_file": "➕",
        "write_file": "💾",
        "run_command": "🖥️ ",
        "git_commit_and_push": "📦",
        "log_run_step": "📋",
        "list_directory": "📁",
        "create_directory": "📁",
    }
    return icons.get(tool, "🔧")


def process_line(raw: str, run_id_filter: str | None) -> str | None:
    """Parse one log line and return a pretty string, or None to suppress it."""
    m = re.match(
        r"agentception-app\s+\|\s+(?P<level>\S+)\s+(?P<module>\S+)\s+(?P<msg>.+)",
        raw,
    )
    if not m:
        return None

    level = m.group("level")
    module = m.group("module")
    msg = m.group("msg")
    ts = _ts()

    # ── Run dispatched (startup banner) ────────────────────────────────────────
    dm = _RE_DISPATCHED.search(msg)
    if dm:
        rid = dm.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        role = dm.group("role")
        arch = dm.group("arch")
        ctx = dm.group("ctx")
        return (
            f"\n{ts}  {GREEN}{BOLD}🚀 LAUNCHED  {rid}{RESET}\n"
            f"       {GREY}role={role}  arch={arch}  context_files={ctx}{RESET}"
        )

    # ── Agent loop start ───────────────────────────────────────────────────────
    rsm = _RE_RUN_START.search(msg)
    if rsm:
        rid = rsm.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        tools = rsm.group("tools")
        return f"{ts}  {GREY}    loop ready — {tools} tools available{RESET}"

    # ── Worktree index complete ────────────────────────────────────────────────
    wim = _RE_WORKTREE_INDEXED.search(msg)
    if wim:
        rid = wim.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        return (
            f"{ts}  {GREY}    🗂  worktree index ready — "
            f"{wim.group('files')} files / {wim.group('chunks')} chunks{RESET}"
        )

    # ── log_run_step — agent's self-reported progress ─────────────────────────
    sm = _RE_RUN_STEP.search(msg)
    if sm:
        step = sm.group("step")
        im = _RE_ITERATION.search(step)
        if im:
            _state.iteration = int(im.group("n"))
            _state.total = int(im.group("total"))
            # iteration markers are shown in the LLM header, suppress duplicates
            return None
        # Non-iteration step — agent wrote something meaningful
        return f"{ts}  {CYAN}{BOLD}📋 {step}{RESET}"

    # ── dispatch_tool ──────────────────────────────────────────────────────────
    # Tools split into two groups:
    #   A) Tools that emit a separate result line from agentception.tools.*
    #      → suppress dispatch line, render the result line below.
    #   B) Tools with no result line (search_codebase, search_text, GitHub tools,
    #      log_run_step, git_commit_and_push)
    #      → render at dispatch time immediately.
    # Tools whose result line comes from agentception.tools.file_tools —
    # suppress the dispatch line, render only when the result line arrives.
    _RESULT_LINE_TOOLS = frozenset({
        "read_file_lines", "replace_in_file", "insert_after_in_file", "write_file",
    })
    # Tools whose result line comes from a different module or doesn't exist —
    # render at dispatch time only.
    _DISPATCH_ONLY_TOOLS = frozenset({
        "read_file", "list_directory", "create_directory",
    })
    dtm = _RE_DISPATCH_TOOL.search(msg)
    if dtm:
        rid = dtm.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        _state.current_run_id = rid
        tool_name = dtm.group("tool")
        if tool_name in _RESULT_LINE_TOOLS:
            return None  # wait for the file_tools result line
        if tool_name in _DISPATCH_ONLY_TOOLS:
            return f"{ts}  {BLUE}{_tool_icon('read_file_lines')} {tool_name}{RESET}"
        if tool_name in ("search_codebase", "search_text"):
            return f"{ts}  {BLUE}🔍 {tool_name}{RESET}"
        if tool_name in ("log_run_step", "git_commit_and_push", "run_command"):
            return None  # rendered via dedicated patterns below
        # GitHub MCP tools and anything else — show immediately
        if any(gh in tool_name for gh in ("pull_request", "issue_", "create_branch", "list_branch", "get_me", "search_")):
            return f"{ts}  {CYAN}🐙 {tool_name}{RESET}"
        return f"{ts}  {BLUE}{_tool_icon(tool_name)} {tool_name}{RESET}"

    # ── file_tools result lines (rendered independently of dispatch) ───────────

    rfm = _RE_READ_FILE.search(msg)
    if rfm and "file_tools" in module:
        path = _shorten_path(rfm.group("path"))
        start, end, total = rfm.group("start"), rfm.group("end"), rfm.group("total")
        return (
            f"{ts}  {BLUE}{_tool_icon('read_file_lines')} read{RESET}  "
            f"{WHITE}{path}{RESET}  {GREY}lines {start}–{end} / {total}{RESET}"
        )

    rpm = _RE_REPLACE.search(msg)
    if rpm and "file_tools" in module:
        path = _shorten_path(rpm.group("path"))
        count = rpm.group("count")
        return (
            f"{ts}  {GREEN}{_tool_icon('replace_in_file')} replaced{RESET}  "
            f"{WHITE}{path}{RESET}  {GREY}({count} replacement{'s' if count != '1' else ''}){RESET}"
        )

    inm = _RE_INSERT.search(msg)
    if inm and "file_tools" in module:
        path = _shorten_path(inm.group("path"))
        return (
            f"{ts}  {GREEN}{_tool_icon('insert_after_in_file')} inserted{RESET}  "
            f"{WHITE}{path}{RESET}"
        )

    wfm = _RE_WRITE.search(msg)
    if wfm and "file_tools" in module:
        path = _shorten_path(wfm.group("path"))
        byte_tag = f"  {GREY}({wfm.group('bytes')} bytes){RESET}" if wfm.group("bytes") else ""
        return (
            f"{ts}  {GREEN}{_tool_icon('write_file')} wrote{RESET}  "
            f"{WHITE}{path}{RESET}{byte_tag}"
        )

    # ── shell command ──────────────────────────────────────────────────────────
    scmd = _RE_SHELL_CMD.search(msg)
    if scmd:
        cmd = _shorten_cmd(scmd.group("cmd"))
        return f"{ts}  {ORANGE}{_tool_icon('run_command')} ${RESET}  {WHITE}{cmd}{RESET}"

    sdm = _RE_SHELL_DONE.search(msg)
    if sdm:
        exit_code = int(sdm.group("exit"))
        stdout_bytes = _fmt_number(sdm.group("stdout"))
        if exit_code == 0:
            return f"{ts}  {GREEN}   ✅ exit=0{RESET}  {GREY}({stdout_bytes} bytes out){RESET}"
        else:
            return f"{ts}  {RED}   ❌ exit={exit_code}{RESET}  {GREY}({stdout_bytes} bytes out){RESET}"

    # ── git commit/push ────────────────────────────────────────────────────────
    gcm = _RE_GIT_COMMIT.search(msg)
    if gcm:
        branch = gcm.group("branch")
        return f"{ts}  {GREEN}{BOLD}📦 git push → {branch}{RESET}"

    # ── GitHub MCP tool ────────────────────────────────────────────────────────
    ghm = _RE_GITHUB_TOOL.search(msg)
    if ghm:
        return f"{ts}  {CYAN}🐙 github/{ghm.group('tool')}{RESET}"

    # catch-all: any GitHub MCP call visible in logs
    if "github_mcp" in msg or "create_pull_request" in msg or "merge_pull_request" in msg:
        short = msg[:120] + "…" if len(msg) > 120 else msg
        return f"{ts}  {CYAN}🐙 {short}{RESET}"

    # ── LLM turn header ────────────────────────────────────────────────────────
    lm = _RE_LLM_CALL.search(msg)
    if lm:
        _state.history_len = int(lm.group("turns"))
        iteration = _state.iteration
        total = _state.total
        iter_tag = f"{iteration}/{total}" if iteration else "?"
        model = lm.group("model").split("-")[0] + "…"  # truncate long model names
        return (
            f"\n{ts}  {MAGENTA}{BOLD}╔══ ITER {iter_tag}  [{model}]{RESET}"
        )

    # ── LLM token usage ───────────────────────────────────────────────────────
    um = _RE_LLM_USAGE.search(msg)
    if um:
        inp = _fmt_number(um.group("input"))
        cw = _fmt_number(um.group("cw"))
        cr = _fmt_number(um.group("cr"))
        hist = _state.history_len
        cr_int = int(um.group("cr"))
        # Colour cache_read green when it's high (good caching), yellow when low
        cr_col = GREEN if cr_int > 10_000 else YELLOW if cr_int > 0 else GREY
        return (
            f"{ts}  {GREY}    in={inp}  cache_write={cw}  "
            f"{cr_col}cache_read={cr}{RESET}  {GREY}history={hist}msgs{RESET}"
        )

    # ── agent text reply (before tool calls or at end_turn) ────────────────────
    rlm = _RE_LLM_REPLY.search(msg)
    if rlm:
        chars = int(rlm.group("chars"))
        text = rlm.group("text").strip()
        # Truncate long replies for display — full text is in the raw log.
        display = text[:200] + ("…" if len(text) > 200 else "")
        return f"{ts}  {GREY}💬 ({chars:,}ch) {display}{RESET}"

    # ── LLM done / stop reason ─────────────────────────────────────────────────
    ldm = _RE_LLM_DONE.search(msg)
    if ldm:
        reason = ldm.group("reason")
        calls = ldm.group("calls")
        if reason == "end_turn":
            tag = f"{GREEN}end_turn{RESET}"
        elif reason.startswith("tool_calls"):
            n = int(calls)
            tag = f"{CYAN}→ {n} tool call{'s' if n != 1 else ''}{RESET}"
        else:
            tag = f"{YELLOW}{reason}{RESET}"
        return f"{ts}  {MAGENTA}╚══ {tag}{RESET}"

    # ── inter-turn pacing ─────────────────────────────────────────────────────
    dlm = _RE_DELAY.search(msg)
    if dlm:
        secs = float(dlm.group("secs"))
        bar = "▓" * int(secs) + "░" * max(0, 7 - int(secs))
        return f"{ts}  {GREY}⏳ {bar} {secs:.1f}s{RESET}"

    # ── errors ────────────────────────────────────────────────────────────────
    if level == "ERROR":
        em = _RE_ERROR.search(msg)
        text = em.group("msg") if em else msg
        return f"{ts}  {RED}{BOLD}❌ {text}{RESET}"

    # ── warnings that matter ───────────────────────────────────────────────────
    wm = _RE_WARN.search(msg)
    if wm and any(kw in msg for kw in ("429", "stale", "reaper", "SSL", "retry", "circuit")):
        return f"{ts}  {YELLOW}⚠️  {wm.group('msg')}{RESET}"

    # ── teardown ───────────────────────────────────────────────────────────────
    tdm = _RE_TEARDOWN.search(msg)
    if tdm:
        teardown_rid = tdm.group("run_id")
        if run_id_filter and teardown_rid != run_id_filter:
            return None
        tmsg = tdm.group("msg")
        return f"{ts}  {GREY}🧹 teardown[{teardown_rid}]: {tmsg}{RESET}"

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
        bufsize=1,  # line-buffered reads from docker
    )

    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            out = process_line(line, run_id_filter)
            if out is not None:
                print(out, flush=True)
    except KeyboardInterrupt:
        print(f"\n{GREY}stopped.{RESET}", flush=True)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
