from __future__ import annotations

"""UI routes: Plan page and its API endpoints.

Endpoints
---------
POST /api/plan/preview                   — Step 1.A: brain dump → PlanSpec YAML (SSE stream)
POST /api/plan/validate                  — Validate (possibly edited) YAML against PlanSpec
GET  /plan                               — full page
GET  /plan/recent-runs                   — HTMX partial (sidebar refresh)
GET  /api/plan/{run_id}/plan-text        — return original plan text for re-run

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
loads the canonical validated YAML into the Monaco editor.
"""

import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.requests import Request

from agentception.readers.llm_phase_planner import _strip_fences
from agentception.services.llm import call_openrouter_stream
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Plan page — static data (defined once, passed to Jinja)
# ---------------------------------------------------------------------------

_PLAN_FUNNEL_STAGES = [
    {"icon": "🧠", "label": "Plan",    "desc": "Your raw input"},
    {"icon": "📋", "label": "Analyze", "desc": "Classify items"},
    {"icon": "🗂️", "label": "Phase",   "desc": "Group by dependency"},
    {"icon": "🏷️", "label": "Label",   "desc": "Create GitHub labels"},
    {"icon": "📝", "label": "Issues",  "desc": "File structured tickets"},
    {"icon": "🤖", "label": "Agents",  "desc": "Dispatch to engineers"},
]

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
            "- Let users star/pin their favourite agents\n"
            "- Add Slack notifications for PR merges\n"
            "- Dark mode across the entire dashboard\n"
            "- Export pipeline config as a shareable template"
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


def _normalize_plan_dict(raw: object) -> object:
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
    phases: list[dict[str, object]] = []
    for phase_label in sorted(body.keys()):
        phase_body = body[phase_label]
        if not isinstance(phase_body, dict):
            continue
        phase_entry: dict[str, object] = {"label": phase_label}
        phase_entry.update(phase_body)
        phases.append(phase_entry)

    logger.warning(
        "⚠️ Normalised alternative YAML shape: initiative-as-key=%r → canonical PlanSpec",
        initiative_slug,
    )
    return {"initiative": initiative_slug, "phases": phases}


def _parse_task_fields(content: str) -> dict[str, str]:
    """Parse key=value lines from the structured header of a ``.agent-task`` file.

    Only processes lines before the first blank line or ``PLAN_DUMP:`` marker so
    that multi-line plan text is never misinterpreted as a key=value pair.
    """
    fields: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "PLAN_DUMP:":
            break
        if "=" in stripped:
            key, _, val = stripped.partition("=")
            fields[key.strip()] = val.strip()
    return fields


def _count_plan_items(plan_text: str) -> int:
    """Count non-empty lines in a PLAN_DUMP block as a proxy for item count."""
    return sum(1 for ln in plan_text.splitlines() if ln.strip())


async def _build_recent_plans() -> list[dict[str, str]]:
    """Scan the worktrees directory and return metadata for the 6 most recent plan runs.

    Each entry contains: slug, label_prefix, preview, ts, batch_id, item_count.
    ``item_count`` is a line-count heuristic over the PLAN_DUMP block (not a live
    GitHub issue count) so no network call is needed on the hot render path.
    """
    from agentception.config import settings as _cfg

    recent_plans: list[dict[str, str]] = []
    worktrees_dir = _cfg.worktrees_dir
    try:
        if worktrees_dir.exists():
            candidates = sorted(
                (d for d in worktrees_dir.iterdir() if d.is_dir() and d.name.startswith("plan-")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for d in candidates[:6]:
                label_prefix = ""
                preview = ""
                batch_id = d.name
                item_count = "—"
                task_file = d / ".agent-task"
                if task_file.exists():
                    try:
                        content = task_file.read_text(encoding="utf-8")
                        fields = _parse_task_fields(content)
                        label_prefix = fields.get("LABEL_PREFIX", "")
                        batch_id = fields.get("BATCH_ID", d.name)
                        if "PLAN_DUMP:" in content:
                            plan_part = content.split("PLAN_DUMP:", 1)[1].strip()
                            first = next((ln.strip() for ln in plan_part.splitlines() if ln.strip()), "")
                            preview = first[:90]
                            count = _count_plan_items(plan_part)
                            item_count = str(count) if count else "—"
                    except OSError:
                        pass
                ts_raw = d.name[len("plan-"):]
                try:
                    ts_fmt = f"{ts_raw[:4]}-{ts_raw[4:6]}-{ts_raw[6:8]} {ts_raw[9:11]}:{ts_raw[11:13]}"
                except Exception:
                    ts_fmt = ts_raw
                recent_plans.append({
                    "slug": d.name,
                    "label_prefix": label_prefix,
                    "preview": preview,
                    "ts": ts_fmt,
                    "batch_id": batch_id,
                    "item_count": item_count,
                })
    except OSError:
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
    Monaco editor.  ``initiative`` is extracted for the UI to display.
    ``phase_count`` and ``issue_count`` are convenience totals.
    """

    yaml: str
    initiative: str
    phase_count: int
    issue_count: int


def _sse(obj: dict[str, object]) -> str:
    """Format a dict as a single SSE data line."""
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/api/plan/preview")
async def plan_preview(body: PlanDraftRequest) -> StreamingResponse:
    """Step 1.A -- convert free-form text into a PlanSpec YAML via SSE stream.

    Returns ``text/event-stream``.  Each event is a JSON object on a ``data:``
    line.  See module docstring for the full event shape reference.

    Requires ``OPENROUTER_API_KEY`` to be configured — returns HTTP 503 if absent.
    """
    from agentception.config import settings as _cfg
    from agentception.models import PlanSpec
    from agentception.readers.llm_phase_planner import _YAML_SYSTEM_PROMPT

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
            async for llm_chunk in call_openrouter_stream(
                augmented_dump,
                system_prompt=_YAML_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=8192,
            ):
                if llm_chunk["type"] == "thinking":
                    pass  # discard — never sent to browser
                else:
                    # "content" chunks are the YAML output
                    accumulated += llm_chunk["text"]
                    yield _sse({"t": "chunk", "text": llm_chunk["text"]})

            # Validate and canonicalise the full output.
            yaml_str = _strip_fences(accumulated)

            # Detect prose response: yaml.safe_load returns a str (not a dict)
            # when the model outputs conversational text instead of YAML.
            import yaml as _yaml_mod
            parsed: object = _yaml_mod.safe_load(yaml_str) if yaml_str.strip() else None
            if not isinstance(parsed, dict):
                logger.warning(
                    "⚠️ LLM returned prose instead of YAML (first 200 chars): %s",
                    accumulated[:200],
                )
                yield _sse({
                    "t": "error",
                    "detail": (
                        "Your input was too short or vague for the model to plan. "
                        "Add more detail — describe actual bugs, features, or tech debt you want tackled."
                    ),
                })
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
            yield _sse({
                "t": "done",
                "yaml": canonical,
                "initiative": spec.initiative,
                "phase_count": len(spec.phases),
                "issue_count": total,
            })
        except Exception as exc:
            logger.error("❌ Plan stream error: %s | accumulated (200): %s", exc, accumulated[:200])
            yield _sse({"t": "error", "detail": str(exc)})

    if not _cfg.openrouter_api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENROUTER_API_KEY is not configured. Set it to use the Plan step.",
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

    Called by the Monaco editor's ``onDidChangeModelContent`` handler
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

    Accepts the (possibly edited) YAML from the CodeMirror editor, validates it
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
            yield _sse(dict(event))  # TypedDict → plain dict for _sse

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
            "funnel_stages": _PLAN_FUNNEL_STAGES,
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

    if "PLAN_DUMP:" not in content:
        raise HTTPException(status_code=404, detail="No PLAN_DUMP section in task file.")

    plan_text = content.split("PLAN_DUMP:", 1)[1].strip()
    return JSONResponse({"plan_text": plan_text})
