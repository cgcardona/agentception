#!/usr/bin/env -S python3 -u
"""watch_run.py — pretty-print agentception agent logs for a specific run.

Usage:
    python scripts/watch_run.py <run_id>
    python scripts/watch_run.py adhoc-348aa0b753d4

Pipes `docker compose logs agentception --follow` and renders only the lines
relevant to <run_id> in a clean, colour-coded terminal format.

If no run_id is given, shows ALL agent activity (useful during dispatch to see
what just started).  Each line is prefixed with a short run-id tag so you can
tell agents apart when multiple are running in parallel.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from collections import defaultdict
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

# Colour rotation for run-id prefixes when watching multiple agents at once.
_RUN_COLOURS = [CYAN, MAGENTA, YELLOW, GREEN, ORANGE, BLUE, WHITE]

# ── Patterns ───────────────────────────────────────────────────────────────────

_RE_RUN_STEP = re.compile(
    r"log_run_step: issue=(?P<issue>\S+) step='(?P<step>.+?)'"
)
_RE_ITERATION = re.compile(r"Step\s+(?P<n>\d+)")

# dispatch_tool — run_id tag (agent_loop.py)
# Optional trailing: key=value pairs (path='…', lines=1-50, pattern='…', directory='…')
_RE_DISPATCH_TOOL = re.compile(
    r"dispatch_tool — run_id=(?P<run_id>\S+) tool=(?P<tool>\S+)(?P<args_suffix>.*)"
)
# Parse key=value from args_suffix; value may be quoted or unquoted.
_RE_DISPATCH_ARG = re.compile(
    r"\s+(\w+)=(?:'([^']*)'|\"([^\"]*)\"|(\S+))"
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
    r"write_file — (?P<path>\S+)(?:\s+\((?P<bytes>\d+) bytes\))?"
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
_RE_LLM_RETRY = re.compile(r"LLM retry (?P<n>\d+)/(?P<max>\d+) after (?P<secs>[\d.]+)s")
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


# ── Per-run state ─────────────────────────────────────────────────────────────
# Each key is a run_id string.  Using a plain class keeps the type clean while
# defaultdict ensures the entry exists the first time any run_id is seen.

class _RunState:
    iteration: int = 0
    history_len: int = 0
    pending_arg: str = ""      # key arg from last dispatch_tool
    colour_idx: int = 0        # index into _RUN_COLOURS for this run's prefix


# Assign colours round-robin as new run_ids are seen.
_run_states: dict[str, _RunState] = {}
_next_colour_idx: list[int] = [0]   # list so inner func can mutate it


def _get_state(run_id: str) -> _RunState:
    if run_id not in _run_states:
        state = _RunState()
        state.colour_idx = _next_colour_idx[0] % len(_RUN_COLOURS)
        _next_colour_idx[0] += 1
        _run_states[run_id] = state
    return _run_states[run_id]


# The last run_id seen in a dispatch_tool line — used by file_tools result
# lines which don't carry a run_id in their message.
_last_run_id: list[str] = [""]


def _ts() -> str:
    return GREY + datetime.now().strftime("%H:%M:%S") + RESET


def _run_tag(run_id: str, run_id_filter: str | None) -> str:
    """Return a coloured short run-id prefix when watching multiple runs."""
    if run_id_filter:
        return ""   # single-run mode — no prefix needed
    st = _get_state(run_id)
    colour = _RUN_COLOURS[st.colour_idx]
    short = run_id.replace("issue-", "#").replace("review-", "rv-")
    return f"{colour}[{short}]{RESET} "


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
        tag = _run_tag(rid, run_id_filter)
        role = dm.group("role")
        arch = dm.group("arch")
        ctx = dm.group("ctx")
        return (
            f"\n{ts}  {tag}{GREEN}{BOLD}🚀 LAUNCHED  {rid}{RESET}\n"
            f"       {GREY}role={role}  arch={arch}  context_files={ctx}{RESET}"
        )

    # ── Agent loop start ───────────────────────────────────────────────────────
    rsm = _RE_RUN_START.search(msg)
    if rsm:
        rid = rsm.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        tools = rsm.group("tools")
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{GREY}    loop ready — {tools} tools available{RESET}"

    # ── Worktree index complete ────────────────────────────────────────────────
    wim = _RE_WORKTREE_INDEXED.search(msg)
    if wim:
        rid = wim.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        tag = _run_tag(rid, run_id_filter)
        return (
            f"{ts}  {tag}{GREY}    🗂  worktree index ready — "
            f"{wim.group('files')} files / {wim.group('chunks')} chunks{RESET}"
        )

    # ── log_run_step — agent's self-reported progress ─────────────────────────
    sm = _RE_RUN_STEP.search(msg)
    if sm:
        # log_run_step logs a bare issue number (e.g. "854") but _last_run_id[0]
        # holds the full run_id (e.g. "issue-854") set by the preceding
        # dispatch_tool line.  Use _last_run_id[0] so both share the same key.
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        step = sm.group("step")
        im = _RE_ITERATION.search(step)
        if im:
            _get_state(rid).iteration = int(im.group("n"))
            return None  # shown in the LLM ITER header
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{CYAN}{BOLD}📋 {step}{RESET}"

    # ── dispatch_tool ──────────────────────────────────────────────────────────
    _RESULT_LINE_TOOLS = frozenset({
        "read_file_lines", "replace_in_file", "insert_after_in_file", "write_file",
    })
    _DISPATCH_ONLY_TOOLS = frozenset({
        "read_file", "list_directory", "create_directory",
    })
    dtm = _RE_DISPATCH_TOOL.search(msg)
    if dtm:
        rid = dtm.group("run_id")
        if run_id_filter and rid != run_id_filter:
            return None
        _last_run_id[0] = rid
        st = _get_state(rid)
        tool_name = dtm.group("tool")
        args_suffix = dtm.group("args_suffix") or ""
        # Parse key=value pairs from args_suffix for rich display.
        parsed: dict[str, str] = {}
        for m in _RE_DISPATCH_ARG.finditer(args_suffix):
            key = m.group(1)
            val = m.group(2) or m.group(3) or m.group(4) or ""
            parsed[key] = val
        tag = _run_tag(rid, run_id_filter)

        if tool_name in _RESULT_LINE_TOOLS:
            st.pending_arg = parsed.get("path", "")
            # read_file_lines: skip here — file_tools logs a result line with path+lines+total;
            # we render that one only to avoid showing the same read twice.
            if tool_name == "read_file_lines":
                return None
            return None
        if tool_name in _DISPATCH_ONLY_TOOLS:
            path = parsed.get("path", "")
            path_short = _shorten_path(path) if path else tool_name
            return f"{ts}  {tag}{BLUE}{_tool_icon('read_file_lines')} read{RESET}  {WHITE}{path_short}{RESET}"
        if tool_name in ("search_codebase", "search_text"):
            pattern = parsed.get("pattern") or parsed.get("query", "")
            directory = parsed.get("directory", "")
            if len(pattern) > 60:
                pattern = pattern[:60] + "…"
            parts = [f"{ts}  {tag}{BLUE}🔍 {tool_name}{RESET}"]
            if pattern:
                parts.append(f"  {GREY}pattern={pattern!r}{RESET}")
            if directory and directory != ".":
                parts.append(f"  {GREY}dir={directory}{RESET}")
            return "".join(parts)
        if tool_name in ("log_run_step", "git_commit_and_push", "run_command"):
            return None  # rendered via dedicated patterns below
        if any(gh in tool_name for gh in ("pull_request", "issue_", "create_branch", "list_branch", "get_me", "search_")):
            arg_val = parsed.get("path") or parsed.get("query", "") or ""
            if len(arg_val) > 90:
                arg_val = arg_val[:90] + "…"
            arg_suffix = f"  {GREY}{arg_val}{RESET}" if arg_val else ""
            return f"{ts}  {tag}{CYAN}🐙 {tool_name}{RESET}{arg_suffix}"
        arg_val = parsed.get("path") or parsed.get("query", "") or ""
        if len(arg_val) > 90:
            arg_val = arg_val[:90] + "…"
        arg_suffix = f"  {GREY}{arg_val}{RESET}" if arg_val else ""
        return f"{ts}  {tag}{BLUE}{_tool_icon(tool_name)} {tool_name}{RESET}{arg_suffix}"

    # ── file_tools result lines ────────────────────────────────────────────────
    # These don't carry run_id — use the last seen run_id from dispatch_tool.

    rfm = _RE_READ_FILE.search(msg)
    if rfm and "file_tools" in module:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        path = _shorten_path(rfm.group("path"))
        start, end, total = rfm.group("start"), rfm.group("end"), rfm.group("total")
        tag = _run_tag(rid, run_id_filter)
        return (
            f"{ts}  {tag}{BLUE}{_tool_icon('read_file_lines')} read{RESET}  "
            f"{WHITE}{path}{RESET}  {GREY}lines {start}–{end} / {total}{RESET}"
        )

    rpm = _RE_REPLACE.search(msg)
    if rpm and "file_tools" in module:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        path = _shorten_path(rpm.group("path"))
        count = rpm.group("count")
        tag = _run_tag(rid, run_id_filter)
        return (
            f"{ts}  {tag}{GREEN}{_tool_icon('replace_in_file')} replaced{RESET}  "
            f"{WHITE}{path}{RESET}  {GREY}({count} replacement{'s' if count != '1' else ''}){RESET}"
        )

    inm = _RE_INSERT.search(msg)
    if inm and "file_tools" in module:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        path = _shorten_path(inm.group("path"))
        tag = _run_tag(rid, run_id_filter)
        return (
            f"{ts}  {tag}{GREEN}{_tool_icon('insert_after_in_file')} inserted{RESET}  "
            f"{WHITE}{path}{RESET}"
        )

    wfm = _RE_WRITE.search(msg)
    if wfm and "file_tools" in module:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        path = _shorten_path(wfm.group("path"))
        byte_tag = f"  {GREY}({wfm.group('bytes')} bytes){RESET}" if wfm.group("bytes") else ""
        tag = _run_tag(rid, run_id_filter)
        return (
            f"{ts}  {tag}{GREEN}{_tool_icon('write_file')} wrote{RESET}  "
            f"{WHITE}{path}{RESET}{byte_tag}"
        )

    # ── shell command ──────────────────────────────────────────────────────────
    scmd = _RE_SHELL_CMD.search(msg)
    if scmd:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        cmd = _shorten_cmd(scmd.group("cmd"))
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{ORANGE}{_tool_icon('run_command')} ${RESET}  {WHITE}{cmd}{RESET}"

    sdm = _RE_SHELL_DONE.search(msg)
    if sdm:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        exit_code = int(sdm.group("exit"))
        stdout_bytes = _fmt_number(sdm.group("stdout"))
        tag = _run_tag(rid, run_id_filter)
        if exit_code == 0:
            return f"{ts}  {tag}{GREEN}   ✅ exit=0{RESET}  {GREY}({stdout_bytes} bytes out){RESET}"
        else:
            return f"{ts}  {tag}{RED}   ❌ exit={exit_code}{RESET}  {GREY}({stdout_bytes} bytes out){RESET}"

    # ── git commit/push ────────────────────────────────────────────────────────
    gcm = _RE_GIT_COMMIT.search(msg)
    if gcm:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        branch = gcm.group("branch")
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{GREEN}{BOLD}📦 git push → {branch}{RESET}"

    # ── GitHub MCP tool ────────────────────────────────────────────────────────
    ghm = _RE_GITHUB_TOOL.search(msg)
    if ghm:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{CYAN}🐙 github/{ghm.group('tool')}{RESET}"

    if "github_mcp" in msg or "create_pull_request" in msg or "merge_pull_request" in msg:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        tag = _run_tag(rid, run_id_filter)
        short = msg[:120] + "…" if len(msg) > 120 else msg
        return f"{ts}  {tag}{CYAN}🐙 {short}{RESET}"

    # ── LLM turn header ────────────────────────────────────────────────────────
    lm = _RE_LLM_CALL.search(msg)
    if lm:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        st = _get_state(rid)
        st.history_len = int(lm.group("turns"))
        iteration = st.iteration
        iter_tag = str(iteration) if iteration else "?"
        short_id = rid.replace("issue-", "#").replace("review-", "rv-")
        tag = _run_tag(rid, run_id_filter)
        return (
            f"\n{ts}  {tag}{MAGENTA}{BOLD}╔══ ITER {iter_tag}  [{short_id}]{RESET}"
        )

    # ── LLM token usage ───────────────────────────────────────────────────────
    um = _RE_LLM_USAGE.search(msg)
    if um:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        st = _get_state(rid)
        inp = _fmt_number(um.group("input"))
        cw = _fmt_number(um.group("cw"))
        cr = _fmt_number(um.group("cr"))
        hist = st.history_len
        cr_int = int(um.group("cr"))
        cr_col = GREEN if cr_int > 10_000 else YELLOW if cr_int > 0 else GREY
        tag = _run_tag(rid, run_id_filter)
        return (
            f"{ts}  {tag}{GREY}    in={inp}  cache_write={cw}  "
            f"{cr_col}cache_read={cr}{RESET}  {GREY}history={hist}msgs{RESET}"
        )

    # ── agent text reply ───────────────────────────────────────────────────────
    rlm = _RE_LLM_REPLY.search(msg)
    if rlm:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        chars = int(rlm.group("chars"))
        text = rlm.group("text").strip()
        tag = _run_tag(rid, run_id_filter)
        prefix = f"{ts}  {tag}{GREY}💬 ({chars:,}ch) "
        indent = " " * (len(ts) + 2 + len(tag.replace(RESET, "").replace(CYAN, "").replace(MAGENTA, "").replace(YELLOW, "").replace(GREEN, "").replace(ORANGE, "").replace(BLUE, "").replace(WHITE, "")) + 4 + len(f"({chars:,}ch) "))
        wrapped = textwrap.wrap(text, width=120, subsequent_indent=indent)
        return (prefix + "\n".join(wrapped) + RESET) if wrapped else ""

    # ── LLM done / stop reason ─────────────────────────────────────────────────
    ldm = _RE_LLM_DONE.search(msg)
    if ldm:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        reason = ldm.group("reason")
        calls = ldm.group("calls")
        if reason == "end_turn":
            tag_str = f"{GREEN}end_turn{RESET}"
        elif reason.startswith("tool_calls"):
            n = int(calls)
            tag_str = f"{CYAN}→ {n} tool call{'s' if n != 1 else ''}{RESET}"
        else:
            tag_str = f"{YELLOW}{reason}{RESET}"
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{MAGENTA}╚══ {tag_str}{RESET}"

    # ── inter-turn pacing ─────────────────────────────────────────────────────
    dlm = _RE_DELAY.search(msg)
    if dlm:
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        secs = float(dlm.group("secs"))
        bar = "▓" * int(secs) + "░" * max(0, 7 - int(secs))
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{GREY}⏳ {bar} {secs:.1f}s{RESET}"

    # ── errors ────────────────────────────────────────────────────────────────
    if level == "ERROR":
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        em = _RE_ERROR.search(msg)
        text = em.group("msg") if em else msg
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{RED}{BOLD}❌ {text}{RESET}"

    # ── warnings that matter ───────────────────────────────────────────────────
    wm = _RE_WARN.search(msg)
    if wm and any(kw in msg for kw in ("429", "stale", "reaper", "SSL", "retry", "circuit")):
        rid = _last_run_id[0]
        if run_id_filter and rid != run_id_filter:
            return None
        tag = _run_tag(rid, run_id_filter)
        return f"{ts}  {tag}{YELLOW}⚠️  {wm.group('msg')}{RESET}"

    # ── teardown ───────────────────────────────────────────────────────────────
    tdm = _RE_TEARDOWN.search(msg)
    if tdm:
        teardown_rid = tdm.group("run_id")
        if run_id_filter and teardown_rid != run_id_filter:
            return None
        tag = _run_tag(teardown_rid, run_id_filter)
        tmsg = tdm.group("msg")
        return f"{ts}  {tag}{GREY}🧹 teardown[{teardown_rid}]: {tmsg}{RESET}"

    return None


import threading
import time as _time_mod

# ── Per-run silence tracker ────────────────────────────────────────────────────
# Tracks the last output timestamp and LLM-call state independently per run_id
# so that one busy agent doesn't mask a silent/stuck agent.

class _SilenceState:
    last_output_ts: float = 0.0
    last_llm_call_ts: float = 0.0
    llm_retry_count: int = 0


_silence_lock = threading.Lock()
_silence: dict[str, _SilenceState] = defaultdict(lambda: _SilenceState())
_stop_heartbeat = threading.Event()


def _heartbeat(run_id_filter: str | None) -> None:
    """Background thread: print a status line when any run goes silent."""
    WARN_AFTER_S = 10
    STUCK_AFTER_S = 120

    while not _stop_heartbeat.wait(timeout=5):
        now = _time_mod.time()
        with _silence_lock:
            snapshot = {rid: (s.last_output_ts, s.last_llm_call_ts, s.llm_retry_count)
                        for rid, s in _silence.items()}

        for rid, (last_out, last_llm, retry_count) in snapshot.items():
            if run_id_filter and rid != run_id_filter:
                continue
            silent_for = now - last_out if last_out else 0.0
            if silent_for < WARN_AFTER_S:
                continue

            ts_str = GREY + _time_mod.strftime("%H:%M:%S") + RESET
            tag = _run_tag(rid, run_id_filter)

            if last_llm and (now - last_llm) > WARN_AFTER_S:
                llm_wait = int(now - last_llm)
                retry_tag = f" (retry {retry_count})" if retry_count else ""
                if silent_for >= STUCK_AFTER_S:
                    label = (
                        f"{RED}{BOLD}⚠️  LLM call in flight{retry_tag} — {llm_wait}s"
                        f" — LIKELY STUCK (container crash?){RESET}"
                    )
                else:
                    label = f"{YELLOW}⏳ waiting for Anthropic response{retry_tag} — {llm_wait}s elapsed{RESET}"
            else:
                if silent_for >= STUCK_AFTER_S:
                    label = f"{RED}{BOLD}⚠️  no agent activity for {int(silent_for)}s — run may be dead{RESET}"
                else:
                    label = f"{GREY}    … no new activity for {int(silent_for)}s{RESET}"

            print(f"{ts_str}  {tag}{label}", flush=True)


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
            f"{GREY}    Each line is prefixed [#issue] or [rv-N] to identify the agent.{RESET}\n"
            f"{GREY}    (Ctrl-C to stop){RESET}\n"
        )

    # Seed the default silence entry so heartbeat fires even before first log.
    with _silence_lock:
        if run_id_filter:
            s = _silence[run_id_filter]
            s.last_output_ts = _time_mod.time()

    hb = threading.Thread(target=_heartbeat, args=(run_id_filter,), daemon=True)
    hb.start()

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
            out = process_line(line, run_id_filter)
            if out is not None:
                now = _time_mod.time()
                rid = _last_run_id[0]
                with _silence_lock:
                    s = _silence[rid]
                    s.last_output_ts = now
                    if _RE_LLM_CALL.search(line):
                        s.last_llm_call_ts = now
                        s.llm_retry_count = 0
                    elif _RE_LLM_DONE.search(line):
                        s.last_llm_call_ts = 0.0
                        s.llm_retry_count = 0
                    elif (mr := _RE_LLM_RETRY.search(line)):
                        s.last_llm_call_ts = now
                        s.llm_retry_count = int(mr.group("n"))
                    # Run failed (LLM error, cancel, etc.) — stop heartbeat "waiting" for this run.
                    elif "log_run_error" in line or "agent_loop LLM error" in line:
                        s.last_llm_call_ts = 0.0
                        s.llm_retry_count = 0
                print(out, flush=True)
    except KeyboardInterrupt:
        print(f"\n{GREY}stopped.{RESET}", flush=True)
    finally:
        _stop_heartbeat.set()
        proc.terminate()


if __name__ == "__main__":
    main()
