"""Dependency DAG builder for the AgentCeption intelligence layer.

Parses every open issue body for ``Depends on #NNN`` patterns and assembles
a directed acyclic graph (DAG) of issue dependencies.  The DAG is the
foundation for the dashboard visualisation (AC-402) and feeds the scheduling
intelligence that prevents an agent from starting work before its dependencies
are merged.

Typical usage::

    from agentception.intelligence.dag import build_dag

    dag = await build_dag()
    for (src, dst) in dag.edges:
        print(f"#{src} depends on #{dst}")
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from agentception.intelligence.analyzer import parse_deps_from_body
from agentception.readers.github import get_open_issues

logger = logging.getLogger(__name__)

# Re-export so callers can import parse_deps_from_body from this module
# without knowing it lives in analyzer.py.
__all__ = ["IssueNode", "DependencyDAG", "build_dag", "parse_deps_from_body"]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class IssueNode(BaseModel):
    """A single issue node in the dependency DAG.

    Fields mirror the data available from the GitHub API.  ``deps`` lists the
    issue numbers that *this* issue depends on — i.e. work that must be
    completed before this issue can be started.
    """

    number: int
    title: str
    state: str          # "open" | "closed"
    labels: list[str]
    has_wip: bool
    deps: list[int]     # issue numbers this one depends on


class DependencyDAG(BaseModel):
    """Directed acyclic graph of open issue dependencies.

    ``edges`` encodes ``(from, to)`` pairs where *from* depends on *to*.
    Both ``from`` and ``to`` are GitHub issue numbers.  The ``nodes`` list
    contains one :class:`IssueNode` per open issue regardless of whether it
    participates in any edge.
    """

    nodes: list[IssueNode]
    edges: list[tuple[int, int]]   # (from, to) — from depends on to


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_dag() -> DependencyDAG:
    """Fetch all open issues, parse their dependencies, and return the DAG.

    Calls :func:`~agentception.readers.github.get_open_issues` once to
    retrieve every open issue with its body text, then applies
    :func:`parse_deps_from_body` to extract ``Depends on #N`` declarations.
    Closed issues are not included as nodes; edges that reference closed
    issues are still recorded (the referenced issue will simply not appear
    in ``nodes``).

    Returns
    -------
    DependencyDAG
        ``nodes`` — one per open issue; ``edges`` — one per dependency pair.

    Raises
    ------
    RuntimeError
        When the GitHub CLI subprocess exits with a non-zero status.
    """
    raw_issues = await get_open_issues()
    logger.debug("✅ build_dag: fetched %d open issues", len(raw_issues))

    nodes: list[IssueNode] = []
    edges: list[tuple[int, int]] = []

    for issue in raw_issues:
        number = int(issue["number"]) if isinstance(issue["number"], (int, str)) else 0
        title = str(issue.get("title", ""))
        state = str(issue.get("state", "open"))
        body = str(issue.get("body", "") or "")

        # Normalise labels: gh returns a list of label objects {"name": "...", ...}
        raw_labels = issue.get("labels", [])
        label_names: list[str] = []
        if isinstance(raw_labels, list):
            for lbl in raw_labels:
                if isinstance(lbl, dict):
                    name = lbl.get("name")
                    if isinstance(name, str):
                        label_names.append(name)
                elif isinstance(lbl, str):
                    label_names.append(lbl)

        has_wip = "agent:wip" in label_names
        deps = parse_deps_from_body(body)

        nodes.append(
            IssueNode(
                number=number,
                title=title,
                state=state,
                labels=label_names,
                has_wip=has_wip,
                deps=deps,
            )
        )

        for dep_number in deps:
            edges.append((number, dep_number))

    logger.info(
        "✅ build_dag: %d nodes, %d edges",
        len(nodes),
        len(edges),
    )
    return DependencyDAG(nodes=nodes, edges=edges)
