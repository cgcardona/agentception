"""UI routes: GitHub issues and pull requests list/detail pages."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from agentception.config import settings as _settings
from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/issues", response_class=HTMLResponse)
async def issues_list(
    request: Request,
    state: str | None = None,
) -> HTMLResponse:
    """List all synced issues from the DB, filterable by state."""
    from agentception.db.queries import get_all_issues

    issues = await get_all_issues(repo=_settings.gh_repo, state=state)
    return _TEMPLATES.TemplateResponse(
        request,
        "issues_list.html",
        {"issues": issues, "state": state},
    )


@router.get("/issues/{number}", response_class=HTMLResponse)
async def issue_detail(request: Request, number: int) -> HTMLResponse:
    """Issue detail page — body, linked PRs, agent runs, and comments."""
    from agentception.db.queries import get_issue_detail

    issue = await get_issue_detail(repo=_settings.gh_repo, number=number)
    if issue is None:
        raise HTTPException(status_code=404, detail=f"Issue #{number} not found in DB")
    return _TEMPLATES.TemplateResponse(request, "issue.html", {"issue": issue})


@router.get("/prs", response_class=HTMLResponse)
async def prs_list(
    request: Request,
    state: str | None = None,
) -> HTMLResponse:
    """List all synced pull requests from the DB, filterable by state."""
    from agentception.db.queries import get_all_prs

    prs = await get_all_prs(repo=_settings.gh_repo, state=state)
    return _TEMPLATES.TemplateResponse(
        request,
        "prs_list.html",
        {"prs": prs, "state": state},
    )


@router.get("/prs/{number}", response_class=HTMLResponse)
async def pr_detail(request: Request, number: int) -> HTMLResponse:
    """PR detail page — CI checks, reviews, agent runs."""
    from agentception.db.queries import get_pr_detail

    pr = await get_pr_detail(repo=_settings.gh_repo, number=number)
    if pr is None:
        raise HTTPException(status_code=404, detail=f"PR #{number} not found in DB")
    return _TEMPLATES.TemplateResponse(request, "pr.html", {"pr": pr})
