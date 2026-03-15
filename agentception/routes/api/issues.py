"""API routes: GitHub issue/PR HTMX partials (comments, CI checks, reviews)."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.readers.github import (
    add_label_to_issue,
    ensure_label_exists,
    get_issue_comments,
    get_open_issues,
    get_pr_checks,
    get_pr_reviews,
)
from agentception.readers.pipeline_config import read_pipeline_config
from agentception.routes.ui._shared import _TEMPLATES
from agentception.types import JsonValue

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_APPROVAL_LABELS: list[str] = ["db-schema", "security", "api-contract"]


@router.get("/issues/{repo}/{number}/comments", response_class=HTMLResponse)
async def issue_comments_partial(request: Request, repo: str, number: int) -> HTMLResponse:
    """HTMX partial: render comments for issue #{number}.

    Lazily fetches from GitHub so the issue detail page loads without blocking.
    The ``repo`` path segment is accepted for URL uniqueness in HTMX routing
    but the reader uses the globally configured repo from settings.
    """
    comments: list[dict[str, JsonValue]] = []
    try:
        comments = await get_issue_comments(number)
    except Exception as exc:
        logger.warning("⚠️  get_issue_comments(%d) failed: %s", number, exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/issue_comments.html",
        {"comments": comments},
    )


@router.get("/prs/{repo}/{number}/checks", response_class=HTMLResponse)
async def pr_checks_partial(request: Request, repo: str, number: int) -> HTMLResponse:
    """HTMX partial: render CI check statuses for PR #{number}.

    The ``repo`` path segment is accepted for URL uniqueness in HTMX routing
    but the reader uses the globally configured repo from settings.
    """
    checks: list[dict[str, JsonValue]] = []
    error: str | None = None
    try:
        checks = await get_pr_checks(number)
    except Exception as exc:
        error = str(exc)
        logger.warning("⚠️  get_pr_checks(%d) failed: %s", number, exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/pr_checks.html",
        {"checks": checks, "error": error},
    )


@router.get("/prs/{repo}/{number}/reviews", response_class=HTMLResponse)
async def pr_reviews_partial(request: Request, repo: str, number: int) -> HTMLResponse:
    """HTMX partial: render review decisions for PR #{number}.

    The ``repo`` path segment is accepted for URL uniqueness in HTMX routing
    but the reader uses the globally configured repo from settings.
    """
    reviews: list[dict[str, JsonValue]] = []
    error: str | None = None
    try:
        reviews = await get_pr_reviews(number)
    except Exception as exc:
        error = str(exc)
        logger.warning("⚠️  get_pr_reviews(%d) failed: %s", number, exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/pr_reviews.html",
        {"reviews": reviews, "error": error},
    )


@router.get("/issues/approval-queue", response_class=HTMLResponse)
async def approval_queue_partial(request: Request) -> HTMLResponse:
    """HTMX partial: render the list of issues pending human approval.

    Fetches all open issues, retains those whose label set intersects the
    configured ``approval_required_labels``, and removes any that already
    carry the ``"approved"`` label.  Renders ``partials/approval_queue.html``
    so callers can embed it via ``hx-get`` with a polling trigger.
    """
    approval_labels: list[str] = _DEFAULT_APPROVAL_LABELS
    try:
        config = await read_pipeline_config()
        approval_labels = config.approval_required_labels
    except Exception as exc:
        logger.warning("⚠️  Could not read pipeline config for approval labels: %s", exc)

    issues: list[dict[str, JsonValue]] = []
    try:
        all_issues = await get_open_issues()
        for issue in all_issues:
            raw_labels = issue.get("labels")
            if not isinstance(raw_labels, list):
                continue
            label_names: set[str] = set()
            for lbl in raw_labels:
                if isinstance(lbl, dict):
                    name = lbl.get("name")
                    if isinstance(name, str):
                        label_names.add(name)
                elif isinstance(lbl, str):
                    label_names.add(lbl)
            if "approved" in label_names:
                continue
            if label_names & set(approval_labels):
                issues.append(issue)
    except Exception as exc:
        logger.warning("⚠️  approval_queue_partial: get_open_issues failed: %s", exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/approval_queue.html",
        {"issues": issues, "approved": False},
    )


@router.post("/issues/{repo}/{number}/approve", response_class=HTMLResponse)
async def approve_issue(request: Request, repo: str, number: int) -> HTMLResponse:
    """HTMX action: add the ``approved`` label to an issue.

    Ensures the ``approved`` label exists on the repo (idempotent), then adds
    it to the specified issue.  Returns a fragment that replaces the approval
    card with an "Approved" badge via ``hx-swap="outerHTML"``.

    Emits an ``HX-Trigger`` response header carrying a toast notification so
    the dashboard's global toast handler can surface confirmation to the user.

    The ``repo`` path segment is accepted for URL uniqueness in HTMX routing
    but the reader uses the globally configured repo from settings.
    """
    try:
        await ensure_label_exists(
            "approved",
            "2ea44f",
            "Human-approved for pipeline",
        )
        await add_label_to_issue(number, "approved")
        logger.info("✅ Issue #%d approved via UI", number)
    except Exception as exc:
        logger.warning("⚠️  approve_issue(%d) failed: %s", number, exc)

    hx_trigger = json.dumps(
        {"toast": {"message": f"Issue #{number} approved", "type": "success"}}
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "partials/approval_queue.html",
        {"approved": True, "issue_number": number},
        headers={"HX-Trigger": hx_trigger},
    )
