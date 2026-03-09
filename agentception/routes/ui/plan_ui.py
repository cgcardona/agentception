"""UI routes: Plan page and its API endpoints.

Endpoints
---------
POST /api/plan/preview                                    — Step 1.A: brain dump → PlanSpec YAML (SSE stream)
POST /api/plan/validate                                   — Validate (possibly edited) YAML against PlanSpec schema
POST /api/plan/file-issues                                — Step 1.B: file GitHub issues from a PlanSpec YAML (SSE)
GET  /plan                                                — full page (Alpine state machine)
GET  /plan/recent-runs                                    — HTMX partial (sidebar refresh)
GET  /plan/{org}/{repo}/{initiative}                      — redirect to latest batch for this initiative
GET  /plan/{org}/{repo}/{initiative}/{batch_id}           — shareable, server-rendered initiative overview
GET  /api/plan/{run_id}/plan-text                         — return original plan text for re-run

Streaming protocol (POST /api/plan/preview)
-------------------------------------------
The endpoint returns ``text/event-stream`` (SSE).  Each event is a JSON object
on a ``data:`` line followed by ``\\n\\n``.  Event shapes::

    {"t": "chunk", "text": "<raw token(s)>"}   -- one or more output tokens
    {"t": "done",  "yaml": "<full yaml>",
                   "initiative": "...",
                   "phase_count": N, "issue_count": N}  -- stream complete
    {"t": "error", "detail": "<message>"}       -- stream failed

The browser accumulates ``chunk`` texts, shows them live, then on ``done``
loads the canonical validated YAML into the CodeMirror 6 editor.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Literal, TypedDict

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.requests import Request

from agentception.readers.llm_phase_planner import _strip_fences
from agentception.services.llm import LLMChunk, call_anthropic_stream
from ._shared import _TEMPLATES

if TYPE_CHECKING:
    from agentception.readers.issue_creator import IssueFileEvent

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Plan page — static data (defined once, passed to Jinja)
# ---------------------------------------------------------------------------

_PLAN_SEEDS = [
    {
        "label": "🐛 Bug triage",
        "text": (
            "- Login fails intermittently on mobile\n"
            "- Rate limiter not applied to /api/public\n"
            "- CSV export hangs for reports > 10k rows\n"
            "- Dark mode toggle state lost on refresh"
        ),
    },
    {
        "label": "🗓️ Sprint planning",
        "text": (
            "- Migrate auth to JWT with refresh tokens\n"
            "- Add pagination to the issues API\n"
            "- Write integration tests for the billing flow\n"
            "- Document the webhook contract"
        ),
    },
    {
        "label": "💡 Feature ideas",
        "text": (
            "- Add dark mode across the entire dashboard\n"
            "- Show a live activity feed of recent events on the home page\n"
            "- Let users customize notification preferences per project\n"
            "- Export any report as a shareable link with a public read-only view"
        ),
    },
    {
        "label": "🏗️ Tech debt",
        "text": (
            "- Consolidate duplicate GitHub fetch helpers into a single client\n"
            "- Add missing type coverage to the remaining untyped modules\n"
            "- Extract business logic out of route handlers into services\n"
            "- Replace inline SQL strings with typed SQLAlchemy queries"
        ),
    },
]


# ---------------------------------------------------------------------------
# Named types — sidebar entries and SSE event shapes
# ---------------------------------------------------------------------------


class _RecentPlanEntry(TypedDict):
    """One entry in the recent-runs sidebar, built from a plan worktree."""

    slug: str
    label_prefix: str
    preview: str
    ts: str
    batch_id: str
    item_count: str


class _ChunkEvent(TypedDict):
    """Streaming YAML token from the LLM (Step 1.A preview stream)."""

    t: Literal["chunk"]
    text: str


class _PreviewDoneEvent(TypedDict):
    """Emitted once when the LLM stream completes and PlanSpec validation passes."""

    t: Literal["done"]
    yaml: str
    initiative: str
    phase_count: int
    issue_count: int


class _PreviewErrorEvent(TypedDict):
    """Emitted when the preview stream encounters a fatal error."""

    t: Literal["error"]
    detail: str


#: Union of all event shapes produced by the Step 1.A preview stream.
type _PreviewSseEvent = _ChunkEvent | _PreviewDoneEvent | _PreviewErrorEvent

#: Recursive type covering every value ``yaml.safe_load`` can return.
#: Python 3.12 recursive ``type`` aliases make this possible without Any.
type _YamlNode = str | int | float | bool | None | list[_YamlNode] | dict[str, _YamlNode]


# ---------------------------------------------------------------------------
# YAML normalisation shim
# ---------------------------------------------------------------------------


def _normalize_plan_dict(raw: _YamlNode) -> _YamlNode:
    """Coerce alternative YAML shapes into the canonical PlanSpec mapping.

    Claude occasionally returns a top-level dict keyed by the initiative slug
    rather than using flat ``initiative`` / ``phases`` keys, e.g.::

        tech-debt-sprint:
          phase-0:
            description: "..."
            depends_on: []
            issues: [...]

    This function detects that pattern (single top-level key that is neither
    ``"initiative"`` nor ``"phases"``, whose value is a dict of phase-labelled
    sub-dicts) and converts it to::

        initiative: tech-debt-sprint
        phases:
          - label: phase-0
            description: "..."
            depends_on: []
            issues: [...]

    All other shapes are returned unchanged so normal Pydantic validation runs.
    """
    if not isinstance(raw, dict):
        return raw

    # Already in canonical form.
    if "initiative" in raw or "phases" in raw:
        return raw

    keys = list(raw.keys())
    if len(keys) != 1:
        return raw  # multiple top-level keys — let Pydantic report the real error

    initiative_slug = str(keys[0])
    body = raw[initiative_slug]

    if not isinstance(body, dict):
        return raw

    # Check if the values look like phase dicts (have label-like keys starting with "phase-").
    phase_keys = [k for k in body if isinstance(k, str) and k.startswith("phase-")]
    if not phase_keys:
        return raw

    # Convert {phase-0: {description, depends_on, issues}, ...} → list of phase dicts.
    phases: list[_YamlNode] = []
    for phase_label in sorted(body.keys()):
        phase_body = body[phase_label]
        if not isinstance(phase_body, dict):
            continue
        phase_entry: dict[str, _YamlNode] = {"label": phase_label}
        phase_entry.update(phase_body)
        phases.append(phase_entry)

    logger.warning(
        "⚠️ Normalised alternative YAML shape: initiative-as-key=%r → canonical PlanSpec",
        initiative_slug,
    )
    return {"initiative": initiative_slug, "phases": phases}


async def _build_recent_plans() -> list[_RecentPlanEntry]:
    """Return metadata for the 6 most recent coordinator plan runs from the DB.

    Queries ``ac_agent_runs`` for coordinator-tier runs ordered by spawn time.
    Each entry contains: slug, label_prefix, preview, ts, batch_id, item_count.
    Returns an empty list when the DB is unavailable — the plan page degrades
    gracefully to showing no recent runs.
    """
    from agentception.db.queries import get_agent_run_history

    recent_plans: list[_RecentPlanEntry] = []
    try:
        rows = await get_agent_run_history(limit=6, status=None)
        coordinator_rows = [r for r in rows if r.get("tier") == "coordinator"][:6]
        for row in coordinator_rows:
            run_id = str(row.get("id", ""))
            batch_id = str(row.get("batch_id") or run_id)
            # Derive label_prefix from the batch_id (format: label-<slug>-<stamp>-<hex>).
            label_prefix = ""
            if batch_id.startswith("label-"):
                parts = batch_id.split("-", 2)
                if len(parts) >= 2:
                    label_prefix = parts[1]
            spawned_at = str(row.get("spawned_at", ""))
            ts_fmt = spawned_at[:16].replace("T", " ") if spawned_at else ""
            recent_plans.append(_RecentPlanEntry(
                slug=run_id,
                label_prefix=label_prefix,
                preview="",
                ts=ts_fmt,
                batch_id=batch_id,
                item_count="—",
            ))
    except Exception:
        pass
    return recent_plans


class PlanDraftRequest(BaseModel):
    """Request body for ``POST /api/plan/preview`` (Step 1.A).

    ``dump`` is the raw plan text.  ``label_prefix`` is an optional initiative
    slug override — when supplied it replaces the ``initiative`` field Claude
    would have inferred from the text.
    """

    dump: str
    label_prefix: str = ""


class PlanDraftYamlResponse(BaseModel):
    """Response from ``POST /api/plan/preview`` (Step 1.A).

    ``yaml`` is a valid PlanSpec YAML string ready to be loaded into the
    CodeMirror 6 editor.  ``initiative`` is extracted for the UI to display.
    ``phase_count`` and ``issue_count`` are convenience totals.
    """

    yaml: str
    initiative: str
    phase_count: int
    issue_count: int


def _sse(obj: "_PreviewSseEvent | IssueFileEvent") -> str:
    """Serialise a named SSE event TypedDict as a single ``data:`` line."""
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/api/plan/preview")
async def plan_preview(body: PlanDraftRequest) -> StreamingResponse:
    """Step 1.A -- convert free-form text into a PlanSpec YAML via SSE stream.

    Returns ``text/event-stream``.  Each event is a JSON object on a ``data:``
    line.  See module docstring for the full event shape reference.

    Requires ``ANTHROPIC_API_KEY`` to be configured — returns HTTP 503 if absent.
    """
    from agentception.config import settings as _cfg
    from agentception.models import PlanSpec
    from agentception.readers.llm_phase_planner import _build_yaml_system_prompt

    dump = body.dump.strip()
    if not dump:
        raise HTTPException(status_code=422, detail="Plan text must not be empty.")

    # Build the context pack before streaming so the full prompt is ready.
    # Errors are swallowed inside build_context_pack — we never fail the request
    # just because GitHub is slow or a label fetch times out.
    from agentception.readers.context_pack import build_context_pack
    ctx = await build_context_pack()
    augmented_dump = f"{ctx}\n## Your plan\n{dump}" if ctx else dump

    async def _llm_stream() -> AsyncGenerator[str, None]:
        """Stream LLM tokens then emit a validated ``done`` event.

        Yields two SSE event types to the browser:
          {"t": "chunk",    "text": "..."}  -- output YAML token
          {"t": "done",     "yaml": "...", ...}  -- validated, complete
          {"t": "error",    "detail": "..."}  -- something went wrong

        Chain-of-thought ("thinking") tokens from extended reasoning are
        intentionally discarded — they can leak prompt internals and anchor
        users on model reasoning rather than the YAML output.
        """
        accumulated = ""
        try:
            chunk: LLMChunk
            async for chunk in call_anthropic_stream(
                augmented_dump,
                system_prompt=_build_yaml_system_prompt(),
                temperature=0.2,
                max_tokens=8192,
            ):
                if chunk["type"] == "thinking":
                    pass  # discard — never sent to browser
                else:
                    accumulated += chunk["text"]
                    yield _sse(_ChunkEvent(t="chunk", text=chunk["text"]))

            # Validate and canonicalise the full output.
            yaml_str = _strip_fences(accumulated)

            # Detect prose response: yaml.safe_load returns a str (not a dict)
            # when the model outputs conversational text instead of YAML.
            import yaml as _yaml_mod
            parsed: _YamlNode = _yaml_mod.safe_load(yaml_str) if yaml_str.strip() else None
            if not isinstance(parsed, dict):
                logger.warning(
                    "⚠️ LLM returned prose instead of YAML (first 200 chars): %s",
                    accumulated[:200],
                )
                yield _sse(_PreviewErrorEvent(
                    t="error",
                    detail=(
                        "Your input was too short or vague for the model to plan. "
                        "Add more detail — describe actual bugs, features, or tech debt you want tackled."
                    ),
                ))
                return

            # Normalise alternative YAML structures Claude occasionally produces.
            # Claude sometimes returns {initiative_slug: {phase_label: {...}}}
            # instead of the canonical {initiative: ..., phases: [...]} shape.
            parsed = _normalize_plan_dict(parsed)

            spec = PlanSpec.model_validate(parsed)
            canonical = spec.to_yaml()
            total = sum(len(p.issues) for p in spec.phases)
            logger.info(
                "✅ Plan stream done: initiative=%s phases=%d issues=%d",
                spec.initiative, len(spec.phases), total,
            )
            yield _sse(_PreviewDoneEvent(
                t="done",
                yaml=canonical,
                initiative=spec.initiative,
                phase_count=len(spec.phases),
                issue_count=total,
            ))
        except Exception as exc:
            logger.error("❌ Plan stream error: %s | accumulated (200): %s", exc, accumulated[:200])
            yield _sse(_PreviewErrorEvent(t="error", detail=str(exc)))

    if not _cfg.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured. Set it to use the Plan step.",
        )

    return StreamingResponse(_llm_stream(), media_type="text/event-stream")


class PlanValidateRequest(BaseModel):
    """Request body for ``POST /api/plan/validate`` (client-side debounce)."""

    yaml_text: str


class PlanValidateResponse(BaseModel):
    """Validation result from ``POST /api/plan/validate``."""

    valid: bool
    initiative: str = ""
    phase_count: int = 0
    issue_count: int = 0
    detail: str = ""


@router.post("/api/plan/validate", response_model=PlanValidateResponse)
async def plan_validate(body: PlanValidateRequest) -> PlanValidateResponse:
    """Validate a (possibly edited) PlanSpec YAML against the schema.

    Called by the CodeMirror 6 editor's ``EditorView.updateListener``
    (debounced at 600 ms) so the user sees immediate feedback while editing.

    Returns HTTP 200 with ``valid: false`` and a ``detail`` message on schema
    errors — does NOT return 4xx, so the JS handler stays simple.
    """
    from agentception.models import PlanSpec

    text = body.yaml_text.strip()
    if not text:
        return PlanValidateResponse(valid=False, detail="YAML is empty.")

    try:
        spec = PlanSpec.from_yaml(text)
    except Exception as exc:
        short = str(exc)[:200]
        return PlanValidateResponse(valid=False, detail=short)

    total = sum(len(p.issues) for p in spec.phases)
    return PlanValidateResponse(
        valid=True,
        initiative=spec.initiative,
        phase_count=len(spec.phases),
        issue_count=total,
    )


class PlanFileIssuesRequest(BaseModel):
    """Request body for ``POST /api/plan/file-issues`` (Step 1.B).

    ``yaml_text`` must be a valid PlanSpec YAML string, exactly as it appears
    in the CodeMirror editor after the user has reviewed and (optionally) edited
    the output from Step 1.A.
    """

    yaml_text: str


@router.post("/api/plan/file-issues")
async def plan_file_issues(body: PlanFileIssuesRequest) -> StreamingResponse:
    """Step 1.B — file GitHub issues directly from a PlanSpec YAML via SSE.

    Accepts the (possibly edited) YAML from the CodeMirror 6 editor, validates it
    against PlanSpec, ensures the required GitHub labels exist, then creates all
    issues using the ``gh`` CLI — no agents, no LLM calls, no worktrees.

    Streaming protocol (``text/event-stream``)
    ------------------------------------------
    Each event is a JSON object on a ``data:`` line followed by ``\\n\\n``::

        {"t": "start",   "total": N, "initiative": "..."}
        {"t": "label",   "text": "..."}
        {"t": "issue",   "index": N, "total": N, "number": N,
                         "url": "...", "title": "...", "phase": "..."}
        {"t": "blocked", "number": N, "blocked_by": [N, ...]}
        {"t": "done",    "total": N, "initiative": "...",
                         "issues": [{number, url, title, phase, issue_id}, ...]}
        {"t": "error",   "detail": "..."}

    On ``done`` the browser should flip to the success state and render links.
    On ``error`` the browser should show the detail message and stay in review.
    """
    from agentception.models import PlanSpec
    from agentception.readers.issue_creator import file_issues

    text = body.yaml_text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="YAML must not be empty.")

    try:
        spec = PlanSpec.from_yaml(text)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid PlanSpec YAML: {exc}"
        ) from exc

    async def _stream() -> AsyncGenerator[str, None]:
        async for event in file_issues(spec):
            yield _sse(event)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/", response_class=HTMLResponse)
@router.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request) -> HTMLResponse:
    """Plan — convert free-form text into phased GitHub issues."""
    from agentception.config import settings as _cfg

    recent_plans = await _build_recent_plans()
    return _TEMPLATES.TemplateResponse(
        request,
        "plan.html",
        {
            "recent_plans": recent_plans,
            "gh_repo": _cfg.gh_repo,
            "seeds": _PLAN_SEEDS,
        },
    )


@router.get("/plan/recent-runs", response_class=HTMLResponse)
async def plan_recent_runs(request: Request) -> HTMLResponse:
    """HTMX partial — returns the recent-runs sidebar section.

    Triggered by Alpine after a successful plan submit so the sidebar
    updates without a full page reload.
    """
    from agentception.config import settings as _cfg

    recent_plans = await _build_recent_plans()
    return _TEMPLATES.TemplateResponse(
        request,
        "_plan_recent_runs.html",
        {"recent_plans": recent_plans, "gh_repo": _cfg.gh_repo},
    )


_INITIATIVE_SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_BATCH_ID_RE: re.Pattern[str] = re.compile(r"^batch-[0-9a-f]+$")
_REPO_NAME_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _validate_path_params(
    repo: str,
    initiative: str,
    batch_id: str | None = None,
) -> None:
    """Raise HTTP 400 when any URL segment fails its format check."""
    if not _REPO_NAME_RE.match(repo):
        raise HTTPException(status_code=400, detail="Invalid repo slug.")
    if not _INITIATIVE_SLUG_RE.match(initiative):
        raise HTTPException(status_code=400, detail="Invalid initiative slug.")
    if batch_id is not None and not _BATCH_ID_RE.match(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch_id format.")


def _resolve_gh_repo(repo: str) -> str:
    """Reconstruct the full ``org/repo`` string from a bare repo name.

    The URL scheme uses only the bare repo name (e.g. ``agentception``) to
    keep URLs short.  This helper looks up the configured ``settings.gh_repo``
    and returns the full qualified name (e.g. ``cgcardona/agentception``).

    Raises HTTP 404 when the repo name does not match the configured repo,
    preventing enumeration of arbitrary GitHub repos.
    """
    from agentception.config import settings as _cfg

    configured_name = _cfg.gh_repo.split("/")[-1]
    if repo != configured_name:
        raise HTTPException(
            status_code=404,
            detail=f"Repo '{repo}' is not configured in this AgentCeption instance.",
        )
    return _cfg.gh_repo


@router.get("/plan/{repo}/{initiative}", response_model=None)
async def plan_initiative_redirect(
    repo: str,
    initiative: str,
) -> Response:
    """Redirect to the most recent batch for this initiative.

    ``GET /plan/{repo}/{initiative}`` is the human-friendly URL that always
    points to the latest filing.  It resolves to the canonical
    ``/plan/{repo}/{initiative}/{batch_id}`` form by querying the DB.

    Returns 400 on invalid slug, 404 when no batches exist.
    """
    from agentception.db.queries import get_initiative_batches

    _validate_path_params(repo, initiative)
    gh_repo = _resolve_gh_repo(repo)
    batches = await get_initiative_batches(gh_repo, initiative)
    if not batches:
        raise HTTPException(
            status_code=404,
            detail=f"Initiative '{initiative}' not found in '{gh_repo}'. Has Phase 1B been run?",
        )
    latest = batches[0]
    return RedirectResponse(url=f"/plan/{repo}/{initiative}/{latest}", status_code=302)


@router.get("/plan/{repo}/{initiative}/{batch_id}", response_class=HTMLResponse)
async def plan_initiative_page(
    request: Request,
    repo: str,
    initiative: str,
    batch_id: str,
) -> HTMLResponse:
    """Shareable, server-rendered overview of one filing batch.

    Reachable directly (e.g. when a teammate follows a shared link) or via
    ``history.pushState`` in ``plan.ts`` immediately after Phase 1B completes.
    Renders the same visual as the Alpine done-state as a static Jinja2 template
    — no JavaScript required.

    Returns 400 on invalid slugs, 404 when the (repo, initiative, batch_id)
    triple has no rows in ``initiative_phases``.
    """
    import datetime as _dt

    from agentception.db.queries import get_initiative_summary

    _validate_path_params(repo, initiative, batch_id)
    gh_repo = _resolve_gh_repo(repo)

    summary = await get_initiative_summary(gh_repo, initiative, batch_id)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail=f"Batch '{batch_id}' for '{initiative}' in '{gh_repo}' not found.",
        )

    filed_at_display: str | None = None
    if summary["filed_at"]:
        filed_dt = _dt.datetime.fromisoformat(summary["filed_at"])
        filed_at_display = filed_dt.strftime("%-d %b %Y")

    return _TEMPLATES.TemplateResponse(
        request,
        "plan_initiative.html",
        {
            "summary": summary,
            "filed_at_display": filed_at_display,
        },
    )


@router.get("/api/plan/{run_id}/plan-text")
async def plan_run_text(run_id: str) -> JSONResponse:
    """Return the original PLAN_DUMP text for a given run slug.

    Used by the "Re-run →" button in the sidebar: the JS handler fetches this,
    populates the main textarea, and switches Alpine to the ``input`` step so
    the user can edit and resubmit without copy-pasting.

    Parameters
    ----------
    run_id:
        The directory slug, e.g. ``plan-20260303-164033``.  Must start
        with ``plan-`` and must not contain path traversal characters.

    Raises
    ------
    HTTP 400
        When ``run_id`` contains illegal characters or does not start with
        ``plan-``.
    HTTP 404
        When the worktree directory or ``.agent-task`` file does not exist, or
        the file contains no ``PLAN_DUMP:`` section.
    """
    from agentception.config import settings as _cfg

    if not run_id.startswith("plan-") or "/" in run_id or ".." in run_id:
        raise HTTPException(status_code=400, detail="Invalid run_id format.")

    task_file = _cfg.worktrees_dir / run_id / ".agent-task"
    if not task_file.exists():
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    try:
        content = task_file.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("⚠️ Could not read .agent-task for run %s: %s", run_id, exc)
        raise HTTPException(status_code=404, detail="Could not read task file.") from exc

    import tomllib

    try:
        data = tomllib.loads(content)
    except Exception as exc:
        logger.warning("⚠️ Could not parse .agent-task for run %s: %s", run_id, exc)
        raise HTTPException(status_code=404, detail="Could not parse task file.") from exc

    plan_draft = data.get("plan_draft", {})
    plan_text = plan_draft.get("dump", "") if isinstance(plan_draft, dict) else ""
    if not plan_text:
        raise HTTPException(status_code=404, detail="No plan_draft.dump in task file.")

    return JSONResponse({"plan_text": plan_text})
