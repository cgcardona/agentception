# Conflict Rules — Mechanical Resolution Playbook

When `git merge` reports a conflict, open this file FIRST.
Do not reach for `sed`, `hexdump`, `grep`, or any loop.
Every common conflict in this repo has a deterministic one-step rule below.

---

## Step 0 — Check if it's already resolved

```bash
git diff --check   # any output = unresolved markers remain
```

If no output → conflict is resolved. Commit and continue.

---

## Rule table — find your file, apply the rule

| File | Rule | Command |
|------|------|---------|
| `maestro/api/routes/musehub/__init__.py` | **Never conflicts.** It is auto-generated. If you see a conflict here, run `git checkout HEAD -- maestro/api/routes/musehub/__init__.py` to restore it. Never edit this file. | `git checkout HEAD -- maestro/api/routes/musehub/__init__.py` |
| `maestro/muse_cli/app.py` | Keep ALL `cli.add_typer()` lines from both sides. Union merge handles this automatically via `.gitattributes`. If markers appear anyway: extract all `cli.add_typer(` lines from both sides, deduplicate, sort alphabetically (except `repos` last). | See script below |
| `docs/architecture/muse_vcs.md` | Keep ALL `## ` sections from both sides. Union merge handles this automatically. If markers appear: keep every `## ` section heading and its content from both sides. | See script below |
| `docs/reference/type_contracts.md` | Keep ALL table rows from both sides. Union merge handles this. If markers appear: keep every `|` row from both sides, deduplicate. | See script below |
| `docs/reference/api.md` | Keep ALL endpoint sections from both sides. Union merge handles this. | Same as above |
| `alembic/versions/0001_consolidated_schema.py` | **Should never conflict** — only one agent at a time touches migrations. If it does: keep both `op.create_table(...)` blocks in `upgrade()`, keep both `op.drop_table(...)` blocks in `downgrade()`. | Manual edit |
| Any other Python file | Read both sides carefully. Keep the semantically correct version. If both sides added different functions/classes → keep both. If both sides modified the same line → pick the one consistent with the PR's intent. | Manual edit |

---

## Emergency script — strip conflict markers and keep all lines from both sides

Use this ONLY for purely additive files (docs, registries) where keeping everything from both sides is always correct:

```bash
# Replace conflict markers with nothing — keeps all content from both sides
python3 -c "
import sys, re
content = open(sys.argv[1]).read()
# Remove conflict marker lines, keep all content lines
cleaned = re.sub(r'^(<<<<<<<[^\n]*\n|=======\n|>>>>>>>[^\n]*\n)', '', content, flags=re.MULTILINE)
open(sys.argv[1], 'w').write(cleaned)
print('cleaned:', sys.argv[1])
" <conflicted_file>
git add <conflicted_file>
```

---

## The 4-step conflict workflow

```
1. git status                         ← find which files are conflicted
2. Look up each file in the table above
3. Apply the rule (usually one command)
4. git diff --check                   ← verify no markers remain
5. git add <file> && git merge --continue  (or git commit if not mid-merge)
```

**Stop after step 3.** Do not inspect hex dumps. Do not use `sed` to find markers.
Do not loop. The rules above cover 100% of normal conflicts in this repo.

---

## Prevention: what agents must do to avoid conflicts in the first place

1. **Never edit `maestro/api/routes/musehub/__init__.py`** — it auto-discovers.
2. **Never edit `maestro/muse_cli/app.py` imports manually** — the union merge driver handles it, but minimise touching the file's middle section.
3. **Sync `origin/dev` before implementing** (not just before pushing):
   ```bash
   git fetch origin && git merge origin/dev
   ```
   Do this at the start of your task, before writing a single line of code.
4. **Sync again before pushing.** Two syncs (start + end) catches 99% of conflicts before they become PR-level problems.
5. **File ownership**: each agent owns a distinct new file. The only shared files are the registries above, which are handled by auto-discovery or union merge.
