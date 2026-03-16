from __future__ import annotations

"""Activity event persist helper and typed payload shapes.

This module defines:
- One ``TypedDict`` per activity subtype (15 total).
- ``ACTIVITY_SUBTYPES`` — the canonical set of valid subtype strings.
- ``persist_activity_event`` — synchronous helper that writes one
  ``ACAgentEvent`` row with ``event_type="activity"`` to an open
  SQLAlchemy session.

Design contract
---------------
``persist_activity_event`` is intentionally *synchronous* and takes an
already-open ``Session`` (or ``AsyncSession`` used in sync-flush mode).
Callers that hold an async session must call this inside their existing
transaction and ``await session.flush()`` / ``await session.commit()``
themselves.  This keeps the helper free of I/O and trivially testable
with an in-memory SQLite session.

Payload contract
----------------
Every row written has ``event_type="activity"`` and a JSON payload that
always contains ``"subtype": <subtype_string>`` plus the subtype-specific
fields documented in ``docs/reference/activity-events.md``.
"""

import datetime
import json
import logging
from collections.abc import Mapping
from typing import NotRequired, TypedDict, Union

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from agentception.db.models import ACAgentEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical subtype registry
# ---------------------------------------------------------------------------

#: All valid activity subtype strings.  Every entry must have a corresponding
#: TypedDict defined below and exported from this module.
ACTIVITY_SUBTYPES: frozenset[str] = frozenset(
    {
        "tool_invoked",
        "llm_iter",
        "llm_usage",
        "llm_reply",
        "llm_done",
        "shell_start",
        "shell_done",
        "file_read",
        "file_replaced",
        "file_inserted",
        "file_written",
        "git_push",
        "github_tool",
        "dir_listed",
        "search_results",
        "delay",
        "error",
    }
)

# ---------------------------------------------------------------------------
# Payload TypedDicts — one per subtype, no Any
# ---------------------------------------------------------------------------


class ToolInvokedPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``tool_invoked`` activity events.

    Emitted when the agent loop dispatches a tool call to the tool executor.
    ``arg_preview`` is truncated to ≤120 chars before storage.
    """

    tool_name: str
    arg_preview: str  # ≤120 chars


class LlmIterPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``llm_iter`` activity events.

    Emitted at the start of each LLM iteration (one call to the model).
    """

    iteration: int
    model: str
    turns: int


class LlmUsagePayload(dict[str, str | int | float | bool | None]):
    """Payload for ``llm_usage`` activity events.

    Emitted after each LLM response with token-level billing data.
    """

    input_tokens: int
    cache_write: int
    cache_read: int


class LlmReplyPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``llm_reply`` activity events.

    Emitted when the model returns a text reply (non-tool-call content block).
    ``text_preview`` is truncated to ≤200 chars before storage.
    """

    chars: int
    text_preview: str  # ≤200 chars


class LlmDonePayload(dict[str, str | int | float | bool | None]):
    """Payload for ``llm_done`` activity events.

    Emitted when the model signals it has finished (stop_reason received).
    """

    stop_reason: str
    tool_call_count: int


class ShellStartPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``shell_start`` activity events.

    Emitted immediately before a shell command is executed.
    ``cmd_preview`` is truncated to ≤200 chars before storage.
    """

    cmd_preview: str  # ≤200 chars
    cwd: str


class ShellDonePayload(dict[str, str | int | float | bool | None]):
    """Payload for ``shell_done`` activity events.

    Emitted after a shell command exits (success or failure).
    """

    exit_code: int
    stdout_bytes: int
    stderr_bytes: int


class FileReadPayload(TypedDict):
    """Payload for ``file_read`` activity events.

    Emitted when the agent reads a file or a line range from a file.
    ``content_preview`` is a short excerpt (max 10 lines / 400 chars) of the
    content that was actually read, shown in the inspector detail panel.
    """

    path: str
    start_line: int
    end_line: int
    total_lines: int
    content_preview: NotRequired[str]


class DirListedPayload(TypedDict):
    """Payload for ``dir_listed`` activity events.

    Emitted after a successful ``list_directory`` tool call.
    ``entries`` is a newline-delimited string of file/directory names;
    directories carry a trailing ``/``.  ``entry_count`` is a convenience
    field for the summary row so the frontend need not split to count.
    """

    path: str
    entry_count: int
    entries: str  # newline-delimited; split on "\n" to get individual names


class SearchResultsPayload(TypedDict):
    """Payload for ``search_results`` activity events.

    Emitted after a successful ``search_codebase`` or ``search_text`` call.
    ``files`` is a newline-delimited string of unique relative file paths that
    contained at least one match.  ``result_count`` is the number of unique
    files — a convenience field so the frontend need not split to count.
    """

    result_count: int
    files: str  # newline-delimited; split on "\\n" to get individual paths


class FileReplacedPayload(TypedDict):
    """Payload for ``file_replaced`` activity events.

    Emitted after a ``replace_in_file`` / ``str_replace`` operation completes.
    """

    path: str
    replacement_count: int


class FileInsertedPayload(TypedDict):
    """Payload for ``file_inserted`` activity events.

    Emitted after an ``insert_after_in_file`` operation completes.
    """

    path: str


class FileWrittenPayload(TypedDict):
    """Payload for ``file_written`` activity events.

    Emitted after a full ``write_file`` operation completes.
    """

    path: str
    byte_count: int


class GitPushPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``git_push`` activity events.

    Emitted after a successful ``git push`` to the remote.
    """

    branch: str


class GithubToolPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``github_tool`` activity events.

    Emitted when the agent calls a GitHub MCP tool (e.g. ``create_pull_request``).
    ``arg_preview`` is truncated to ≤120 chars before storage.
    """

    tool_name: str
    arg_preview: str  # ≤120 chars


class DelayPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``delay`` activity events.

    Emitted when the agent deliberately sleeps (e.g. rate-limit back-off).
    """

    secs: float


class ErrorPayload(dict[str, str | int | float | bool | None]):
    """Payload for ``error`` activity events.

    Emitted when a recoverable error is caught and logged by the agent loop.
    """

    message: str
    context: str


# ---------------------------------------------------------------------------
# Subtype → TypedDict name mapping (used by test_payload_typeddict_completeness)
# ---------------------------------------------------------------------------

#: Maps each subtype string to the name of its TypedDict class defined above.
#: Used by tests to assert completeness without importing every class individually.
SUBTYPE_TYPEDDICT_NAMES: dict[str, str] = {
    "tool_invoked": "ToolInvokedPayload",
    "llm_iter": "LlmIterPayload",
    "llm_usage": "LlmUsagePayload",
    "llm_reply": "LlmReplyPayload",
    "llm_done": "LlmDonePayload",
    "shell_start": "ShellStartPayload",
    "shell_done": "ShellDonePayload",
    "file_read": "FileReadPayload",
    "file_replaced": "FileReplacedPayload",
    "file_inserted": "FileInsertedPayload",
    "file_written": "FileWrittenPayload",
    "git_push": "GitPushPayload",
    "github_tool": "GithubToolPayload",
    "dir_listed": "DirListedPayload",
    "search_results": "SearchResultsPayload",
    "delay": "DelayPayload",
    "error": "ErrorPayload",
}

# ---------------------------------------------------------------------------
# Persist helper
# ---------------------------------------------------------------------------


def persist_activity_event(
    session: Union[Session, AsyncSession],
    run_id: str,
    subtype: str,
    payload: Mapping[str, str | int | float | bool | None],
) -> None:
    """Write one ``ACAgentEvent`` row with ``event_type="activity"``.

    The stored payload is ``payload | {"subtype": subtype}`` so every row
    is self-describing — consumers never need to join back to a subtype
    registry to understand what they are reading.

    Args:
        session: An open SQLAlchemy ``Session`` or ``AsyncSession``.  The
            caller is responsible for flushing/committing after this call
            (for AsyncSession: ``await session.flush()``).
        run_id: The ``ACAgentRun.id`` this event belongs to.
        subtype: One of the strings in ``ACTIVITY_SUBTYPES``.
        payload: Subtype-specific fields (see TypedDicts above).  Must be
            JSON-serialisable.  ``"subtype"`` is injected automatically —
            do not include it in the caller's dict.

    Raises:
        ValueError: When ``subtype`` is not in ``ACTIVITY_SUBTYPES``.
    """
    if subtype not in ACTIVITY_SUBTYPES:
        raise ValueError(
            f"Unknown activity subtype {subtype!r}. "
            f"Valid subtypes: {sorted(ACTIVITY_SUBTYPES)}"
        )

    full_payload: dict[str, str | int | float | bool | None] = {**payload, "subtype": subtype}

    session.add(
        ACAgentEvent(
            agent_run_id=run_id,
            issue_number=None,
            event_type="activity",
            payload=json.dumps(full_payload),
            recorded_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    logger.debug(
        "activity_event run_id=%s subtype=%s",
        run_id,
        subtype,
    )
