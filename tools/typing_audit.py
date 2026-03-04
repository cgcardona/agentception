#!/usr/bin/env python3
"""Typing audit — find and count all Any-ish patterns in the codebase.

Outputs JSON (machine-readable) + a human summary to stdout.

Usage:
    python tools/typing_audit.py                        # maestro/ + tests/ + storpheus/ + agentception/
    python tools/typing_audit.py --json artifacts/typing_audit.json
    python tools/typing_audit.py --dirs maestro/ storpheus/
    python tools/typing_audit.py --dirs agentception/ --max-any 10
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


# ── Pattern matchers ──────────────────────────────────────────────────────────

_PATTERNS: dict[str, re.Pattern[str]] = {
    "dict_str_any": re.compile(r"\bdict\[str,\s*Any\]|\bDict\[str,\s*Any\]", re.IGNORECASE),
    "list_any": re.compile(r"\blist\[Any\]|\bList\[Any\]", re.IGNORECASE),
    "cast_any": re.compile(r"\bcast\(\s*Any\b"),
    "return_any": re.compile(r"->\s*Any\b"),
    "param_any": re.compile(r":\s*Any\b"),
    "type_ignore": re.compile(r"#\s*type:\s*ignore"),
    "mapping_any": re.compile(r"\bMapping\[str,\s*Any\]", re.IGNORECASE),
    "optional_any": re.compile(r"\bOptional\[Any\]", re.IGNORECASE),
    "sequence_any": re.compile(r"\bSequence\[Any\]|\bIterable\[Any\]", re.IGNORECASE),
    "tuple_any": re.compile(r"\btuple\[.*Any.*\]|\bTuple\[.*Any.*\]"),
}


def _count_pattern_in_line(line: str, pattern: re.Pattern[str]) -> int:
    return len(pattern.findall(line))


def _imports_any(source: str) -> bool:
    """Check if file imports Any from typing."""
    return bool(re.search(r"from\s+typing\s+import\s+.*\bAny\b", source))


def _classify_type_ignores(line: str) -> str:
    """Return the ignore variant (blanket vs specific)."""
    m = re.search(r"#\s*type:\s*ignore\[([^\]]+)\]", line)
    if m:
        return f"type_ignore[{m.group(1)}]"
    return "type_ignore[blanket]"


# ── AST-based detection ──────────────────────────────────────────────────────


def _find_untyped_defs(source: str, filepath: str) -> list[dict[str, Any]]:
    """Find function defs missing return type or param annotations."""
    results: list[dict[str, Any]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return results

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is None:
                results.append({
                    "file": filepath,
                    "line": node.lineno,
                    "name": node.name,
                    "issue": "missing_return_type",
                })
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.annotation is None and arg.arg != "self" and arg.arg != "cls":
                    results.append({
                        "file": filepath,
                        "line": node.lineno,
                        "name": f"{node.name}.{arg.arg}",
                        "issue": "missing_param_type",
                    })
    return results


# ── File scanner ──────────────────────────────────────────────────────────────


def scan_file(filepath: Path) -> dict[str, Any]:
    """Scan a single Python file for Any-ish patterns."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    lines = source.splitlines()
    result: dict[str, Any] = {
        "file": str(filepath),
        "imports_any": _imports_any(source),
        "patterns": defaultdict(int),
        "pattern_lines": defaultdict(list),
        "type_ignore_variants": defaultdict(int),
        "untyped_defs": [],
    }

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        for name, pattern in _PATTERNS.items():
            count = _count_pattern_in_line(line, pattern)
            if count > 0:
                result["patterns"][name] += count
                result["pattern_lines"][name].append(lineno)

                if name == "type_ignore":
                    variant = _classify_type_ignores(line)
                    result["type_ignore_variants"][variant] += 1

    result["untyped_defs"] = _find_untyped_defs(source, str(filepath))
    return result


def scan_directory(directory: Path) -> list[dict[str, Any]]:
    """Scan all Python files in a directory tree."""
    results: list[dict[str, Any]] = []
    for py_file in sorted(directory.rglob("*.py")):
        if "venv" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        if ".git" in py_file.parts:
            continue
        file_result = scan_file(py_file)
        if file_result:
            results.append(file_result)
    return results


# ── Report generation ─────────────────────────────────────────────────────────


def generate_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate aggregate report from scan results."""
    totals: dict[str, int] = defaultdict(int)
    files_with_any_import = 0
    per_file: dict[str, dict[str, int]] = {}
    top_offenders: list[dict[str, Any]] = []
    all_type_ignore_variants: dict[str, int] = defaultdict(int)
    all_untyped_defs: list[dict[str, Any]] = []

    for r in results:
        filepath = r["file"]
        if r.get("imports_any"):
            files_with_any_import += 1

        file_total = 0
        file_patterns: dict[str, int] = {}
        for pattern, count in r.get("patterns", {}).items():
            totals[pattern] += count
            file_patterns[pattern] = count
            file_total += count

        if file_total > 0:
            per_file[filepath] = file_patterns
            top_offenders.append({"file": filepath, "total": file_total, "patterns": file_patterns})

        for variant, count in r.get("type_ignore_variants", {}).items():
            all_type_ignore_variants[variant] += count

        all_untyped_defs.extend(r.get("untyped_defs", []))

    top_offenders.sort(key=lambda x: x["total"], reverse=True)

    return {
        "summary": {
            "total_files_scanned": len(results),
            "files_importing_any": files_with_any_import,
            "total_any_patterns": sum(totals.values()),
            "untyped_defs": len(all_untyped_defs),
        },
        "pattern_totals": dict(totals),
        "type_ignore_variants": dict(all_type_ignore_variants),
        "top_offenders": top_offenders[:30],
        "per_file": per_file,
        "untyped_defs": all_untyped_defs[:50],
    }


def print_human_summary(report: dict[str, Any]) -> None:
    """Print a human-readable summary."""
    s = report["summary"]
    print("\n" + "=" * 70)
    print("  TYPING AUDIT — Any Usage Report")
    print("=" * 70)
    print(f"  Files scanned:        {s['total_files_scanned']}")
    print(f"  Files importing Any:  {s['files_importing_any']}")
    print(f"  Total Any patterns:   {s['total_any_patterns']}")
    print(f"  Untyped defs:         {s['untyped_defs']}")
    print()
    print("  Pattern breakdown:")
    for pattern, count in sorted(report["pattern_totals"].items(), key=lambda x: -x[1]):
        print(f"    {pattern:30s} {count:5d}")
    print()
    if report["type_ignore_variants"]:
        print("  # type: ignore variants:")
        for variant, count in sorted(report["type_ignore_variants"].items(), key=lambda x: -x[1]):
            print(f"    {variant:40s} {count:5d}")
        print()
    print("  Top 15 offenders:")
    for entry in report["top_offenders"][:15]:
        print(f"    {entry['total']:4d}  {entry['file']}")
    print("=" * 70 + "\n")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Any usage in the codebase")
    parser.add_argument(
        "--dirs",
        nargs="+",
        default=["maestro/", "tests/", "storpheus/", "agentception/"],
        help="Directories to scan",
    )
    parser.add_argument("--json", type=str, help="Write JSON report to file")
    parser.add_argument(
        "--max-any",
        type=int,
        default=None,
        help="Fail (exit 1) if total Any patterns exceed this threshold",
    )
    args = parser.parse_args()

    all_results: list[dict[str, Any]] = []
    for d in args.dirs:
        p = Path(d)
        if p.exists():
            all_results.extend(scan_directory(p))
        else:
            print(f"WARNING: {d} does not exist, skipping", file=sys.stderr)

    report = generate_report(all_results)
    print_human_summary(report)

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(report, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"  JSON report written to {args.json}")

    if args.max_any is not None:
        total = report["summary"]["total_any_patterns"]
        if total > args.max_any:
            print(
                f"\n❌ RATCHET FAILED: {total} Any patterns exceed "
                f"threshold of {args.max_any}",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            print(
                f"\n✅ RATCHET OK: {total} Any patterns within "
                f"threshold of {args.max_any}",
            )


if __name__ == "__main__":
    main()
