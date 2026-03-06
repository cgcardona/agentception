from __future__ import annotations

"""Transcript reader for AgentCeption (AC-003).

Walks ``~/.cursor/projects/.../agent-transcripts/`` to build the parent/child
``AgentNode`` tree. Hierarchy is derived from the ``subagents/`` folder
structure that Cursor creates when an agent spawns sub-agents.

Public API:
    find_transcript_root()        → locate the transcripts directory
    build_agent_tree()            → parse one root UUID into an AgentNode tree
    read_transcript_messages()    → parse a JSONL file into [{role, text}]
    read_transcript_full()        → full detail payload for the detail view
    infer_role_from_messages()    → keyword heuristic → role string
    infer_status_from_messages()  → PR-URL heuristic → AgentStatus
    extract_pr_urls()             → all GitHub PR URLs found in messages
    index_transcripts()           → metadata list for the browser list view
"""

import json
import logging
import re
from pathlib import Path

from agentception.config import settings
from agentception.models import AgentNode, AgentStatus

logger = logging.getLogger(__name__)

# Ordered priority table: first matching keyword wins.
_ROLE_KEYWORDS: list[tuple[str, str]] = [
    ("CTO", "cto"),
    ("Engineering Coordinator", "engineering-coordinator"),
    ("QA Coordinator", "qa-coordinator"),
    ("database-architect", "database-architect"),
    ("python-developer", "python-developer"),
    ("pr-reviewer", "pr-reviewer"),
]

_PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+/pull/\d+)")
_ISSUE_RE = re.compile(r"#(\d+)")


async def find_transcript_root() -> Path | None:
    """Find the agent-transcripts/ directory for the current Cursor project.

    Strategy: derive the expected project-directory name from ``settings.repo_dir``
    by stripping the leading slash and replacing ``/`` with ``-``. If that exact
    directory exists under ``cursor_projects_dir``, return it. Otherwise fall back
    to the most-recently-modified ``agent-transcripts/`` found in any project
    directory — useful when the repo is opened under an unexpected path.

    Returns ``None`` if no transcript directory can be found at all.
    """
    base = settings.cursor_projects_dir
    if not base.exists():
        logger.warning("⚠️  cursor_projects_dir does not exist: %s", base)
        return None

    # Derive expected project dir name from repo_dir (e.g. /home/user/dev/agentception)
    # by stripping the leading "/" and replacing "/" with "-" → home-user-dev-agentception
    repo_slug = str(settings.repo_dir).lstrip("/").replace("/", "-")
    exact = base / repo_slug / "agent-transcripts"
    if exact.exists():
        return exact

    # Fall back to the most recently modified agent-transcripts/ anywhere.
    best_mtime: float = -1.0
    best_path: Path | None = None
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        transcripts = project_dir / "agent-transcripts"
        if not transcripts.exists():
            continue
        mtime = transcripts.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best_path = transcripts

    if best_path is None:
        logger.warning("⚠️  No agent-transcripts directory found under %s", base)
    return best_path


async def read_transcript_messages(jsonl_path: Path) -> list[dict[str, str]]:
    """Parse a Cursor JSONL transcript file into a flat list of ``{role, text}`` dicts.

    Each line is a JSON object with the shape::

        {"role": "user"|"assistant",
         "message": {"content": [{"type": "text", "text": "..."}]}}

    Only ``type == "text"`` content blocks are included. Lines that fail JSON
    parsing are silently skipped to tolerate partial writes. Returns ``[]`` for
    an empty or missing file.
    """
    if not jsonl_path.exists():
        return []

    try:
        raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("⚠️  Cannot read transcript %s: %s", jsonl_path, exc)
        return []

    messages: list[dict[str, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        role: str = entry.get("role", "")
        content_parts: list[dict[str, str]] = (
            entry.get("message", {}).get("content", [])
        )
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                messages.append({"role": role, "text": part.get("text", "")})

    return messages


def infer_role_from_messages(messages: list[dict[str, str]]) -> str:
    """Derive a role string from the first assistant message in the transcript.

    Scans ``_ROLE_KEYWORDS`` in order; returns the role for the first keyword
    found in the text. Returns ``"unknown"`` when no keyword matches.

    The first assistant message is the one most likely to contain a system
    prompt or role-declaration preamble, so only it is examined.
    """
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        text = msg.get("text", "")
        for keyword, role in _ROLE_KEYWORDS:
            if keyword in text:
                return role
        # Only inspect the first assistant message.
        break
    return "unknown"


def infer_status_from_messages(messages: list[dict[str, str]]) -> AgentStatus:
    """Derive lifecycle status from the last assistant message.

    Heuristic: if the most-recent assistant message contains a GitHub pull-request
    URL (``github.com/<owner>/<repo>/pull/<n>``), the agent successfully opened
    a PR and is considered ``DONE``. Any other ending state maps to ``UNKNOWN``.
    """
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            if _PR_URL_RE.search(msg.get("text", "")):
                return AgentStatus.DONE
            break
    return AgentStatus.UNKNOWN


def extract_pr_urls(messages: list[dict[str, str]]) -> list[str]:
    """Return all unique GitHub PR URLs found across all messages, preserving order."""
    seen: set[str] = set()
    urls: list[str] = []
    for msg in messages:
        for match in _PR_URL_RE.finditer(msg.get("text", "")):
            url = f"https://github.com/{match.group(1)}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


async def index_transcripts(
    transcripts_dir: Path,
    limit: int = 400,
) -> list[dict[str, object]]:
    """Scan ``transcripts_dir`` and return a metadata list for all parent conversations.

    Each entry has:
    - ``uuid``           — top-level conversation UUID (directory name)
    - ``message_count`` — total JSONL lines (user + assistant)
    - ``user_msg_count``— lines with role == "user"
    - ``asst_msg_count``— lines with role == "assistant"
    - ``subagent_count``— number of .jsonl files in subagents/ subdirectory
    - ``mtime``         — last-modified time (Unix seconds) of the JSONL file
    - ``preview``       — first 160 chars of the first meaningful user message
    - ``linked_issues`` — list of issue numbers found across all user messages
    - ``pr_urls``       — list of unique GitHub PR URLs found in the transcript
    - ``role``          — inferred role string ("python-developer", "cto", …)
    - ``status``        — inferred ``AgentStatus`` value ("done" or "unknown")
    - ``word_count``    — approximate total word count of all messages
    - ``has_subagents`` — ``True`` when at least one subagent is present
    - ``is_coordinator``— ``True`` when the parent .jsonl is absent (pure coordinator)

    Results are sorted by mtime descending (most recently active first).
    Only parent UUIDs (directories directly under transcripts_dir) are included.
    Directories that contain *only* a subagents/ folder (no root JSONL) are
    still indexed so coordinator sessions are visible in the browser.
    """
    entries: list[dict[str, object]] = []

    for uuid_dir in transcripts_dir.iterdir():
        if not uuid_dir.is_dir():
            continue
        uuid = uuid_dir.name
        parent_jsonl = uuid_dir / f"{uuid}.jsonl"
        subagents_dir = uuid_dir / "subagents"
        subagent_count = (
            sum(1 for _ in subagents_dir.glob("*.jsonl"))
            if subagents_dir.is_dir()
            else 0
        )
        is_coordinator = not parent_jsonl.exists() and subagent_count > 0
        if not parent_jsonl.exists() and not is_coordinator:
            continue

        mtime = (
            parent_jsonl.stat().st_mtime
            if parent_jsonl.exists()
            else uuid_dir.stat().st_mtime
        )
        message_count = 0
        user_msg_count = 0
        asst_msg_count = 0
        word_count = 0
        preview = ""
        linked_issues: list[int] = []
        pr_urls: list[str] = []
        messages: list[dict[str, str]] = []

        if parent_jsonl.exists():
            try:
                raw = parent_jsonl.read_text(encoding="utf-8", errors="replace")
                lines = [ln for ln in raw.splitlines() if ln.strip()]
                message_count = len(lines)

                issue_seen: set[int] = set()
                pr_seen: set[str] = set()

                for line in lines:
                    try:
                        entry = json.loads(line)
                        role = entry.get("role", "")
                        if role == "user":
                            user_msg_count += 1
                        elif role == "assistant":
                            asst_msg_count += 1

                        parts = entry.get("message", {}).get("content", [])
                        for p in parts:
                            if not isinstance(p, dict) or p.get("type") != "text":
                                continue
                            text = p.get("text") or ""
                            word_count += len(text.split())
                            messages.append({"role": role, "text": text})

                            # Harvest issues from all user messages.
                            if role == "user":
                                for m in _ISSUE_RE.finditer(text):
                                    n = int(m.group(1))
                                    if n not in issue_seen:
                                        issue_seen.add(n)
                                        linked_issues.append(n)
                                # Use first substantive user message as preview
                                # (skip short/system lines like "[Image]")
                                clean = text.strip()
                                if not preview and len(clean) > 20:
                                    # Strip XML-like wrapper tags common in task files.
                                    clean = re.sub(r"<[^>]+>", "", clean).strip()
                                    preview = clean[:160]

                            # Harvest PR URLs from all messages.
                            for match in _PR_URL_RE.finditer(text):
                                url = f"https://github.com/{match.group(1)}"
                                if url not in pr_seen:
                                    pr_seen.add(url)
                                    pr_urls.append(url)
                    except (json.JSONDecodeError, AttributeError):
                        continue
            except OSError:
                pass

        role_str = infer_role_from_messages(messages)
        status = infer_status_from_messages(messages)

        entries.append({
            "uuid": uuid,
            "message_count": message_count,
            "user_msg_count": user_msg_count,
            "asst_msg_count": asst_msg_count,
            "subagent_count": subagent_count,
            "mtime": mtime,
            "preview": preview,
            "linked_issues": linked_issues,
            "pr_urls": pr_urls,
            "role": role_str,
            "status": status.value,
            "word_count": word_count,
            "has_subagents": subagent_count > 0,
            "is_coordinator": is_coordinator,
        })

    entries.sort(key=lambda e: float(str(e["mtime"])), reverse=True)
    return entries[:limit]


async def read_transcript_full(
    uuid: str,
    transcripts_dir: Path,
    max_messages: int = 300,
) -> dict[str, object] | None:
    """Load the full detail payload for a single transcript UUID.

    Returns a dict with:
    - ``uuid``            — the conversation UUID
    - ``messages``        — list of ``{role, text}`` dicts (capped at *max_messages*)
    - ``total_messages``  — true count before capping
    - ``role``            — inferred role string
    - ``status``          — inferred status string ("done" | "unknown")
    - ``linked_issues``   — unique issue numbers found in user messages
    - ``pr_urls``         — unique GitHub PR URLs in the whole transcript
    - ``word_count``      — total word count
    - ``user_msg_count``  — count of user messages
    - ``asst_msg_count``  — count of assistant messages
    - ``subagents``       — list of ``{uuid, role, status, message_count, preview}``
    - ``mtime``           — last modified Unix timestamp
    - ``is_coordinator``  — True when no root JSONL exists (pure coordinator)

    Returns ``None`` when the UUID directory does not exist.
    """
    uuid_dir = transcripts_dir / uuid
    if not uuid_dir.exists():
        return None

    parent_jsonl = uuid_dir / f"{uuid}.jsonl"
    subagents_dir = uuid_dir / "subagents"
    is_coordinator = not parent_jsonl.exists()

    mtime = (
        parent_jsonl.stat().st_mtime
        if parent_jsonl.exists()
        else uuid_dir.stat().st_mtime
    )

    all_messages = await read_transcript_messages(parent_jsonl)
    total_messages = len(all_messages)

    # Build per-role counts and linked artefacts from the full message list.
    user_msg_count = sum(1 for m in all_messages if m.get("role") == "user")
    asst_msg_count = sum(1 for m in all_messages if m.get("role") == "assistant")
    word_count = sum(len(m.get("text", "").split()) for m in all_messages)

    issue_seen: set[int] = set()
    pr_seen: set[str] = set()
    linked_issues: list[int] = []
    pr_urls: list[str] = []
    for msg in all_messages:
        text = msg.get("text", "")
        if msg.get("role") == "user":
            for m in _ISSUE_RE.finditer(text):
                n = int(m.group(1))
                if n not in issue_seen:
                    issue_seen.add(n)
                    linked_issues.append(n)
        for match in _PR_URL_RE.finditer(text):
            url = f"https://github.com/{match.group(1)}"
            if url not in pr_seen:
                pr_seen.add(url)
                pr_urls.append(url)

    role_str = infer_role_from_messages(all_messages)
    status = infer_status_from_messages(all_messages)

    # Cap messages for the detail view — show the most recent ones when over limit.
    messages_display = (
        all_messages[-max_messages:]
        if len(all_messages) > max_messages
        else all_messages
    )

    # Build subagent metadata list.
    subagents: list[dict[str, object]] = []
    if subagents_dir.is_dir():
        for child_jsonl in sorted(subagents_dir.glob("*.jsonl")):
            child_uuid = child_jsonl.stem
            child_messages = await read_transcript_messages(child_jsonl)
            child_role = infer_role_from_messages(child_messages)
            child_status = infer_status_from_messages(child_messages)
            child_preview = ""
            for cm in child_messages:
                if cm.get("role") == "user":
                    raw_text = re.sub(r"<[^>]+>", "", cm.get("text", "")).strip()
                    if len(raw_text) > 20:
                        child_preview = raw_text[:120]
                        break
            subagents.append({
                "uuid": child_uuid,
                "role": child_role,
                "status": child_status.value,
                "message_count": len(child_messages),
                "preview": child_preview,
            })

    return {
        "uuid": uuid,
        "messages": messages_display,
        "total_messages": total_messages,
        "role": role_str,
        "status": status.value,
        "linked_issues": linked_issues,
        "pr_urls": pr_urls,
        "word_count": word_count,
        "user_msg_count": user_msg_count,
        "asst_msg_count": asst_msg_count,
        "subagents": subagents,
        "mtime": mtime,
        "is_coordinator": is_coordinator,
        "truncated": total_messages > max_messages,
    }


async def build_agent_tree(
    root_uuid: str,
    transcripts_dir: Path,
) -> AgentNode | None:
    """Recursively build an ``AgentNode`` tree rooted at ``root_uuid``.

    Cursor stores transcripts in one of two layouts:

    * **Leaf agent** (no sub-agents spawned)::

        <transcripts_dir>/<root_uuid>/<root_uuid>.jsonl

    * **Coordinator agent** (spawned sub-agents)::

        <transcripts_dir>/<root_uuid>/subagents/<child-uuid>.jsonl
        ...

      The coordinator may or may not have its own ``<root_uuid>.jsonl``;
      children are always leaf ``.jsonl`` files (the structure does not
      nest deeper in practice).

    Returns ``None`` if the root directory does not exist.
    """
    root_dir = transcripts_dir / root_uuid
    if not root_dir.exists():
        logger.warning("⚠️  Transcript directory not found: %s", root_dir)
        return None

    # Own transcript — may be absent for pure-coordinator agents.
    parent_jsonl = root_dir / f"{root_uuid}.jsonl"
    messages = await read_transcript_messages(parent_jsonl)

    # Use directory mtime as fallback when the JSONL file is absent.
    mtime = (
        parent_jsonl.stat().st_mtime
        if parent_jsonl.exists()
        else root_dir.stat().st_mtime
    )

    role = infer_role_from_messages(messages)
    status = infer_status_from_messages(messages)

    # Build children from subagents/ if present.
    children: list[AgentNode] = []
    subagents_dir = root_dir / "subagents"
    if subagents_dir.is_dir():
        for child_jsonl in sorted(subagents_dir.glob("*.jsonl")):
            child_uuid = child_jsonl.stem
            child_messages = await read_transcript_messages(child_jsonl)
            child_role = infer_role_from_messages(child_messages)
            child_status = infer_status_from_messages(child_messages)
            children.append(
                AgentNode(
                    id=child_uuid,
                    role=child_role,
                    status=child_status,
                    message_count=len(child_messages),
                    last_activity_mtime=child_jsonl.stat().st_mtime,
                    transcript_path=str(child_jsonl),
                )
            )

    return AgentNode(
        id=root_uuid,
        role=role,
        status=status,
        message_count=len(messages),
        last_activity_mtime=mtime,
        transcript_path=str(parent_jsonl) if parent_jsonl.exists() else None,
        children=children,
    )
