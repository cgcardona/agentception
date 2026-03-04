from __future__ import annotations

"""Tests for Jinja2 template globals injected by agentception/routes/ui/_shared.py.

Verifies that ``gh_repo`` and ``gh_base_url`` are registered on ``_TEMPLATES.env.globals``
and that their values are derived from :data:`agentception.config.settings` — the single
source of truth for the GitHub repo slug.

Run targeted:
    docker compose exec agentception pytest agentception/tests/test_template_globals.py -v
"""
from __future__ import annotations

from agentception.config import settings
from agentception.routes.ui._shared import _TEMPLATES


def test_gh_repo_global_injected() -> None:
    """``gh_repo`` must be present in the Jinja2 env globals."""
    assert "gh_repo" in _TEMPLATES.env.globals


def test_gh_repo_global_matches_settings() -> None:
    """``gh_repo`` global must equal ``settings.gh_repo`` — no hardcoded fallback."""
    assert _TEMPLATES.env.globals["gh_repo"] == settings.gh_repo


def test_gh_base_url_global_injected() -> None:
    """``gh_base_url`` must be present in the Jinja2 env globals."""
    assert "gh_base_url" in _TEMPLATES.env.globals


def test_gh_base_url_global_format() -> None:
    """``gh_base_url`` must start with ``https://github.com/``."""
    base_url: object = _TEMPLATES.env.globals["gh_base_url"]
    assert isinstance(base_url, str)
    assert base_url.startswith("https://github.com/")


def test_gh_base_url_contains_repo_slug() -> None:
    """``gh_base_url`` must embed the repo slug from settings."""
    base_url: object = _TEMPLATES.env.globals["gh_base_url"]
    assert isinstance(base_url, str)
    assert base_url == f"https://github.com/{settings.gh_repo}"


def test_no_hardcoded_cgcardona_maestro_in_globals() -> None:
    """Template globals must not contain a literal ``cgcardona/maestro`` that bypasses settings.

    If ``settings.gh_repo`` is overridden (e.g. via ``AC_GH_REPO`` env var), the globals
    must reflect the override — they must never be a static string burned in at import time
    independently of settings.
    """
    # The global value must equal settings.gh_repo, whatever settings says it is.
    # This guards against a regression where the global is hard-coded separately from settings.
    assert _TEMPLATES.env.globals["gh_repo"] == settings.gh_repo
    assert _TEMPLATES.env.globals["gh_base_url"] == f"https://github.com/{settings.gh_repo}"
