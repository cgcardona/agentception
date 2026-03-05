"""Assemble a live cognitive architecture context block from a COGNITIVE_ARCH string.

Usage (inside Docker container or on host):
    python3 scripts/gen_prompts/resolve_arch.py "<COGNITIVE_ARCH>" [--mode implementer|reviewer]

COGNITIVE_ARCH format:
    figures:skill1:skill2:...

    - figures: comma-separated figure/archetype ids (left-to-right blending)
    - skills:  colon-separated atomic skill domain ids

Examples:
    resolve_arch.py "lovelace:htmx:jinja2:alpine"
    resolve_arch.py "lovelace,shannon:htmx:jinja2:d3"
    resolve_arch.py "dijkstra:python:fastapi" --mode reviewer
    resolve_arch.py "the_guardian:python"

Output:
    Assembled Markdown context block printed to stdout.
    Intended for use in agent kickoff scripts:
        CONTEXT=$(python3 /app/scripts/gen_prompts/resolve_arch.py "$COGNITIVE_ARCH")

Exit codes:
    0 — success
    1 — COGNITIVE_ARCH string missing or invalid
    2 — referenced figure, archetype, or skill file not found
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _yaml = None  # noqa: F841 — only used via _YAML_AVAILABLE guard at runtime
    _YAML_AVAILABLE = False

SCRIPT_DIR = Path(__file__).parent
_YamlDict = dict[str, object]
ARCHETYPES_DIR = SCRIPT_DIR / "cognitive_archetypes"
FIGURES_DIR = ARCHETYPES_DIR / "figures"
ARCH_DIR = ARCHETYPES_DIR / "archetypes"
SKILLS_DIR = ARCHETYPES_DIR / "skill_domains"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_cognitive_arch(raw: str) -> tuple[list[str], list[str]]:
    """Split COGNITIVE_ARCH string into (figures, skills).

    Format: "figure1,figure2:skill1:skill2"
    The first colon-delimited token is treated as the figures part;
    everything after is individual skill ids.
    """
    raw = raw.strip()
    parts = raw.split(":")
    if not parts or not parts[0]:
        raise ValueError(f"Invalid COGNITIVE_ARCH (empty): {raw!r}")

    figures_raw = parts[0]
    skill_ids = [s.strip() for s in parts[1:] if s.strip()]
    figure_ids = [f.strip() for f in figures_raw.split(",") if f.strip()]

    if not figure_ids:
        raise ValueError(f"Invalid COGNITIVE_ARCH (no figures): {raw!r}")

    return figure_ids, skill_ids


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_figure_or_archetype(id_: str) -> _YamlDict:
    """Load a figure or archetype YAML file. Tries figures/ first, then archetypes/."""
    if not _YAML_AVAILABLE:
        raise ImportError("PyYAML is not installed in the host environment. Install pyyaml or run via docker compose exec agentception.")
    candidate_figure = FIGURES_DIR / f"{id_}.yaml"
    candidate_arch = ARCH_DIR / f"{id_}.yaml"

    for path in (candidate_figure, candidate_arch):
        if path.exists():
            data: object = _yaml.safe_load(path.read_text())
            if data is None:
                raise ValueError(f"Empty YAML file: {path}")
            if not isinstance(data, dict):
                raise ValueError(f"Expected mapping in {path}, got {type(data).__name__}")
            return data

    raise FileNotFoundError(
        f"Cannot find figure or archetype '{id_}' in:\n"
        f"  {candidate_figure}\n"
        f"  {candidate_arch}\n"
        "Check that the id matches a file in cognitive_archetypes/figures/ or archetypes/."
    )


def load_skill(id_: str) -> _YamlDict:
    """Load a skill domain YAML file from skill_domains/."""
    if not _YAML_AVAILABLE:
        raise ImportError("PyYAML is not installed in the host environment. Install pyyaml or run via docker compose exec agentception.")
    path = SKILLS_DIR / f"{id_}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Cannot find skill domain '{id_}' at:\n  {path}\n"
            "Check that the id matches a file in cognitive_archetypes/skill_domains/."
        )
    raw: object = _yaml.safe_load(path.read_text())
    if raw is None:
        raise ValueError(f"Empty skill domain YAML: {path}")
    if not isinstance(raw, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(raw).__name__}")
    data: _YamlDict = raw

    # Warn about deprecated skills
    if data.get("deprecated"):
        superseded = data.get("superseded_by", [])
        note = (
            f"⚠️  Skill '{id_}' is deprecated."
            + (f" Use: {', '.join(superseded)}" if isinstance(superseded, list) else "")
        )
        print(f"<!-- {note} -->", file=sys.stderr)

    return data


def _get_archetype_id_for_figure(figure_data: _YamlDict) -> str | None:
    """Return the archetype id a figure extends, if any."""
    val = figure_data.get("extends")
    return str(val) if val is not None else None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _injection(data: _YamlDict, key: str) -> str:
    """Extract prompt_injection.prefix or .suffix, returning '' if absent."""
    injection = data.get("prompt_injection", {})
    if not isinstance(injection, dict):
        return ""
    val = injection.get(key, "") or ""
    return str(val)


def _display(data: _YamlDict) -> str:
    val = data.get("display_name") or data.get("id") or "Unknown"
    return str(val)


def _load_atom_fragments(figure_data_list: list[tuple[str, _YamlDict]]) -> str:
    """Assemble prompt fragments for atom dimensions that any figure overrides.

    Walks every figure's ``overrides`` dict, loads the corresponding atom YAML
    from ``cognitive_archetypes/atoms/``, and injects the ``prompt_fragment``
    for the chosen value.  Left-to-right figure priority — first override wins
    per dimension.

    Returns a formatted markdown section, or '' when no atom files are found.
    """
    if not _YAML_AVAILABLE:
        return ""

    atoms_dir = ARCHETYPES_DIR / "atoms"
    if not atoms_dir.is_dir():
        return ""

    # Collect overrides left-to-right (first figure wins per dimension)
    resolved: dict[str, str] = {}
    for _fid, fdata in figure_data_list:
        overrides_raw = fdata.get("overrides", {})
        if not isinstance(overrides_raw, dict):
            continue
        for dim, val in overrides_raw.items():
            if str(dim) not in resolved:
                resolved[str(dim)] = str(val)

    if not resolved:
        return ""

    fragments: list[str] = []
    for dim, chosen_val in resolved.items():
        atom_path = atoms_dir / f"{dim}.yaml"
        if not atom_path.exists():
            continue
        try:
            atom_raw: object = _yaml.safe_load(atom_path.read_text())
            if not isinstance(atom_raw, dict):
                continue
            values_raw = atom_raw.get("values", {})
            if not isinstance(values_raw, dict):
                continue
            val_data = values_raw.get(chosen_val, {})
            if not isinstance(val_data, dict):
                continue
            fragment = str(val_data.get("prompt_fragment", "")).strip()
            if fragment:
                fragments.append(fragment)
        except Exception:
            continue  # Never hard-fail on an atom file

    if not fragments:
        return ""

    return "## Cognitive Dimensions\n\n" + "\n\n".join(fragments)


def _render_heuristic(figure_data_list: list[tuple[str, _YamlDict]]) -> str:
    """Return a formatted heuristic block from the primary figure, if present."""
    for _fid, fdata in figure_data_list:
        raw = fdata.get("heuristic")
        if raw:
            h = str(raw).strip()
            return f"**Governing heuristic:** *\"{h}\"*"
    return ""


def _render_failure_modes(figure_data_list: list[tuple[str, _YamlDict]]) -> str:
    """Return a formatted failure-modes block from all figures, if present."""
    all_modes: list[str] = []
    for _fid, fdata in figure_data_list:
        raw = fdata.get("failure_modes")
        if isinstance(raw, list):
            all_modes.extend(str(m).strip() for m in raw if m)
    if not all_modes:
        return ""
    bullet_list = "\n".join(f"- {m}" for m in all_modes)
    return f"## Your failure modes — compensate actively\n\n{bullet_list}"


def assemble(  # noqa: C901  (acceptable complexity for an assembler)
    figure_ids: list[str],
    skill_ids: list[str],
    mode: str = "implementer",
) -> str:
    """Assemble the full context block for an agent.

    Assembly order:
    1. Figure prefix(es) — left to right
    2. Governing heuristic — from the primary figure (if ``heuristic`` field present)
    3. Failure modes — from all figures (if ``failure_modes`` field present)
    4. Archetype prefix — from the primary (first) figure's ``extends`` field,
       if not already represented in figure_ids
    5. Atom fragments — prompt_fragment for each overriding atom dimension
    6. Skill sections — prompt_fragment (implementer) or review_checklist (reviewer)
    7. Figure suffix(es) — right to left
    8. Archetype suffix — same gating as (4)

    Multi-figure blending: prompts concatenated left-to-right.
    Atom conflicts resolved left-to-right (first wins per dimension).
    """
    sections: list[str] = []

    # Load all figures/archetypes
    figure_data_list = []
    for fid in figure_ids:
        data = load_figure_or_archetype(fid)
        figure_data_list.append((fid, data))

    # Load archetype for the primary figure (if not already in figure_ids)
    primary_arch_id = _get_archetype_id_for_figure(figure_data_list[0][1]) if figure_data_list else None
    arch_data = None
    if primary_arch_id and primary_arch_id not in figure_ids:
        try:
            arch_data = load_figure_or_archetype(primary_arch_id)
        except FileNotFoundError:
            pass  # Archetype optional — don't hard-fail

    # 1. Figure prefixes (left → right)
    for _fid, fdata in figure_data_list:
        prefix = _injection(fdata, "prefix")
        if prefix.strip():
            sections.append(prefix.rstrip())

    # 2. Governing heuristic (from primary figure)
    heuristic = _render_heuristic(figure_data_list)
    if heuristic:
        sections.append(heuristic)

    # 3. Failure modes (from all figures)
    failure_modes = _render_failure_modes(figure_data_list)
    if failure_modes:
        sections.append(failure_modes)

    # 4. Archetype prefix (if applicable)
    if arch_data:
        arch_prefix = _injection(arch_data, "prefix")
        if arch_prefix.strip():
            sections.append(arch_prefix.rstrip())

    # 5. Atom fragments (delta injection — only overriding dimensions)
    atom_section = _load_atom_fragments(figure_data_list)
    if atom_section:
        sections.append(atom_section)

    # 6. Skill sections
    if skill_ids:
        skill_section_key = "prompt_fragment" if mode == "implementer" else "review_checklist"
        loaded_skills = []
        for sid in skill_ids:
            sdata = load_skill(sid)
            loaded_skills.append((_display(sdata), sdata))

        if mode == "reviewer":
            sections.append("## Review Checklist")
            for dname, sdata in loaded_skills:
                checklist_raw = sdata.get("review_checklist", "") or ""
                checklist = str(checklist_raw)
                if checklist.strip():
                    sections.append(f"### {dname}\n\n{checklist.rstrip()}")
        else:
            for dname, sdata in loaded_skills:
                fragment_raw = sdata.get("prompt_fragment", "") or ""
                fragment = str(fragment_raw)
                if fragment.strip():
                    sections.append(fragment.rstrip())

    # 7. Figure suffixes (right → left)
    for _fid, fdata in reversed(figure_data_list):
        suffix = _injection(fdata, "suffix")
        if suffix.strip():
            sections.append(suffix.rstrip())

    # 8. Archetype suffix
    if arch_data:
        arch_suffix = _injection(arch_data, "suffix")
        if arch_suffix.strip():
            sections.append(arch_suffix.rstrip())

    if not sections:
        print(
            "⚠️  No content assembled for COGNITIVE_ARCH — figures/skills may have empty "
            "prompt_injection fields. Check the YAML files for this architecture.",
            file=sys.stderr,
        )
    return "\n\n---\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_display_names(
    figure_ids: list[str], skill_ids: list[str]
) -> tuple[str, str]:
    """Return (figures_display, skills_display) human-readable strings."""
    figure_labels: list[str] = []
    for fid in figure_ids:
        try:
            fdata = load_figure_or_archetype(fid)
            figure_labels.append(str(fdata.get("display_name", fid)))
        except FileNotFoundError:
            figure_labels.append(fid)
    skill_labels: list[str] = []
    for sid in skill_ids:
        try:
            sdata = load_skill(sid)
            skill_labels.append(str(sdata.get("display_name", sid)))
        except FileNotFoundError:
            skill_labels.append(sid)
    figures_str = " × ".join(figure_labels)
    skills_str = " · ".join(skill_labels) if skill_labels else "none"
    return figures_str, skills_str


def _normalize_arch_display(arch: str) -> str:
    """Normalize a COGNITIVE_ARCH string for display.

    Strips the 'the_' prefix from archetype names and removes underscores so
    figure IDs read as single clean tokens: the_guardian → guardian,
    von_neumann → vonneumann.  Skill tokens (after the first colon) are left
    unchanged — they have no underscores and are already idiomatic.
    """
    def norm(token: str) -> str:
        t = token.strip()
        if t.startswith("the_"):
            t = t[4:]
        return t.replace("_", "")

    parts = arch.split(":")
    parts[0] = ",".join(norm(f) for f in parts[0].split(","))
    return ":".join(parts)


def render_fingerprint(
    arch: str,
    role: str,
    session: str,
    batch: str,
    wave: str,
    coordinator: str,
    started_at: str = "",
) -> str:
    """Render the canonical agent fingerprint as a collapsible GitHub markdown block.

    This is the single source of truth for fingerprint format. All agents call
    this and embed the output verbatim — same block, same format, everywhere.

    Every fingerprint shows the full lineage chain:
      Role + Architecture (who the agent is)
      CTO Wave (which wave the CTO dispatched)
      Coordinator Batch (which batch the coordinator assembled)
      Coordinator (which coordinator identity spawned this agent)
      Timestamp (when this fingerprint was written — always present)

    Pass started_at (ISO-8601 string) to use a specific timestamp; otherwise
    the current UTC time is used so every fingerprint always carries a timestamp.
    """
    import datetime as _dt

    arch_display = _normalize_arch_display(arch)
    timestamp = started_at if started_at else _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = [
        f"| **Role** | `{role}` |",
        f"| **Architecture** | `{arch_display}` |",
        f"| **Session** | `{session}` |",
        f"| **CTO Wave** | `{wave}` |",
        f"| **Coordinator Batch** | `{batch}` |",
        f"| **Coordinator** | `{coordinator}` |",
        f"| **Timestamp** | `{timestamp}` |",
    ]

    lines = [
        "<details>",
        "<summary>🤖 Agent Fingerprint</summary>",
        "",
        "| | |",
        "|---|---|",
        *rows,
        "",
        "</details>",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "cognitive_arch",
        metavar="COGNITIVE_ARCH",
        help='Cognitive architecture string, e.g. "lovelace:htmx:jinja2:alpine"',
    )
    parser.add_argument(
        "--mode",
        choices=["implementer", "reviewer"],
        default="implementer",
        help="implementer: shows prompt_fragment (default). reviewer: shows review_checklist.",
    )
    parser.add_argument(
        "--fingerprint",
        action="store_true",
        help="Output a canonical GitHub fingerprint block instead of the prompt context.",
    )
    parser.add_argument("--role", default="unset", help="Agent role for fingerprint.")
    parser.add_argument("--session", default="unset", help="Agent session ID for fingerprint.")
    parser.add_argument("--batch", default="none", help="Coordinator batch ID for fingerprint.")
    parser.add_argument("--wave", default="unset", help="CTO wave ID for fingerprint.")
    parser.add_argument("--coordinator", default="unset", help="Coordinator fingerprint string.")
    parser.add_argument("--started-at", default="", help="ISO-8601 start timestamp (reviewer context).")
    args = parser.parse_args()

    if args.fingerprint:
        print(render_fingerprint(
            arch=args.cognitive_arch,
            role=args.role,
            session=args.session,
            batch=args.batch,
            wave=args.wave,
            coordinator=args.coordinator,
            started_at=args.started_at,
        ))
        return

    try:
        figure_ids, skill_ids = parse_cognitive_arch(args.cognitive_arch)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    if not _YAML_AVAILABLE:
        print("❌ PyYAML is not installed. Run: pip3 install pyyaml  OR use docker compose exec agentception python3 ...", file=sys.stderr)
        sys.exit(2)

    try:
        output = assemble(figure_ids, skill_ids, mode=args.mode)
    except (FileNotFoundError, ImportError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(2)

    # Identity banner → stderr only (never pollutes $CONTEXT variable)
    figures_str, skills_str = _resolve_display_names(figure_ids, skill_ids)
    print(f"🎭 [{args.mode}] {figures_str} | {skills_str}", file=sys.stderr)

    print(output, end="")


if __name__ == "__main__":
    main()
