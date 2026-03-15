from __future__ import annotations

"""Inspector panel regression tests.

Guards against accidental re-introduction of the chat textarea and Send
button that were removed in inspector-chat-removal-p0-001 / p0-002.
Also verifies that the Stop button — the only remaining agent control —
is still present in the rendered build page.
"""

from pathlib import Path

import jinja2

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


class _StubRequest:
    """Minimal request stub so base.html's request.url.path checks don't fail."""

    class _URL:
        path = "/ship/agentception/test-initiative"

    url = _URL()


def _render_build() -> str:
    """Render build.html with a minimal stub context."""
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
    env.filters["truncate"] = lambda s, length, killwords, end: str(s)[:length]

    ctx: dict[str, _StubRequest | str | int | bool | list[str] | dict[str, str]] = {
        "request": _StubRequest(),
        "repo": "cgcardona/agentception",
        "repo_name": "agentception",
        "initiative": "test-initiative",
        "initiatives": ["test-initiative"],
        "open_issues": 0,
        "total_issues": 0,
        "groups": [],
        "figures": [],
        "role_figure_map": {},
        "worktree_index_enabled": False,
    }
    tmpl = env.get_template("build.html")
    return tmpl.render(ctx)


# ---------------------------------------------------------------------------
# Absence assertions — chat textarea and Send button must not appear
# ---------------------------------------------------------------------------


def test_inspector_has_no_chat_textarea() -> None:
    """build.html must not contain a note/chat textarea in the inspector panel.

    The chat input was removed in inspector-chat-removal-p0-001.  Any
    re-introduction would be a regression.
    """
    html = _render_build()
    assert "build-inspector__chat-input" not in html, (
        "build.html contains 'build-inspector__chat-input'. "
        "The chat textarea was removed and must not be re-introduced."
    )
    # Belt-and-suspenders: also check the placeholder text that was used
    assert "Send a note to this agent" not in html, (
        "build.html contains the chat textarea placeholder text. "
        "The chat textarea was removed and must not be re-introduced."
    )


def test_inspector_has_no_send_button() -> None:
    """build.html must not contain a Send button for agent notes.

    The Send button was removed in inspector-chat-removal-p0-002.  Any
    re-introduction would be a regression.
    """
    html = _render_build()
    assert "build-inspector__chat-send" not in html, (
        "build.html contains 'build-inspector__chat-send'. "
        "The Send button was removed and must not be re-introduced."
    )


# ---------------------------------------------------------------------------
# Presence assertion — Stop button must remain
# ---------------------------------------------------------------------------


def test_inspector_stop_button_is_present() -> None:
    """build.html must still contain the Stop button for the agent controls.

    The Stop button is the only remaining agent control after the chat
    removal.  This test guards against accidental over-removal.
    """
    html = _render_build()
    assert "build-inspector__stop-btn" in html, (
        "build.html does not contain 'build-inspector__stop-btn'. "
        "The Stop button must remain as the sole agent control in the inspector."
    )
    assert "stopAgent()" in html, (
        "build.html does not contain the stopAgent() click handler. "
        "The Stop button must remain functional."
    )


# ---------------------------------------------------------------------------
# Inspector partial — also clean
# ---------------------------------------------------------------------------


def test_inspector_partial_has_no_textarea() -> None:
    """_inspector.html partial must not contain any textarea element."""
    partial_path = TEMPLATES_DIR / "_inspector.html"
    raw = partial_path.read_text()
    assert "<textarea" not in raw, (
        "_inspector.html contains a <textarea> element. "
        "The chat textarea was removed and must not appear in the inspector partial."
    )


def test_inspector_partial_has_no_send_button() -> None:
    """_inspector.html partial must not contain a Send button."""
    partial_path = TEMPLATES_DIR / "_inspector.html"
    raw = partial_path.read_text()
    # Check for any send-related class or text that would indicate a Send button
    assert "chat-send" not in raw, (
        "_inspector.html contains 'chat-send'. "
        "The Send button was removed and must not appear in the inspector partial."
    )
    assert "sendNote" not in raw, (
        "_inspector.html contains 'sendNote'. "
        "The Send button handler was removed and must not appear in the inspector partial."
    )
