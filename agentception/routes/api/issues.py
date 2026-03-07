"""API routes: GitHub issue/PR HTMX partials (comments, CI checks, reviews)."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from starlette.requests import Request

from agentception.routes.ui._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_APPROVAL_LABELS: list[str] = ["db-schema", "security", "api-contract"]


@router.get("/issues/{org}/{repo}/{number}/comments")
async def issue_comments_partial(request: Request, org: str, repo: str, number: int) -> object:
    """HTMX partial: render comments for issue #{number} in {org}/{repo}.

    Lazily fetches from GitHub so the issue detail page loads without blocking.
    """
    from agentception.readers.github import get_issue_comments

    comments: list[dict[str, object]] = []
    try:
        comments = await get_issue_comments(number)
    except Exception as exc:
        logger.warning("⚠️  get_issue_comments(%d) failed: %s", number, exc)

    return _TEMPLATES.TemplateResponse(
        request,
        "partials/issue_comments.html",
        {"comments": comments},
    )


@router.get("/prs/{org}/{repo}/{number}/checks")
async def pr_checks_partial(request: Request, org: str, repo: str, number: int) -> object:
    """HTMX partial: render CI check statuses for PR #{number} in {org}/{repo}."""
    from agentception.readers.github import get_pr_checks

    checks: list[dict[str, object]] = []
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


@router.get("/prs/{org}/{repo}/{number}/reviews")
async def pr_reviews_partial(request: Request, org: str, repo: str, number: int) -> object:
    """HTMX partial: render review decisions for PR #{number} in {org}/{repo}."""
    from agentception.readers.github import get_pr_reviews

    reviews: list[dict[str, object]] = []
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


@router.get("/issues/approval-queue")
async def approval_queue_partial(request: Request) -> object:
    """HTMX partial: render the list of issues pending human approval.

    Fetches all open issues, retains those whose label set intersects the
    configured ``approval_required_labels``, and removes any that already
    carry the ``"approved"`` label.  Renders ``partials/approval_queue.html``
    so callers can embed it via ``hx-get`` with a polling trigger.
    """
    from agentception.readers.github import get_open_issues
    from agentception.readers.pipeline_config import read_pipeline_config

    approval_labels: list[str] = _DEFAULT_APPROVAL_LABELS
    try:
        config = await read_pipeline_config()
        approval_labels = config.approval_required_labels
    except Exception as exc:
        logger.warning("⚠️  Could not read pipeline config for approval labels: %s", exc)

    issues: list[dict[str, object]] = []
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


@router.post("/issues/{org}/{repo}/{number}/approve")
async def approve_issue(request: Request, org: str, repo: str, number: int) -> object:
    """HTMX action: add the ``approved`` label to an issue.

    Ensures the ``approved`` label exists on the repo (idempotent), then adds
    it to the specified issue.  Returns a fragment that replaces the approval
    card with an "Approved" badge via ``hx-swap="outerHTML"``.

    Emits an ``HX-Trigger`` response header carrying a toast notification so
    the dashboard's global toast handler can surface confirmation to the user.
    """
    from agentception.readers.github import add_label_to_issue, ensure_label_exists

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
