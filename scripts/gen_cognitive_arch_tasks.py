#!/usr/bin/env python3
"""Generate and manage batched .agent-task files for cognitive architecture enrichment.

Subcommands:

    generate  Write one .agent-task file per figure batch (default).
    cleanup   Scan the tasks directory, record completed tasks to DB, delete their files.

Examples:

    python scripts/gen_cognitive_arch_tasks.py generate \\
        --out-dir ~/.agentception/tasks --repo /path/to/repo

    python scripts/gen_cognitive_arch_tasks.py cleanup \\
        --tasks-dir ~/.agentception/tasks --repo /path/to/repo

Task files are ephemeral — they live only until the agent commits.  The cleanup
subcommand deletes files for completed/failed tasks and optionally records a thin
row to ``ac_task_runs`` when ``DATABASE_URL`` is set.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Figures — ordered by thematic affinity so each batch is coherent
# ---------------------------------------------------------------------------

# Grouped for thematic coherence (keeps each agent's context tight)
FIGURE_BATCHES: list[tuple[str, list[str]]] = [
    (
        "systems-language-designers",
        [
            "linus_torvalds",
            "bjarne_stroustrup",
            "ritchie",
            "ken_thompson",
            "rob_pike",
            "graydon_hoare",
        ],
    ),
    (
        "language-inventors",
        [
            "guido_van_rossum",
            "james_gosling",
            "anders_hejlsberg",
            "matz",
            "brendan_eich",
            "joe_armstrong",
        ],
    ),
    (
        "algorithms-and-theory",
        [
            "knuth",
            "dijkstra",
            "hamming",
            "turing",
            "mccarthy",
            "leslie_lamport",
        ],
    ),
    (
        "ai-and-ml-pioneers",
        [
            "geoffrey_hinton",
            "yann_lecun",
            "ilya_sutskever",
            "andrej_karpathy",
            "fei_fei_li",
            "demis_hassabis",
        ],
    ),
    (
        "distributed-systems-and-infra",
        [
            "jeff_dean",
            "werner_vogels",
            "ryan_dahl",
            "martin_fowler",
            "kent_beck",
            "dhh",
        ],
    ),
    (
        "product-and-design",
        [
            "steve_jobs",
            "don_norman",
            "paul_graham",
            "patrick_collison",
            "jeff_bezos",
            "sam_altman",
        ],
    ),
    (
        "security-and-cryptography",
        [
            "bruce_schneier",
            "david_chaum",
            "hal_finney",
            "nick_szabo",
            "emin_gun_sirer",
            "vint_cerf",
        ],
    ),
    (
        "blockchain-and-web3",
        [
            "satoshi_nakamoto",
            "vitalik_buterin",
            "gavin_wood",
            "gabriel_cardona",
        ],
    ),
    (
        "science-and-physics",
        [
            "feynman",
            "einstein",
            "newton",
            "shannon",
            "linus_pauling",
            "von_neumann",
        ],
    ),
    (
        "leadership-and-strategy",
        [
            "peter_drucker",
            "andy_grove",
            "sun_tzu",
            "satya_nadella",
            "elon_musk",
            "bill_gates",
        ],
    ),
    (
        "pioneering-engineers",
        [
            "hopper",
            "lovelace",
            "margaret_hamilton",
            "barbara_liskov",
            "marie_curie",
            "darwin",
        ],
    ),
    (
        "web-and-platform-builders",
        [
            "tim_berners_lee",
            "fabrice_bellard",
            "john_carmack",
            "wozniak",
            "rich_hickey",
            "nassim_taleb",
        ],
    ),
    (
        "misc-and-interdisciplinary",
        [
            "da_vinci",
            "nikola_tesla",
            "carl_sagan",
        ],
    ),
]

SPEC_PATH = ".agentception/cognitive-arch-enrichment-spec.md"
FIGURES_DIR = "scripts/gen_prompts/cognitive_archetypes/figures"
# REPO_PATH and WORKTREES_BASE are derived at runtime from --repo and the
# resolved worktrees base, so no hardcoded project paths live here.
_DEFAULT_REPO_PATH = str(Path.home() / "dev" / "agentception")
_DEFAULT_WORKTREES_BASE = str(Path.home() / ".agentception" / "worktrees" / "agentception")


def _task_toml(batch_name: str, figures: list[str], repo: str) -> str:
    figure_list = ", ".join(figures)
    figure_files = "\n".join(f"  - {FIGURES_DIR}/{f}.yaml" for f in figures)
    verify_figure = figures[0]
    branch_name = f"agent/cog-arch-{batch_name}"
    worktree_path = f"{_DEFAULT_WORKTREES_BASE}/cog-arch-{batch_name}"

    briefing_body = (
        f"        ## Setup — create your worktree first\n\n"
        f"        REPO={_DEFAULT_REPO_PATH}\n"
        f"        WORKTREE={worktree_path}\n\n"
        f"        git -C $REPO worktree add $WORKTREE -b {branch_name}\n"
        f"        cd $WORKTREE\n\n"
        f"        All your file edits and git operations happen inside $WORKTREE.\n\n"
        f"        ## Your task\n\n"
        f"        Read the full spec at: $REPO/{SPEC_PATH}\n\n"
        f"        Your assigned figures for this batch ({batch_name}):\n"
        f"            {figure_list}\n\n"
        "        For each figure:\n"
        f"        1. Read its existing YAML at $WORKTREE/{FIGURES_DIR}/<id>.yaml\n"
        "        2. Enrich it to the gabriel_cardona.yaml quality bar (see spec)\n"
        "        3. Add top-level `heuristic` field (one governing sentence)\n"
        "        4. Add top-level `failure_modes` field (2-4 items with active compensations)\n"
        "        5. Deepen `prompt_injection.prefix` to 5+ concrete, figure-specific paragraphs\n"
        "        6. Expand `prompt_injection.suffix` to 5-8 behavioral checkpoints\n"
        "        7. Verify atom override values against $WORKTREE/scripts/gen_prompts/cognitive_archetypes/atoms/\n"
        "        8. Write the improved YAML back to disk (inside $WORKTREE)\n\n"
        "        After all figures are done, verify one assembles cleanly:\n"
        f"            python3 $WORKTREE/scripts/gen_prompts/resolve_arch.py \\\n"
        f"                {verify_figure}:python --mode implementer\n\n"
        "        Then commit inside the worktree:\n"
        f"            cd $WORKTREE\n"
        f"            git add {FIGURES_DIR}/\n"
        f'            git commit -m "feat(cognitive-arch): enrich {batch_name} figures"\n\n'
        "        Do NOT push. Commit only.\n"
    )

    header = (
        f"# .agent-task -- Cognitive Architecture Enrichment: {batch_name}\n"
        "# Generated by scripts/gen_cognitive_arch_tasks.py\n"
        "# Dispatch via AgentCeption or open in Cursor.\n"
    )

    task_block = (
        "\n[task]\n"
        f'id          = "cog-arch-{batch_name}"\n'
        f'title       = "Enrich cognitive architecture figures: {batch_name}"\n'
        'priority    = "high"\n'
        'role        = "technical-writer"\n'
        'cognitive_arch = "gabriel_cardona"\n'
    )

    context_block = (
        "\n[context]\n"
        f'repo        = "{repo}"\n'
        f'spec        = "$REPO/{SPEC_PATH}"\n'
        f'figures_dir = "$REPO/{FIGURES_DIR}"\n'
    )

    scope_block = (
        "\n[scope]\n"
        "# Figures assigned to this batch\n"
        f"figures = [{figure_list}]\n\n"
        "# Files you will read and modify\n"
        f"target_files = [\n{figure_files}\n]\n"
    )

    instructions_block = (
        "\n[instructions]\n"
        'briefing = """\n'
        + briefing_body
        + '"""\n'
    )

    constraints_block = (
        "\n[constraints]\n"
        '- "Do not invent biographical facts -- only assert what you are confident is documented."\n'
        '- "Do not change id, layer, or skill_domains primary/secondary."\n'
        '- "Do not add skill domains that do not exist in scripts/gen_prompts/cognitive_archetypes/skill_domains/."\n'
        '- "Keep all atom override values within valid options from the atom YAML files."\n'
        "- \"Prefix must start with '## Cognitive Architecture: <display_name>'.\"\n"
        '- "Commit to worktree branch only -- do not push."\n'
    )

    return header + task_block + context_block + scope_block + instructions_block + constraints_block


def _branch_commit_sha(repo: str, branch: str) -> str | None:
    """Return the tip SHA of a local branch, or None if it doesn't exist."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", branch],
            capture_output=True,
            text=True,
        )
        sha = result.stdout.strip()
        return sha if sha else None
    except Exception:
        return None


def _record_to_db(
    task_id: str,
    task_type: str,
    branch: str,
    commit_sha: str | None,
    payload: dict[str, object],
    status: str,
) -> bool:
    """Write a thin row to ac_task_runs via psycopg2 if DATABASE_URL is set.

    Returns True on success, False if DATABASE_URL is absent or insert fails.
    Intentionally synchronous — this script runs on the host, not inside a
    service, so there's no event loop to await.
    """
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("AC_DATABASE_URL")
    if not db_url:
        return False

    # Convert asyncpg URL to psycopg2-compatible URL for the host-side script.
    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        import psycopg2  # type: ignore[import-untyped]

        conn = psycopg2.connect(sync_url)
        cur = conn.cursor()
        now = datetime.now(tz=timezone.utc)
        completed_at = now if status in ("completed", "failed") else None
        cur.execute(
            """
            INSERT INTO ac_task_runs
                (id, task_type, branch, commit_sha, payload_json, status, created_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                status       = EXCLUDED.status,
                commit_sha   = EXCLUDED.commit_sha,
                completed_at = EXCLUDED.completed_at
            """,
            (
                task_id,
                task_type,
                branch,
                commit_sha,
                json.dumps(payload),
                status,
                now,
                completed_at,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as exc:
        print(f"⚠️  DB record skipped ({exc})")
        return False


def cmd_generate(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    for batch_name, figures in FIGURE_BATCHES:
        content = _task_toml(batch_name, figures, args.repo)
        out_path = out_dir / f"cog-arch-{batch_name}.agent-task"
        out_path.write_text(content)
        print(f"✅  {out_path}  ({len(figures)} figures)")

        # Record as pending in DB if available.
        _record_to_db(
            task_id=f"cog-arch-{batch_name}",
            task_type="cognitive-arch-enrichment",
            branch=f"agent/cog-arch-{batch_name}",
            commit_sha=None,
            payload={"batch": batch_name, "figures": figures},
            status="pending",
        )

    total = sum(len(f) for _, f in FIGURE_BATCHES)
    print(f"\n📦 {len(FIGURE_BATCHES)} task files written → {out_dir}")
    print(f"   {total} figures total across {len(FIGURE_BATCHES)} batches")
    print(
        "\nRun cleanup after agents commit:\n"
        f"  python3 scripts/gen_cognitive_arch_tasks.py cleanup --tasks-dir {out_dir} --repo {args.repo}"
    )


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Scan tasks dir, record completed tasks to DB, delete their files."""
    tasks_dir = Path(args.tasks_dir).expanduser()
    repo = args.repo

    if not tasks_dir.exists():
        print(f"⚠️  Tasks directory not found: {tasks_dir}")
        return

    task_files = sorted(tasks_dir.glob("*.agent-task"))
    if not task_files:
        print(f"✅  No task files found in {tasks_dir} — already clean.")
        return

    deleted = 0
    kept = 0

    for task_file in task_files:
        # Derive task ID and branch from filename.
        task_id = task_file.stem  # e.g. "cog-arch-systems-language-designers"
        branch = f"agent/{task_id}"  # e.g. "agent/cog-arch-systems-language-designers"

        sha = _branch_commit_sha(repo, branch)
        if sha:
            status = "completed"
            print(f"✅  {task_id}  → committed {sha[:8]}  (deleting file)")
            _record_to_db(
                task_id=task_id,
                task_type="cognitive-arch-enrichment",
                branch=branch,
                commit_sha=sha,
                payload={},
                status=status,
            )
            task_file.unlink()
            deleted += 1
        else:
            print(f"⏳  {task_id}  → no commit yet  (keeping file)")
            kept += 1

    print(f"\n🗑️  Deleted {deleted} completed task file(s), kept {kept} pending.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate and manage batched agent-task files for cognitive architecture enrichment.",
    )
    sub = parser.add_subparsers(dest="cmd")

    # --- generate subcommand (default) ---
    gen_p = sub.add_parser("generate", help="Write .agent-task files (default action).")
    gen_p.add_argument(
        "--out-dir",
        default="/tmp/cog-arch-tasks",
        help="Directory to write task files (default: /tmp/cog-arch-tasks)",
    )
    gen_p.add_argument(
        "--repo",
        default=_DEFAULT_REPO_PATH,
        help=f"Repo path to embed in tasks (default: {_DEFAULT_REPO_PATH})",
    )

    # --- cleanup subcommand ---
    clean_p = sub.add_parser("cleanup", help="Record completed tasks to DB and delete their files.")
    clean_p.add_argument(
        "--tasks-dir",
        default=str(Path.home() / ".agentception/tasks"),
        help="Directory containing .agent-task files (default: ~/.agentception/tasks)",
    )
    clean_p.add_argument(
        "--repo",
        default=_DEFAULT_REPO_PATH,
        help=f"Repo root for git branch checks (default: {_DEFAULT_REPO_PATH})",
    )

    args = parser.parse_args()

    if args.cmd == "cleanup":
        cmd_cleanup(args)
    else:
        # Default: generate (support both bare invocation and explicit subcommand)
        if args.cmd is None:
            # No subcommand given — treat remaining args as generate flags.
            gen_p.parse_args([], namespace=args)
            args.out_dir = getattr(args, "out_dir", "/tmp/cog-arch-tasks")
            args.repo = getattr(args, "repo", _DEFAULT_REPO_PATH)
        cmd_generate(args)


if __name__ == "__main__":
    main()
