"""UI routes: GitHub issues and pull requests list/detail pages."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from ._shared import _TEMPLATES

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/issues/{org}/{repo}", response_class=HTMLResponse)
async def issues_list(
    request: Request,
    org: str,
    repo: str,
    state: str | None = None,
) -> HTMLResponse:
    """List all synced issues for ``{org}/{repo}``, filterable by state."""
    from agentception.db.queries import get_all_issues

    gh_repo = f"{org}/{repo}"
    issues = await get_all_issues(repo=gh_repo, state=state)
    return _TEMPLATES.TemplateResponse(
        request,
        "issues_list.html",
        {"issues": issues, "state": state, "repo": gh_repo},
    )


@router.get("/issues/{org}/{repo}/{number}", response_class=HTMLResponse)
async def issue_detail(request: Request, org: str, repo: str, number: int) -> HTMLResponse:
    """Issue detail page — body, linked PRs, agent runs, and comments."""
    from agentception.db.queries import get_issue_detail

    gh_repo = f"{org}/{repo}"
    issue = await get_issue_detail(repo=gh_repo, number=number)
    if issue is None:
        raise HTTPException(status_code=404, detail=f"Issue #{number} not found in DB")
    return _TEMPLATES.TemplateResponse(request, "issue.html", {"issue": issue, "repo": gh_repo})


@router.get("/prs/{org}/{repo}", response_class=HTMLResponse)
async def prs_list(
    request: Request,
    org: str,
    repo: str,
    state: str | None = None,
) -> HTMLResponse:
    """List all synced pull requests for ``{org}/{repo}``, filterable by state."""
    from agentception.db.queries import get_all_prs

    gh_repo = f"{org}/{repo}"
    prs = await get_all_prs(repo=gh_repo, state=state)
    return _TEMPLATES.TemplateResponse(
        request,
        "prs_list.html",
        {"prs": prs, "state": state, "repo": gh_repo},
    )


@router.get("/prs/{org}/{repo}/{number}", response_class=HTMLResponse)
async def pr_detail(request: Request, org: str, repo: str, number: int) -> HTMLResponse:
    """PR detail page — CI checks, reviews, agent runs."""
    from agentception.db.queries import get_pr_detail

    gh_repo = f"{org}/{repo}"
    pr = await get_pr_detail(repo=gh_repo, number=number)
    if pr is None:
        raise HTTPException(status_code=404, detail=f"PR #{number} not found in DB")
    return _TEMPLATES.TemplateResponse(request, "pr.html", {"pr": pr, "repo": gh_repo})
