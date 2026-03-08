from __future__ import annotations

"""Shared TOML helpers for ``.agent-task`` file generation.

Both API routes (``routes/api/_shared.py``) and services (``services/spawn_child.py``)
need to emit TOML ``.agent-task`` files.  Keeping the helpers here prevents
either layer from importing the other (which would create a layering violation).
"""

# Supported scalar/collection types in a TOML .agent-task document.
type TomlValue = str | int | bool | list[str] | list[int]


def _escape_toml_str(s: str) -> str:
    """Escape backslashes and double-quotes for a TOML basic string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def toml_val(value: TomlValue) -> str:
    """Render a Python value as its TOML inline representation.

    Handles ``str`` (with multiline detection), ``bool`` (before ``int``, since
    ``bool ⊂ int``), ``int``, ``list[str]``, and ``list[int]``.  Multiline
    strings are emitted as TOML multiline basic strings so long bodies survive
    round-trips through ``tomllib`` without manual escaping.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if "\n" in value:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"""\n{escaped}\n"""'
        return f'"{_escape_toml_str(value)}"'
    # list[str] | list[int] — inline array
    parts: list[str] = []
    for item in value:
        if isinstance(item, int):
            parts.append(str(item))
        else:
            parts.append(f'"{_escape_toml_str(item)}"')
    return "[" + ", ".join(parts) + "]"


def render_toml_str(sections: dict[str, dict[str, TomlValue]]) -> str:
    """Render a dict of TOML sections into a valid TOML document string.

    Each key in *sections* becomes a ``[section]`` header.  Sections are
    separated by a blank line for readability.  No external dependency — this
    is a purpose-built minimal TOML emitter for the fixed ``.agent-task`` schema.
    """
    lines: list[str] = []
    for section, fields in sections.items():
        lines.append(f"[{section}]")
        for key, value in fields.items():
            lines.append(f"{key} = {toml_val(value)}")
        lines.append("")
    return "\n".join(lines)
