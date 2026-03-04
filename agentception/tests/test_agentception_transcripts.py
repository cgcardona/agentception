"""Tests for agentception/readers/transcripts.py (AC-003).

Covers JSONL parsing, role/status heuristics, and AgentNode tree construction.
All tests use temporary directories — no dependency on live ~/.cursor/projects/.

Run targeted:
    pytest agentception/tests/test_agentception_transcripts.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentception.models import AgentStatus
from agentception.readers.transcripts import (
    build_agent_tree,
    infer_role_from_messages,
    infer_status_from_messages,
    read_transcript_messages,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_entry(role: str, text: str) -> str:
    """Serialise one JSONL transcript line."""
    return json.dumps(
        {
            "role": role,
            "message": {
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def _write_jsonl(path: Path, entries: list[tuple[str, str]]) -> None:
    """Write a list of (role, text) pairs to a JSONL file."""
    path.write_text(
        "\n".join(_make_entry(role, text) for role, text in entries),
        encoding="utf-8",
    )


# ── read_transcript_messages ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_read_transcript_messages_valid_jsonl(tmp_path: Path) -> None:
    """Valid JSONL with two entries is parsed into two {role, text} dicts."""
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(
        jsonl,
        [
            ("user", "Hello, agent!"),
            ("assistant", "I am your assistant."),
        ],
    )

    messages = await read_transcript_messages(jsonl)

    assert len(messages) == 2
    assert messages[0] == {"role": "user", "text": "Hello, agent!"}
    assert messages[1] == {"role": "assistant", "text": "I am your assistant."}


@pytest.mark.anyio
async def test_read_transcript_messages_empty_file(tmp_path: Path) -> None:
    """An empty JSONL file returns an empty list without raising."""
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")

    messages = await read_transcript_messages(jsonl)

    assert messages == []


@pytest.mark.anyio
async def test_read_transcript_messages_missing_file(tmp_path: Path) -> None:
    """A path that does not exist returns [] without raising."""
    messages = await read_transcript_messages(tmp_path / "nonexistent.jsonl")
    assert messages == []


@pytest.mark.anyio
async def test_read_transcript_messages_skips_malformed_lines(tmp_path: Path) -> None:
    """Malformed JSON lines are silently skipped; valid lines are returned."""
    jsonl = tmp_path / "mixed.jsonl"
    jsonl.write_text(
        "\n".join(
            [
                _make_entry("user", "good line"),
                "{this is not json",
                _make_entry("assistant", "another good line"),
            ]
        ),
        encoding="utf-8",
    )

    messages = await read_transcript_messages(jsonl)

    assert len(messages) == 2
    assert messages[0]["text"] == "good line"
    assert messages[1]["text"] == "another good line"


@pytest.mark.anyio
async def test_read_transcript_messages_non_text_content_ignored(
    tmp_path: Path,
) -> None:
    """Content blocks with type != 'text' are excluded from the result."""
    jsonl = tmp_path / "tool.jsonl"
    entry = {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Shell", "input": {}},
                {"type": "text", "text": "Done."},
            ]
        },
    }
    jsonl.write_text(json.dumps(entry), encoding="utf-8")

    messages = await read_transcript_messages(jsonl)

    assert len(messages) == 1
    assert messages[0] == {"role": "assistant", "text": "Done."}


# ── infer_role_from_messages ──────────────────────────────────────────────────


def test_infer_role_cto() -> None:
    """An assistant message containing 'CTO' maps to role 'cto'."""
    messages = [
        {"role": "user", "text": "What is your role?"},
        {"role": "assistant", "text": "I am operating as CTO for this session."},
    ]
    assert infer_role_from_messages(messages) == "cto"


def test_infer_role_python_developer() -> None:
    """An assistant message containing 'python-developer' maps to that role."""
    messages = [
        {
            "role": "assistant",
            "text": "You are a python-developer on the Maestro project.",
        },
    ]
    assert infer_role_from_messages(messages) == "python-developer"


def test_infer_role_pr_reviewer() -> None:
    """An assistant message containing 'pr-reviewer' maps to that role."""
    messages = [
        {"role": "assistant", "text": "Operating as pr-reviewer for PR #42."},
    ]
    assert infer_role_from_messages(messages) == "pr-reviewer"


def test_infer_role_muse_specialist() -> None:
    """An assistant message containing 'muse-specialist' maps to that role."""
    messages = [
        {"role": "assistant", "text": "You are a muse-specialist agent."},
    ]
    assert infer_role_from_messages(messages) == "muse-specialist"


def test_infer_role_unknown_when_no_keywords() -> None:
    """A message with no known keywords returns 'unknown'."""
    messages = [
        {"role": "assistant", "text": "Let me help you with that."},
    ]
    assert infer_role_from_messages(messages) == "unknown"


def test_infer_role_only_checks_first_assistant_message() -> None:
    """Only the first assistant message is examined; later ones are ignored."""
    messages = [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "General assistant here."},
        {"role": "assistant", "text": "Now acting as CTO."},
    ]
    # First assistant message has no keyword → unknown, even though second has CTO.
    assert infer_role_from_messages(messages) == "unknown"


def test_infer_role_empty_messages() -> None:
    """Empty message list returns 'unknown' without raising."""
    assert infer_role_from_messages([]) == "unknown"


# ── infer_status_from_messages ────────────────────────────────────────────────


def test_infer_status_done_when_last_assistant_has_pr_url() -> None:
    """Last assistant message containing a GitHub PR URL → DONE."""
    messages = [
        {"role": "user", "text": "Did you open a PR?"},
        {
            "role": "assistant",
            "text": "Yes! https://github.com/cgcardona/maestro/pull/42",
        },
    ]
    assert infer_status_from_messages(messages) == AgentStatus.DONE


def test_infer_status_unknown_without_pr_url() -> None:
    """Last assistant message with no PR URL → UNKNOWN."""
    messages = [
        {"role": "user", "text": "Status?"},
        {"role": "assistant", "text": "Still implementing."},
    ]
    assert infer_status_from_messages(messages) == AgentStatus.UNKNOWN


def test_infer_status_unknown_empty_messages() -> None:
    """Empty message list returns UNKNOWN without raising."""
    assert infer_status_from_messages([]) == AgentStatus.UNKNOWN


def test_infer_status_uses_last_assistant_message() -> None:
    """Only the last assistant message matters; earlier PR URLs are ignored."""
    messages = [
        {
            "role": "assistant",
            "text": "Opened https://github.com/cgcardona/maestro/pull/10",
        },
        {"role": "user", "text": "That was reverted. Can you redo it?"},
        {"role": "assistant", "text": "Re-implementing now."},
    ]
    assert infer_status_from_messages(messages) == AgentStatus.UNKNOWN


# ── build_agent_tree ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_build_agent_tree_missing_directory(tmp_path: Path) -> None:
    """A root_uuid whose directory doesn't exist returns None."""
    result = await build_agent_tree("nonexistent-uuid", tmp_path)
    assert result is None


@pytest.mark.anyio
async def test_build_agent_tree_single_node(tmp_path: Path) -> None:
    """Single JSONL with no subagents/ produces a leaf AgentNode."""
    uuid = "aaaaaaaa-0000-0000-0000-000000000001"
    node_dir = tmp_path / uuid
    node_dir.mkdir()
    _write_jsonl(
        node_dir / f"{uuid}.jsonl",
        [
            ("user", "Implement issue #42"),
            ("assistant", "python-developer here. Starting implementation."),
        ],
    )

    node = await build_agent_tree(uuid, tmp_path)

    assert node is not None
    assert node.id == uuid
    assert node.role == "python-developer"
    assert node.message_count == 2
    assert node.children == []
    assert node.transcript_path is not None


@pytest.mark.anyio
async def test_build_agent_tree_parent_child(tmp_path: Path) -> None:
    """Parent UUID with one subagent produces a tree of depth 2."""
    parent_uuid = "bbbbbbbb-0000-0000-0000-000000000001"
    child_uuid = "cccccccc-0000-0000-0000-000000000001"

    parent_dir = tmp_path / parent_uuid
    subagents_dir = parent_dir / "subagents"
    subagents_dir.mkdir(parents=True)

    # Parent has no own JSONL (coordinator pattern).
    _write_jsonl(
        subagents_dir / f"{child_uuid}.jsonl",
        [
            ("user", "Review PR #99"),
            (
                "assistant",
                "Operating as pr-reviewer. "
                "PR merged: https://github.com/cgcardona/maestro/pull/99",
            ),
        ],
    )

    node = await build_agent_tree(parent_uuid, tmp_path)

    assert node is not None
    assert node.id == parent_uuid
    assert len(node.children) == 1

    child = node.children[0]
    assert child.id == child_uuid
    assert child.role == "pr-reviewer"
    assert child.status == AgentStatus.DONE
    assert child.message_count == 2


@pytest.mark.anyio
async def test_build_agent_tree_coordinator_no_own_jsonl(tmp_path: Path) -> None:
    """Coordinator without own JSONL still returns a valid parent AgentNode."""
    uuid = "dddddddd-0000-0000-0000-000000000001"
    node_dir = tmp_path / uuid
    sub_dir = node_dir / "subagents"
    sub_dir.mkdir(parents=True)

    child_uuid = "eeeeeeee-0000-0000-0000-000000000001"
    _write_jsonl(
        sub_dir / f"{child_uuid}.jsonl",
        [("assistant", "I am a python-developer agent.")],
    )

    node = await build_agent_tree(uuid, tmp_path)

    assert node is not None
    assert node.id == uuid
    # No own JSONL → role falls back to "unknown", status to UNKNOWN.
    assert node.role == "unknown"
    assert node.status == AgentStatus.UNKNOWN
    assert node.message_count == 0
    assert node.transcript_path is None
    assert len(node.children) == 1


@pytest.mark.anyio
async def test_build_agent_tree_multiple_children_sorted(tmp_path: Path) -> None:
    """Multiple subagents are returned sorted by filename (deterministic order)."""
    parent_uuid = "ffffffff-0000-0000-0000-000000000001"
    sub_dir = tmp_path / parent_uuid / "subagents"
    sub_dir.mkdir(parents=True)

    child_uuids = [
        "cccccccc-0000-0000-0000-000000000003",
        "aaaaaaaa-0000-0000-0000-000000000003",
        "bbbbbbbb-0000-0000-0000-000000000003",
    ]
    for cu in child_uuids:
        _write_jsonl(sub_dir / f"{cu}.jsonl", [("assistant", f"Agent {cu}")])

    node = await build_agent_tree(parent_uuid, tmp_path)

    assert node is not None
    assert len(node.children) == 3
    # Should be alphabetically sorted by child UUID.
    actual_ids = [c.id for c in node.children]
    assert actual_ids == sorted(child_uuids)
