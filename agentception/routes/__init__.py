"""Routes sub-package for AgentCeption.

Split into two sub-packages, each further decomposed into domain modules:

``ui/``  — HTML pages rendered via Jinja2 templates.
  Modules: overview, agents, telemetry, dag, config, ab_testing,
           brain_dump, roles_ui, github_ui, transcripts, worktrees,
           docs, api_reference, templates_ui.
  Shared: _shared.py (_TEMPLATES singleton, helper functions).

``api/``  — JSON endpoints consumed by HTMX and future clients.
  Modules: pipeline, control, config, intelligence, telemetry,
           worktrees, issues.
  Shared: _shared.py (_SENTINEL, _build_agent_task, helpers).

Both routers are registered in ``app.py`` via ``app.include_router()``.
Additional standalone routers (control, intelligence, roles, templates_api)
are registered directly from their own modules and are not part of these
two sub-packages.
"""
from __future__ import annotations
