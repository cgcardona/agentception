"""Shared type definitions used across the AgentCeption codebase.

This module holds cross-cutting type aliases and TypedDicts that are
referenced by multiple packages (services, readers, routes, MCP, etc.).
"""

from __future__ import annotations

from typing import TypeAlias


# Recursive JSON-value union — the true runtime type of ``json.loads()``
# and ``yaml.safe_load()`` output.  Only use for genuinely dynamic JSON
# with unknown shape; known structures get their own TypedDict.
JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)

# A JSON Schema object (Draft-07 / OpenAPI 3.x compatible).  The schema
# spec is inherently recursive (``items``, ``properties`` values, ``allOf``
# members are all schemas themselves), so the value type is ``JsonValue``
# rather than a fixed TypedDict.
JsonSchemaObj: TypeAlias = dict[str, JsonValue]
