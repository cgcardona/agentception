from __future__ import annotations

"""Smoke tests confirming the ResizeObserver feature is present in dag.js and
survives esbuild bundling into the compiled app.js.

These are static assertions — no browser or DOM required.  They guard against
the feature being accidentally stripped by the bundler or removed during
refactoring.

Run targeted:
    pytest agentception/tests/test_dag_resize.py -v
"""
from __future__ import annotations

import pathlib

import pytest


_JS_DIR = pathlib.Path(__file__).parent.parent / "static" / "js"
_STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"


def _dag_js_source() -> str:
    return (_JS_DIR / "dag.js").read_text(encoding="utf-8")


def _compiled_app_js() -> str:
    path = _STATIC_DIR / "app.js"
    if not path.exists():
        pytest.skip("compiled app.js not found — run `npm run build` first")
    return path.read_text(encoding="utf-8")


def test_dag_js_contains_resize_observer() -> None:
    """dag.js source must reference ResizeObserver (signal-source for resize)."""
    assert "ResizeObserver" in _dag_js_source(), (
        "ResizeObserver not found in dag.js — was the feature removed?"
    )


def test_dag_js_contains_active_filter_latch() -> None:
    """dag.js source must declare the _activeFilter latch variable."""
    assert "_activeFilter" in _dag_js_source(), (
        "_activeFilter latch not found in dag.js — resize signal path is broken"
    )


def test_dag_js_contains_resize_debounce() -> None:
    """dag.js source must debounce resize events via _resizeTimer."""
    assert "_resizeTimer" in _dag_js_source(), (
        "_resizeTimer not found in dag.js — debounce low-pass filter is missing"
    )


def test_dag_js_observer_observes_svg() -> None:
    """dag.js must call _observer.observe on the dag-svg element."""
    source = _dag_js_source()
    assert "_observer.observe" in source, (
        "_observer.observe() call not found in dag.js"
    )
    assert "dag-svg" in source, (
        "'dag-svg' element id not found in dag.js"
    )


def test_compiled_app_js_contains_resize_observer() -> None:
    """Compiled app.js must still contain ResizeObserver after bundling.

    esbuild does not polyfill or strip browser-native APIs, so this confirms
    the feature was not accidentally removed from the source before the build.
    """
    assert "ResizeObserver" in _compiled_app_js(), (
        "ResizeObserver not found in compiled app.js — bundler may have "
        "dropped the feature or the build is stale"
    )
