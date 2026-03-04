from __future__ import annotations

"""Self-containment regression tests for agentception extraction readiness.

These tests verify that the agentception/ package is fully self-contained and
ready for extraction into a standalone repository.  They must all pass before
any extraction attempt.
"""

import re
import tomllib
from pathlib import Path

import pytest

# Root of the agentception package (one level up from this test file's directory)
AGENTCEPTION_ROOT = Path(__file__).parent.parent


def _all_python_files(exclude_self: bool = False) -> list[Path]:
    """Return all .py files under agentception/ (excluding __pycache__)."""
    this_file = Path(__file__).resolve()
    return [
        p
        for p in AGENTCEPTION_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
        and (not exclude_self or p.resolve() != this_file)
    ]


def test_no_maestro_imports_in_agentception() -> None:
    """No .py file in agentception/ may import from the maestro package.

    AgentCeption must be fully self-contained for standalone extraction.
    Cross-package imports from maestro.* would create an unresolvable
    dependency on the host monorepo after extraction.
    """
    violations: list[str] = []
    pattern = re.compile(r"^\s*(from maestro[.\s]|import maestro[.\s])", re.MULTILINE)

    for path in _all_python_files(exclude_self=False):
        source = path.read_text(encoding="utf-8")
        if pattern.search(source):
            violations.append(str(path.relative_to(AGENTCEPTION_ROOT.parent)))

    assert not violations, (
        "Found maestro.* imports in agentception/ — these must be removed before extraction:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_no_hardcoded_gabriel_paths() -> None:
    """No .py file in agentception/ may contain a literal hardcoded user path.

    Hardcoded user-specific paths break portability.  All paths must be
    derived from environment variables, Path.home(), or Path.cwd() at runtime.
    This test file is excluded from the scan since it inherently contains the
    pattern string for detection purposes.
    """
    violations: list[tuple[str, int, str]] = []
    _HARDCODED_PATH = "/Users/" + "gabriel/"  # split so this file doesn't self-match
    pattern = re.compile(re.escape(_HARDCODED_PATH))

    for path in _all_python_files(exclude_self=True):
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if pattern.search(line):
                violations.append((
                    str(path.relative_to(AGENTCEPTION_ROOT.parent)),
                    lineno,
                    line.strip(),
                ))

    assert not violations, (
        "Found hardcoded user-specific paths in agentception/ — make them configurable:\n"
        + "\n".join(f"  {v[0]}:{v[1]}: {v[2]}" for v in violations)
    )


def test_pyproject_toml_valid() -> None:
    """agentception/pyproject.toml must exist, be valid TOML, and contain required keys.

    Required structure:
      [project] name, version, requires-python, dependencies
      [project.scripts] agentception entrypoint
      [build-system] requires, build-backend
    """
    toml_path = AGENTCEPTION_ROOT / "pyproject.toml"
    assert toml_path.exists(), f"pyproject.toml not found at {toml_path}"

    with toml_path.open("rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    assert project.get("name") == "agentception", (
        f"[project].name must be 'agentception', got {project.get('name')!r}"
    )
    assert "version" in project, "[project].version is missing"
    assert "requires-python" in project, "[project].requires-python is missing"
    assert "dependencies" in project, "[project].dependencies is missing"
    assert isinstance(project["dependencies"], list), "[project].dependencies must be a list"
    assert len(project["dependencies"]) > 0, "[project].dependencies must not be empty"

    scripts = project.get("scripts", {})
    assert "agentception" in scripts, (
        "[project.scripts].agentception entry point is missing"
    )
    assert scripts["agentception"] == "agentception.app:main", (
        f"[project.scripts].agentception must point to 'agentception.app:main', "
        f"got {scripts['agentception']!r}"
    )

    build_system = data.get("build-system", {})
    assert "requires" in build_system, "[build-system].requires is missing"
    assert "build-backend" in build_system, "[build-system].build-backend is missing"
