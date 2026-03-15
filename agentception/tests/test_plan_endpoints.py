from __future__ import annotations

"""Tests for the Plan API SSE endpoints — Phase 1A preview and Phase 1B file-issues.

Covers:
- POST /api/plan/validate — schema validation against PlanSpec
- POST /api/plan/preview  — Step 1.A SSE stream (empty, missing key, chunk+done, prose error)
- POST /api/plan/file-issues — Step 1.B SSE stream (empty YAML, invalid YAML, stream forwarding)

All LLM calls and gh CLI subprocess calls are mocked so no network or process I/O occurs.

Run targeted:
    pytest agentception/tests/test_plan_endpoints.py -v
"""

import json
import textwrap
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentception.app import app
from agentception.models import PlanSpec
from agentception.services.llm import LLMChunk
from agentception.types import JsonValue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Async httpx client wrapping the FastAPI app for SSE endpoint tests."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse_events(body: str) -> list[dict[str, JsonValue]]:
    """Parse a raw SSE response body into a list of decoded JSON event dicts."""
    events: list[dict[str, JsonValue]] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            events.append(json.loads(payload))
    return events


_MINIMAL_VALID_YAML = textwrap.dedent("""\
    initiative: auth-rewrite
    phases:
      - label: 0-foundation
        description: "Add the User model and Alembic migration"
        depends_on: []
        issues:
          - id: auth-rewrite-p0-001
            title: "Add SQLAlchemy User model"
            body: "## Context\\nAdd a User model with id, email, hashed_password."
""")

_TWO_PHASE_YAML = textwrap.dedent("""\
    initiative: big-project
    phases:
      - label: 0-foundation
        description: "Foundation"
        depends_on: []
        issues:
          - id: big-project-p0-001
            title: "First issue"
            body: "## Context\\nDo it."
          - id: big-project-p0-002
            title: "Second issue"
            body: "## Context\\nDo more."
      - label: 1-api
        description: "API layer"
        depends_on: ["0-foundation"]
        issues:
          - id: big-project-p1-001
            title: "Third issue"
            body: "## Context\\nAnd more."
""")


# ---------------------------------------------------------------------------
# POST /api/plan/validate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_validate_empty_yaml_returns_valid_false(async_client: AsyncClient) -> None:
    """Empty yaml_text → valid=False, detail contains 'empty'."""
    resp = await async_client.post("/api/plan/validate", json={"yaml_text": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert "empty" in body["detail"].lower()


@pytest.mark.anyio
async def test_validate_whitespace_only_returns_valid_false(async_client: AsyncClient) -> None:
    """Whitespace-only yaml_text → valid=False."""
    resp = await async_client.post("/api/plan/validate", json={"yaml_text": "   \n\n  "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False


@pytest.mark.anyio
async def test_validate_malformed_yaml_returns_valid_false(async_client: AsyncClient) -> None:
    """Syntactically malformed YAML → valid=False with a non-empty detail."""
    resp = await async_client.post("/api/plan/validate", json={"yaml_text": ": invalid: [yaml"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["detail"]


@pytest.mark.anyio
async def test_validate_wrong_schema_returns_valid_false(async_client: AsyncClient) -> None:
    """Valid YAML that doesn't conform to PlanSpec → valid=False with detail."""
    resp = await async_client.post("/api/plan/validate", json={"yaml_text": "key: value\n"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["detail"]


@pytest.mark.anyio
async def test_validate_single_phase_spec_returns_correct_counts(
    async_client: AsyncClient,
) -> None:
    """Correct single-phase PlanSpec → valid=True with initiative, phase_count, issue_count."""
    resp = await async_client.post("/api/plan/validate", json={"yaml_text": _MINIMAL_VALID_YAML})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["initiative"] == "auth-rewrite"
    assert body["phase_count"] == 1
    assert body["issue_count"] == 1
    assert body["detail"] == ""


@pytest.mark.anyio
async def test_validate_two_phase_spec_returns_correct_counts(
    async_client: AsyncClient,
) -> None:
    """Two-phase, three-issue PlanSpec → valid=True with phase_count=2, issue_count=3."""
    resp = await async_client.post("/api/plan/validate", json={"yaml_text": _TWO_PHASE_YAML})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["initiative"] == "big-project"
    assert body["phase_count"] == 2
    assert body["issue_count"] == 3


# ---------------------------------------------------------------------------
# POST /api/plan/preview
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_preview_empty_dump_returns_422(async_client: AsyncClient) -> None:
    """Empty dump → HTTP 422 before the stream even starts."""
    resp = await async_client.post("/api/plan/preview", json={"dump": "", "label_prefix": ""})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_preview_whitespace_dump_returns_422(async_client: AsyncClient) -> None:
    """Whitespace-only dump → HTTP 422."""
    resp = await async_client.post(
        "/api/plan/preview", json={"dump": "   \n  ", "label_prefix": ""}
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_preview_missing_api_key_returns_503(async_client: AsyncClient) -> None:
    """When provider is anthropic and ANTHROPIC_API_KEY is absent, the endpoint returns HTTP 503."""
    from agentception.config import LLMProviderChoice, settings

    settings_anthropic_no_key = settings.model_copy(
        update={"anthropic_api_key": "", "use_local_llm": False, "llm_provider": LLMProviderChoice.anthropic}
    )
    with patch("agentception.config.settings", settings_anthropic_no_key):
        resp = await async_client.post(
            "/api/plan/preview", json={"dump": "do some things", "label_prefix": ""}
        )
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_preview_valid_input_streams_chunk_and_done_events(
    async_client: AsyncClient,
) -> None:
    """A valid LLM response streams chunk events then a done event with PlanSpec metadata."""

    async def fake_llm_stream(
        *_args: str, **_kwargs: str | int | bool | float | None
    ) -> AsyncGenerator[LLMChunk, None]:
        yield LLMChunk(type="content", text="initiative: auth-rewrite\n")
        yield LLMChunk(type="content", text=_MINIMAL_VALID_YAML[len("initiative: auth-rewrite\n"):])

    async def return_spec(spec: PlanSpec) -> PlanSpec:
        return spec

    with (
        patch(
            "agentception.routes.ui.plan_ui.completion_stream",
            side_effect=fake_llm_stream,
        ),
        patch(
            "agentception.readers.context_pack.build_context_pack",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.ui.plan_ui.enrich_plan_with_codebase_context",
            side_effect=return_spec,
        ),
        patch(
            "agentception.config.settings",
            MagicMock(anthropic_api_key="test-key", **_passthrough_settings()),
        ),
    ):
        resp = await async_client.post(
            "/api/plan/preview",
            json={"dump": "Build user authentication", "label_prefix": ""},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    events = _parse_sse_events(resp.text)
    chunk_events = [e for e in events if e.get("t") == "chunk"]
    done_events = [e for e in events if e.get("t") == "done"]
    assert chunk_events, "Expected at least one chunk event"
    assert len(done_events) == 1, "Expected exactly one done event"
    done = done_events[0]
    assert done["initiative"] == "auth-rewrite"
    assert done["phase_count"] == 1
    assert done["issue_count"] == 1
    assert isinstance(done["yaml"], str) and done["yaml"]


@pytest.mark.anyio
async def test_preview_thinking_and_content_chunks_are_both_streamed(async_client: AsyncClient) -> None:
    """Both chain-of-thought ('thinking') and content chunks are forwarded to the browser."""

    fenced_yaml = "```yaml\n" + _MINIMAL_VALID_YAML + "\n```\n"

    async def fake_llm_stream(
        *_args: str, **_kwargs: str | int | bool | float | None
    ) -> AsyncGenerator[LLMChunk, None]:
        yield LLMChunk(type="thinking", text="<internal reasoning>")
        yield LLMChunk(type="content", text=fenced_yaml)

    async def return_spec(spec: PlanSpec) -> PlanSpec:
        return spec

    with (
        patch(
            "agentception.routes.ui.plan_ui.completion_stream",
            side_effect=fake_llm_stream,
        ),
        patch(
            "agentception.readers.context_pack.build_context_pack",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.ui.plan_ui.enrich_plan_with_codebase_context",
            side_effect=return_spec,
        ),
        patch(
            "agentception.config.settings",
            MagicMock(anthropic_api_key="test-key", **_passthrough_settings()),
        ),
    ):
        resp = await async_client.post(
            "/api/plan/preview",
            json={"dump": "Build user authentication", "label_prefix": ""},
        )

    events = _parse_sse_events(resp.text)
    chunk_texts: list[str] = [str(e.get("text", "")) for e in events if e.get("t") == "chunk"]
    assert any("<internal reasoning>" in t for t in chunk_texts), "Thinking chunks must be streamed"
    assert any("initiative:" in t for t in chunk_texts), "Content chunks must be streamed"
    done_events = [e for e in events if e.get("t") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["initiative"] == "auth-rewrite"


@pytest.mark.anyio
async def test_preview_prose_response_uses_fallback_plan(async_client: AsyncClient) -> None:
    """When the LLM returns prose instead of YAML, we never push back — emit the fallback clarify-and-scope plan."""

    async def fake_llm_stream(
        *_args: str, **_kwargs: str | int | bool | float | None
    ) -> AsyncGenerator[LLMChunk, None]:
        yield LLMChunk(type="content", text="Sure, here are some ideas for your project...")

    async def return_spec(spec: PlanSpec) -> PlanSpec:
        return spec

    with (
        patch(
            "agentception.routes.ui.plan_ui.completion_stream",
            side_effect=fake_llm_stream,
        ),
        patch(
            "agentception.readers.context_pack.build_context_pack",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.ui.plan_ui.enrich_plan_with_codebase_context",
            side_effect=return_spec,
        ),
        patch(
            "agentception.config.settings",
            MagicMock(anthropic_api_key="test-key", **_passthrough_settings()),
        ),
    ):
        resp = await async_client.post(
            "/api/plan/preview",
            json={"dump": "?", "label_prefix": ""},
        )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    error_events = [e for e in events if e.get("t") == "error"]
    assert not error_events, "We never push back with an error for vague input"
    done_events = [e for e in events if e.get("t") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["initiative"] == "clarify-and-scope"
    assert done_events[0]["phase_count"] == 1
    assert done_events[0]["issue_count"] == 1


@pytest.mark.anyio
async def test_preview_malformed_yaml_uses_fallback_plan_not_error(async_client: AsyncClient) -> None:
    """When the LLM returns content that is invalid YAML (e.g. numbered list), we emit done with fallback, not error.

    Regression: safe_load can raise on input like '1. Login fails...\\n2. Rate limiter...';
    we must catch that and plug the fallback plan into the browser instead of showing a parse error.
    """

    async def fake_llm_stream(
        *_args: str, **_kwargs: str | int | bool | float | None
    ) -> AsyncGenerator[LLMChunk, None]:
        yield LLMChunk(
            type="content",
            text=(
                "1. Login fails intermittently on mobile\n"
                "2. Rate limiter not applied to /api/public\n"
                "3. CSV export hangs for reports > 10k rows"
            ),
        )

    async def return_spec(spec: PlanSpec) -> PlanSpec:
        return spec

    with (
        patch(
            "agentception.routes.ui.plan_ui.completion_stream",
            side_effect=fake_llm_stream,
        ),
        patch(
            "agentception.readers.context_pack.build_context_pack",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.ui.plan_ui.enrich_plan_with_codebase_context",
            side_effect=return_spec,
        ),
        patch(
            "agentception.config.settings",
            MagicMock(anthropic_api_key="test-key", **_passthrough_settings()),
        ),
    ):
        resp = await async_client.post(
            "/api/plan/preview",
            json={"dump": "Bug list", "label_prefix": ""},
        )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    error_events = [e for e in events if e.get("t") == "error"]
    assert not error_events, "Malformed YAML must yield fallback plan, not error event"
    done_events = [e for e in events if e.get("t") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["initiative"] == "clarify-and-scope"
    assert isinstance(done_events[0]["yaml"], str) and "initiative:" in done_events[0]["yaml"]


@pytest.mark.anyio
async def test_preview_local_model_think_tags_separated(async_client: AsyncClient) -> None:
    """Local model thinking (via <think> tags) is split at the LLM layer; only YAML is accumulated.

    Simulates what completion_stream returns after _normalize_think_tags: thinking chunks
    are properly classified so plan_ui only accumulates the content (fenced YAML).
    The done event must contain the real plan, not the fallback.
    """
    fenced_yaml = "```yaml\n" + _MINIMAL_VALID_YAML + "\n```\n"

    async def fake_llm_stream(
        *_args: str, **_kwargs: str | int | bool | float | None
    ) -> AsyncGenerator[LLMChunk, None]:
        yield LLMChunk(type="thinking", text="Let me plan this carefully.")
        yield LLMChunk(type="content", text=fenced_yaml)

    async def return_spec(spec: PlanSpec) -> PlanSpec:
        return spec

    with (
        patch(
            "agentception.routes.ui.plan_ui.completion_stream",
            side_effect=fake_llm_stream,
        ),
        patch(
            "agentception.readers.context_pack.build_context_pack",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.ui.plan_ui.enrich_plan_with_codebase_context",
            side_effect=return_spec,
        ),
        patch(
            "agentception.config.settings",
            MagicMock(anthropic_api_key="test-key", **_passthrough_settings()),
        ),
    ):
        resp = await async_client.post(
            "/api/plan/preview",
            json={"dump": "Bug triage list", "label_prefix": ""},
        )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    chunk_texts = [str(e.get("text", "")) for e in events if e.get("t") == "chunk"]
    assert any("Let me plan" in t for t in chunk_texts), "Thinking chunks streamed to browser"
    assert any("initiative:" in t for t in chunk_texts), "Content chunks streamed to browser"
    error_events = [e for e in events if e.get("t") == "error"]
    assert not error_events, "Must not produce an error"
    done_events = [e for e in events if e.get("t") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["initiative"] == "auth-rewrite", (
        "Must use the real plan, not fall back to clarify-and-scope"
    )


@pytest.mark.anyio
async def test_preview_multi_issue_yaml_with_repeated_structure_not_truncated(
    async_client: AsyncClient,
) -> None:
    """Multi-issue YAML with repeated body section headers (## Context etc.) must not be truncated.

    Regression: an earlier repetition-detector checked whether the last 150 chars of accumulated
    appeared earlier in the buffer.  Structured YAML naturally repeats issue body sections
    (## Context, ## Objective, depends_on: [], ...) so the detector fired mid-stream and the
    truncated YAML failed to parse, falling back to clarify-and-scope.

    The repetition_penalty in the model payload is the correct prevention mechanism.
    The stream must consume the full YAML regardless of structural repetition.
    """

    async def fake_llm_stream(
        *_args: str, **_kwargs: str | int | bool | float | None
    ) -> AsyncGenerator[LLMChunk, None]:
        yield LLMChunk(type="content", text=_TWO_PHASE_YAML)

    async def return_spec(spec: PlanSpec) -> PlanSpec:
        return spec

    with (
        patch(
            "agentception.routes.ui.plan_ui.completion_stream",
            side_effect=fake_llm_stream,
        ),
        patch(
            "agentception.readers.context_pack.build_context_pack",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "agentception.routes.ui.plan_ui.enrich_plan_with_codebase_context",
            side_effect=return_spec,
        ),
        patch(
            "agentception.config.settings",
            MagicMock(anthropic_api_key="test-key", **_passthrough_settings()),
        ),
    ):
        resp = await async_client.post(
            "/api/plan/preview",
            json={"dump": "Refactor backend", "label_prefix": ""},
        )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    error_events = [e for e in events if e.get("t") == "error"]
    assert not error_events, "Repeated YAML structure must not trigger an error"
    done_events = [e for e in events if e.get("t") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["initiative"] == "big-project"
    assert done_events[0]["phase_count"] == 2
    assert done_events[0]["issue_count"] == 3


@pytest.mark.anyio
async def test_preview_context_pack_is_prepended_to_dump(async_client: AsyncClient) -> None:
    """Context pack content is injected before the user dump in the LLM call."""
    received_prompt: list[str] = []

    async def capture_stream(
        user_prompt: str, **_kwargs: str | int | bool | float | None
    ) -> AsyncGenerator[LLMChunk, None]:
        received_prompt.append(user_prompt)
        yield LLMChunk(type="content", text=_MINIMAL_VALID_YAML)

    ctx_text = "## Open Issues\n- #42 Fix login\n"

    with (
        patch(
            "agentception.routes.ui.plan_ui.completion_stream",
            side_effect=capture_stream,
        ),
        patch(
            "agentception.readers.context_pack.build_context_pack",
            new_callable=AsyncMock,
            return_value=ctx_text,
        ),
        patch(
            "agentception.config.settings",
            MagicMock(anthropic_api_key="test-key", **_passthrough_settings()),
        ),
    ):
        await async_client.post(
            "/api/plan/preview",
            json={"dump": "Build auth", "label_prefix": ""},
        )

    assert received_prompt, "LLM stream was never called"
    first_prompt = received_prompt[0]
    assert isinstance(first_prompt, str)
    assert ctx_text in first_prompt, "Context pack must be prepended to the dump"
    assert "Build auth" in first_prompt


# ---------------------------------------------------------------------------
# POST /api/plan/file-issues
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_file_issues_empty_yaml_returns_422(async_client: AsyncClient) -> None:
    """Empty yaml_text → HTTP 422 before any gh calls."""
    resp = await async_client.post("/api/plan/file-issues", json={"yaml_text": ""})
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_file_issues_whitespace_yaml_returns_422(async_client: AsyncClient) -> None:
    """Whitespace-only yaml_text → HTTP 422."""
    resp = await async_client.post(
        "/api/plan/file-issues", json={"yaml_text": "   \n  "}
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_file_issues_invalid_yaml_returns_422(async_client: AsyncClient) -> None:
    """Syntactically invalid YAML → HTTP 422 with detail."""
    resp = await async_client.post(
        "/api/plan/file-issues", json={"yaml_text": ": invalid [yaml"}
    )
    assert resp.status_code == 422
    assert "YAML" in resp.json().get("detail", "")


@pytest.mark.anyio
async def test_file_issues_schema_invalid_yaml_returns_422(async_client: AsyncClient) -> None:
    """YAML that parses but fails PlanSpec validation → HTTP 422."""
    resp = await async_client.post(
        "/api/plan/file-issues", json={"yaml_text": "key: value\n"}
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_file_issues_valid_yaml_returns_sse_stream(async_client: AsyncClient) -> None:
    """A valid PlanSpec YAML starts the SSE stream with Content-Type text/event-stream."""
    from agentception.models import PlanSpec
    from agentception.readers.issue_creator import IssueFileEvent, StartEvent

    async def fake_file_issues(_spec: PlanSpec) -> AsyncGenerator[IssueFileEvent, None]:
        yield StartEvent(t="start", total=1, initiative="auth-rewrite")

    with patch(
        "agentception.readers.issue_creator.file_issues",
        side_effect=fake_file_issues,
    ):
        resp = await async_client.post(
            "/api/plan/file-issues", json={"yaml_text": _MINIMAL_VALID_YAML}
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    events = _parse_sse_events(resp.text)
    start_events = [e for e in events if e.get("t") == "start"]
    assert start_events, "Expected a start event in the SSE stream"
    assert start_events[0]["initiative"] == "auth-rewrite"


@pytest.mark.anyio
async def test_file_issues_forwards_all_event_types(async_client: AsyncClient) -> None:
    """All event types from the file_issues generator are forwarded verbatim."""
    from agentception.models import PlanSpec
    from agentception.readers.issue_creator import (
        DoneEvent,
        IssueEvent,
        IssueFileEvent,
        LabelEvent,
        StartEvent,
    )
    from agentception.readers.issue_creator import CreatedIssue

    async def fake_file_issues(_spec: PlanSpec) -> AsyncGenerator[IssueFileEvent, None]:
        yield StartEvent(t="start", total=1, initiative="auth-rewrite")
        yield LabelEvent(t="label", text="Ensuring labels exist…")
        yield IssueEvent(
            t="issue",
            index=1,
            total=1,
            number=101,
            url="https://github.com/test/repo/issues/101",
            title="Add SQLAlchemy User model",
            phase="0-foundation",
        )
        yield DoneEvent(
            t="done",
            total=1,
            initiative="auth-rewrite",
            batch_id="batch-abc123",
            issues=[
                CreatedIssue(
                    issue_id="auth-rewrite-p0-001",
                    number=101,
                    url="https://github.com/test/repo/issues/101",
                    title="Add SQLAlchemy User model",
                    phase="0-foundation",
                )
            ],
            coordinator_arch={},
        )

    with patch(
        "agentception.readers.issue_creator.file_issues",
        side_effect=fake_file_issues,
    ):
        resp = await async_client.post(
            "/api/plan/file-issues", json={"yaml_text": _MINIMAL_VALID_YAML}
        )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    types = {e.get("t") for e in events}
    assert types == {"start", "label", "issue", "done"}

    done = next(e for e in events if e.get("t") == "done")
    assert done["initiative"] == "auth-rewrite"
    assert done["total"] == 1
    assert done["batch_id"] == "batch-abc123"
    issues = done.get("issues", [])
    assert isinstance(issues, list) and len(issues) == 1
    first_issue = issues[0]
    assert isinstance(first_issue, dict)
    assert first_issue["number"] == 101


# ---------------------------------------------------------------------------
# GET /plan/{repo}/{initiative} — redirect to latest batch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_initiative_redirect_returns_404_when_no_batches(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative} returns 404 when get_initiative_batches returns []."""
    with patch(
        "agentception.db.queries.get_initiative_batches",
        new=AsyncMock(return_value=[]),
    ):
        resp = await async_client.get("/plan/agentception/no-such-initiative")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_plan_initiative_redirect_returns_302_to_latest_batch(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative} redirects to /{batch_id} when batches exist."""
    with patch(
        "agentception.db.queries.get_initiative_batches",
        new=AsyncMock(return_value=["batch-abc123def456", "batch-older"]),
    ):
        resp = await async_client.get(
            "/plan/agentception/auth-rewrite",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"].endswith(
        "/plan/agentception/auth-rewrite/batch-abc123def456"
    )


@pytest.mark.anyio
async def test_plan_initiative_redirect_returns_400_for_invalid_initiative(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative} returns 400 when the initiative slug is invalid."""
    resp = await async_client.get("/plan/agentception/-bad-start")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_plan_initiative_redirect_returns_404_for_unknown_repo(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative} returns 404 when the repo is not configured."""
    resp = await async_client.get("/plan/notarepo/some-initiative")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /plan/{repo}/{initiative}/{batch_id} — shareable batch overview
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plan_initiative_page_returns_404_when_not_found(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative}/{batch_id} returns 404 when summary is None."""
    with patch(
        "agentception.db.queries.get_initiative_summary",
        new=AsyncMock(return_value=None),
    ):
        resp = await async_client.get(
            "/plan/agentception/no-such-initiative/batch-abc123"
        )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_plan_initiative_page_returns_400_for_invalid_batch_id(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative}/{batch_id} returns 400 for invalid batch_id."""
    resp = await async_client.get("/plan/agentception/auth-rewrite/not-a-batch-id")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_plan_initiative_page_renders_summary(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative}/{batch_id} renders the initiative name and phase cards."""
    from agentception.db.queries import (
        InitiativeIssueRow,
        InitiativePhaseRow,
        InitiativeSummary,
    )

    summary: InitiativeSummary = InitiativeSummary(
        repo="cgcardona/agentception",
        initiative="auth-rewrite",
        batch_id="batch-abc123",
        phase_count=2,
        issue_count=3,
        open_count=2,
        closed_count=1,
        filed_at="2026-03-06T12:00:00",
        phases=[
            InitiativePhaseRow(
                label="auth-rewrite/0-foundation",
                short_label="0-foundation",
                order=0,
                is_active=True,
                is_complete=False,
                issues=[
                    InitiativeIssueRow(
                        number=101,
                        title="Add SQLAlchemy User model",
                        url="https://github.com/cgcardona/agentception/issues/101",
                        state="open",
                    ),
                    InitiativeIssueRow(
                        number=102,
                        title="JWT middleware",
                        url="https://github.com/cgcardona/agentception/issues/102",
                        state="closed",
                    ),
                ],
            ),
            InitiativePhaseRow(
                label="auth-rewrite/1-api",
                short_label="1-api",
                order=1,
                is_active=False,
                is_complete=False,
                issues=[
                    InitiativeIssueRow(
                        number=103,
                        title="Login endpoint",
                        url="https://github.com/cgcardona/agentception/issues/103",
                        state="open",
                    ),
                ],
            ),
        ],
    )

    with patch(
        "agentception.db.queries.get_initiative_summary",
        new=AsyncMock(return_value=summary),
    ):
        resp = await async_client.get(
            "/plan/agentception/auth-rewrite/batch-abc123"
        )

    assert resp.status_code == 200
    html = resp.text
    assert "auth-rewrite" in html
    assert "0-foundation" in html
    assert "1-api" in html
    assert "#101" in html
    assert "Add SQLAlchemy User model" in html
    assert "JWT middleware" in html
    assert "ACTIVE" in html
    assert "BLOCKED" in html
    # Closed issue gets struck-through link class
    assert "plan-done-issue-link--closed" in html
    # Filed date rendered
    assert "Mar" in html or "6" in html
    # Batch ID is displayed in the footer
    assert "batch-abc123" in html


@pytest.mark.anyio
async def test_plan_initiative_page_shows_complete_phase(
    async_client: AsyncClient,
) -> None:
    """GET /plan/{repo}/{initiative}/{batch_id} shows plan-done-phase-card--complete."""
    from agentception.db.queries import (
        InitiativeIssueRow,
        InitiativePhaseRow,
        InitiativeSummary,
    )

    summary: InitiativeSummary = InitiativeSummary(
        repo="cgcardona/agentception",
        initiative="my-proj",
        batch_id="batch-d0e123abc456",
        phase_count=1,
        issue_count=1,
        open_count=0,
        closed_count=1,
        filed_at=None,
        phases=[
            InitiativePhaseRow(
                label="my-proj/0-done",
                short_label="0-done",
                order=0,
                is_active=False,
                is_complete=True,
                issues=[
                    InitiativeIssueRow(
                        number=1,
                        title="Completed task",
                        url="https://github.com/cgcardona/agentception/issues/1",
                        state="closed",
                    ),
                ],
            ),
        ],
    )

    with patch(
        "agentception.db.queries.get_initiative_summary",
        new=AsyncMock(return_value=summary),
    ):
        resp = await async_client.get(
            "/plan/agentception/my-proj/batch-d0e123abc456"
        )

    assert resp.status_code == 200
    assert "plan-done-phase-card--complete" in resp.text
    assert "COMPLETE" in resp.text


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _passthrough_settings() -> dict[str, JsonValue]:
    """Return a dict of settings attributes needed by route handlers under test.

    When we replace ``agentception.config.settings`` with a ``MagicMock`` the
    mock auto-stubs every attribute access, which is fine for most calls.  The
    one exception is any attribute explicitly checked for truthiness in the
    handler itself (like ``anthropic_api_key``).  All others are left to the
    MagicMock default.
    """
    return {}
