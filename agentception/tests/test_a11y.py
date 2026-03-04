from __future__ import annotations

"""Accessibility (a11y) smoke tests for AgentCeption templates.

Verifies that structural a11y requirements are present in the rendered HTML:
- base.html has the skip-link and main-content anchor
- modal templates include keydown.escape handlers

Run targeted:
    pytest agentception/tests/test_a11y.py -v
"""

import pathlib

import pytest


_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"


def _read(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text()


# ---------------------------------------------------------------------------
# base.html structural requirements
# ---------------------------------------------------------------------------


def test_base_has_skip_link_class() -> None:
    """base.html must contain a skip-link element for keyboard users."""
    content = _read("base.html")
    assert 'class="skip-link"' in content, (
        'base.html is missing <a class="skip-link"> skip-to-content anchor'
    )


def test_base_has_main_content_id() -> None:
    """base.html <main> must carry id='main-content' so the skip link target exists."""
    content = _read("base.html")
    assert 'id="main-content"' in content, (
        'base.html <main> is missing id="main-content"'
    )


def test_base_skip_link_points_to_main_content() -> None:
    """The skip link href must match the main-content anchor."""
    content = _read("base.html")
    assert 'href="#main-content"' in content, (
        'Skip link in base.html must use href="#main-content"'
    )


# ---------------------------------------------------------------------------
# Modal Escape-key handlers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "overview.html",
        "agent.html",
        "roles.html",
    ],
)
def test_modal_template_has_keydown_escape(template: str) -> None:
    """Every template that contains a modal must handle the Escape key."""
    content = _read(template)
    assert "keydown.escape" in content, (
        f"{template} has a modal but is missing @keydown.escape handler"
    )


# ---------------------------------------------------------------------------
# Modal click-outside (backdrop click) handlers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "overview.html",
        "agent.html",
        "roles.html",
    ],
)
def test_modal_template_has_click_self(template: str) -> None:
    """Modal backdrops must close on click outside (click.self)."""
    content = _read(template)
    assert "click.self" in content, (
        f"{template} has a modal backdrop but is missing @click.self handler"
    )


# ---------------------------------------------------------------------------
# ARIA on modal backdrops
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "overview.html",
        "agent.html",
        "roles.html",
    ],
)
def test_modal_template_has_aria_modal(template: str) -> None:
    """Modal elements must carry role='dialog' and aria-modal='true'."""
    content = _read(template)
    assert 'role="dialog"' in content, (
        f"{template} modal is missing role='dialog'"
    )
    assert 'aria-modal="true"' in content, (
        f"{template} modal is missing aria-modal='true'"
    )


# ---------------------------------------------------------------------------
# Spawn page — keyboard accessibility on interactive div elements
# ---------------------------------------------------------------------------


def test_spawn_issue_cards_have_tabindex() -> None:
    """Issue cards in spawn.html must be keyboard-reachable via tabindex."""
    content = _read("spawn.html")
    assert "tabindex" in content, (
        "spawn.html issue cards or role options are missing tabindex for keyboard navigation"
    )


def test_spawn_issue_cards_have_keydown_handler() -> None:
    """Issue cards must respond to Enter/Space for keyboard activation."""
    content = _read("spawn.html")
    assert "keydown.enter" in content, (
        "spawn.html is missing keydown.enter handler on interactive div elements"
    )


# ---------------------------------------------------------------------------
# Config page — tab widget ARIA completeness
# ---------------------------------------------------------------------------


def test_config_tab_buttons_have_ids() -> None:
    """Config sidebar tab buttons must have id attributes for aria-labelledby pairing."""
    content = _read("config.html")
    assert 'id="tab-btn-allocation"' in content
    assert 'id="tab-btn-labels"' in content
    assert 'id="tab-btn-ab"' in content
    assert 'id="tab-btn-projects"' in content


def test_config_panels_have_aria_labelledby() -> None:
    """Config tabpanels must reference their controlling button via aria-labelledby."""
    content = _read("config.html")
    assert 'aria-labelledby="tab-btn-allocation"' in content
    assert 'aria-labelledby="tab-btn-labels"' in content
    assert 'aria-labelledby="tab-btn-ab"' in content
    assert 'aria-labelledby="tab-btn-projects"' in content


# ---------------------------------------------------------------------------
# Brain Dump — horizontal step indicator (issue #827)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Batch context bar — base.html structural requirements (issue #80)
# ---------------------------------------------------------------------------


def test_base_has_batch_bar_element() -> None:
    """base.html must include the persistent batch context bar div."""
    content = _read("base.html")
    assert 'class="batch-bar"' in content, (
        "base.html is missing the .batch-bar persistent context strip"
    )


def test_base_batch_bar_uses_alpine_component() -> None:
    """The batch bar must be wired to the batchBar() Alpine component."""
    content = _read("base.html")
    assert 'x-data="batchBar()"' in content, (
        "base.html batch bar is missing x-data=\"batchBar()\""
    )


def test_base_batch_bar_hidden_when_empty() -> None:
    """The batch bar must use x-show=\"batchId\" to hide when no batch is active."""
    content = _read("base.html")
    assert 'x-show="batchId"' in content, (
        'base.html batch bar is missing x-show="batchId" guard'
    )


def test_base_batch_bar_has_dismiss_button() -> None:
    """The batch bar must include a dismiss (✕) button."""
    content = _read("base.html")
    assert 'batch-bar__dismiss' in content, (
        "base.html batch bar is missing the .batch-bar__dismiss button"
    )
    assert 'dismiss()' in content, (
        "base.html batch bar dismiss button is missing @click=\"dismiss()\" handler"
    )


def test_base_batch_bar_has_nav_links() -> None:
    """The batch bar must expose Plan, Build, and Ship navigation links."""
    content = _read("base.html")
    assert "batch-bar__link" in content, (
        "base.html batch bar is missing .batch-bar__link navigation anchors"
    )
    assert "'/build?initiative='" in content, (
        "base.html batch bar Build link is missing ?initiative= param"
    )
    assert "'/ship?batch='" in content, (
        "base.html batch bar Ship link is missing ?batch= param"
    )


def test_plan_stepper_present() -> None:
    """plan.html must include the horizontal step indicator nav element."""
    content = _read("plan.html")
    assert 'class="bd-stepper"' in content, (
        "plan.html is missing the .bd-stepper nav element"
    )


def test_plan_stepper_has_aria_label() -> None:
    """The stepper nav must carry aria-label='Progress' for screen readers."""
    content = _read("plan.html")
    assert 'aria-label="Progress"' in content, (
        "plan.html stepper is missing aria-label='Progress'"
    )


def test_plan_stepper_has_four_steps() -> None:
    """The stepper must render exactly four step labels: Plan, Preview, Running, Done."""
    content = _read("plan.html")
    for label in ("Plan", "Preview", "Running", "Done"):
        assert f">{label}<" in content, (
            f"plan.html stepper is missing step label '{label}'"
        )


def test_plan_stepper_driven_by_step_var() -> None:
    """Stepper classes must reference the existing Alpine 'step' variable, not new state."""
    content = _read("plan.html")
    assert "step === 'input'" in content, (
        "plan.html stepper must use the existing Alpine 'step' variable"
    )
    assert "step === 'loading'" in content, (
        "plan.html stepper must reference 'loading' state"
    )
    assert "step === 'done'" in content, (
        "plan.html stepper must reference 'done' state"
    )
