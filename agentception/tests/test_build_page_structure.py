from __future__ import annotations

"""Regression tests for build.html DOM structure.

Invariant: id="initiative-tabs-nav" must never be a descendant of
id="build-board". If it is, every 5-second board swap re-inserts a
fresh hx-trigger="load" element, causing an infinite reload loop.
"""

from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest


TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


class _StubRequest:
    """Minimal request stub so base.html's request.url.path checks don't fail."""

    class _URL:
        path = "/ship/agentception/test-initiative"

    url = _URL()


def _render(template_name: str, ctx: dict) -> str:  # type: ignore[type-arg]
    """Render a Jinja2 template with a minimal stub context."""
    from urllib.parse import quote
    import json

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    env.filters["urlencode"] = lambda s: quote(str(s), safe="")
    env.filters["tojson"] = lambda v: json.dumps(v)
    env.filters["replace"] = lambda s, old, new: str(s).replace(old, new)
    env.filters["title"] = lambda s: str(s).title()
    env.filters["truncate"] = lambda s, l, k, e: str(s)[:l]

    full_ctx = {"request": _StubRequest(), **ctx}
    tmpl = env.get_template(template_name)
    return tmpl.render(full_ctx)


class _AncestorTracker(HTMLParser):
    """Track whether element B is ever a descendant of element A by id."""

    def __init__(self, ancestor_id: str, descendant_id: str) -> None:
        super().__init__()
        self._ancestor_id = ancestor_id
        self._descendant_id = descendant_id
        self._depth_stack: list[tuple[str, str | None]] = []  # (tag, id)
        self._inside_ancestor = 0
        self.found_descendant_inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        elem_id = attr_dict.get("id")
        if elem_id == self._ancestor_id:
            self._inside_ancestor += 1
        if self._inside_ancestor > 0 and elem_id == self._descendant_id:
            self.found_descendant_inside = True
        self._depth_stack.append((tag, elem_id))

    def handle_endtag(self, tag: str) -> None:
        # Pop matching open tag; track when we leave the ancestor
        for i in range(len(self._depth_stack) - 1, -1, -1):
            if self._depth_stack[i][0] == tag:
                _, elem_id = self._depth_stack.pop(i)
                if elem_id == self._ancestor_id:
                    self._inside_ancestor = max(0, self._inside_ancestor - 1)
                break


def _has_load_trigger(html: str) -> bool:
    """Return True if any element has hx-trigger containing 'load'."""

    class _Checker(HTMLParser):
        found = False

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            for name, value in attrs:
                if name == "hx-trigger" and value and "load" in value:
                    self.found = True

    checker = _Checker()
    checker.feed(html)
    return checker.found


# ---------------------------------------------------------------------------
# Minimal stub context for build.html
# ---------------------------------------------------------------------------

_BUILD_CTX: dict = {  # type: ignore[type-arg]
    "repo": "cgcardona/agentception",
    "repo_name": "agentception",
    "initiative": "test-initiative",
    "initiatives": ["test-initiative"],
    "open_issues": 0,
    "total_issues": 0,
    "groups": [],
    "figures": [],
    "role_figure_map": {},
}


def test_initiatives_div_is_not_inside_build_board() -> None:
    """id='initiative-tabs-nav' must be a sibling of, not inside, id='build-board'.

    Scale assumption: single-page template; brute-force HTML parse is O(n) in
    template size and fast enough for CI.
    """
    html = _render("build.html", _BUILD_CTX)
    tracker = _AncestorTracker(ancestor_id="build-board", descendant_id="initiative-tabs-nav")
    tracker.feed(html)
    assert not tracker.found_descendant_inside, (
        "id='initiative-tabs-nav' is nested inside id='build-board'. "
        "This causes an infinite reload loop — every 5s board swap re-fires hx-trigger='load'."
    )


def test_build_board_partial_has_no_load_trigger() -> None:
    """_build_board.html must not contain any element with hx-trigger containing 'load'.

    If it does, the board swap target re-inserts a polling element on every
    5-second refresh, creating an infinite loop.
    """
    partial_path = TEMPLATES_DIR / "_build_board.html"
    raw = partial_path.read_text()
    assert not _has_load_trigger(raw), (
        "_build_board.html contains an element with hx-trigger='load'. "
        "This element would be re-inserted on every board swap, causing an infinite reload loop."
    )


def test_build_page_has_htmx_indicator() -> None:
    """build.html must contain an element with class 'htmx-indicator' for the resync spinner."""
    html = _render("build.html", _BUILD_CTX)
    assert "htmx-indicator" in html, (
        "build.html does not contain 'htmx-indicator'. "
        "The resync spinner must use the .htmx-indicator class so HTMX toggles it automatically."
    )


def test_build_page_no_hourglass_emoji() -> None:
    """build.html must not contain any hourglass Unicode characters."""
    html = _render("build.html", _BUILD_CTX)
    assert "\u231b" not in html, "build.html contains U+231B ⌛ hourglass character"
    assert "\u23f3" not in html, "build.html contains U+23F3 ⏳ hourglass character"
    assert "hourglass" not in html.lower(), "build.html contains the string 'hourglass'"
