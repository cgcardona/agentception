# Agent Command Policy — Tier Classification

This document defines which shell commands agents may run without confirmation,
which require caution, and which are forbidden. Every parallel-agent workflow
reads this policy before executing any command.

> **Why this exists:** With 5–8 agents running concurrently, per-command
> confirmation dialogs create severe friction. This policy eliminates that
> friction for safe commands while preserving human oversight for destructive ones.

---

## How Cursor's Three Tiers Actually Work

Cursor does not have a formal graylist or blacklist. Here is how the three tiers
map to Cursor's actual mechanisms:

| Policy tier | Cursor mechanism | What happens |
|-------------|-----------------|--------------|
| **Green** | Command Allowlist (UI) | Runs immediately, no prompt, outside sandbox |
| **Yellow** | Auto-sandbox (not on allowlist) | Runs automatically in sandbox; if sandbox restriction tripped → one-time prompt |
| **Red** | Agent behavioral rule (not Cursor-enforced) | Agent reads this doc and refuses; sandbox prompts if a violation reaches it |

**Key implication:** The sandbox does not block specific command patterns — it restricts
filesystem access (outside workspace) and network access (controlled by sandbox.json).
Red-tier commands within the workspace are blocked by agent reasoning, not by Cursor.
This is why agents MUST read this document and follow it.

---

## Cursor Configuration (one-time setup)

### External-File Protection — must be OFF

`Cmd+Shift+J` → Cursor Settings → Agents → **External-File Protection → disable**

Parallel agents edit files inside git worktrees at `~/.agentception/worktrees/`, which are
outside the main workspace. With External-File Protection on, every file edit in a
worktree triggers a "Blocked / Allow edits to external files" prompt. Turn it off once.

### Auto-Run Mode

`Cmd+Shift+J` → Cursor Settings → Agents → Auto-Run → **Auto-Run in Sandbox**

This is the correct setting. Green-tier commands are on the allowlist and run
immediately. Yellow-tier commands run in the sandbox without prompting unless
they trip a restriction. Red-tier commands that reach the sandbox will prompt.

### Workspace sandbox.json (already committed)

`.cursor/sandbox.json` grants the sandbox:
- Docker socket access (`/var/run/docker.sock`) — all `docker compose exec` commands
- Worktrees directory (`~/.agentception/worktrees`) — parallel agent workflows
- Full outbound network — GitHub CLI, Docker Hub, OpenRouter, pypi

### Command Allowlist — paste this into the Cursor UI text field

Clear the existing allowlist entirely, then paste the block below into
**Cursor Settings → Agents → Auto-Run → Command Allowlist**.

> **IMPORTANT:** Paste this as a **single unbroken line** — no line breaks.
> Cursor strips newlines and treats them as spaces, merging adjacent entries
> into one broken token. The comma is the only separator Cursor respects.
>
> **Re-paste whenever this file changes.** If agents are still prompted for commands
> that appear in this list, your Cursor settings are out of date — clear and re-paste.
>
> **Backslash continuations trigger prompts.** Cursor treats the continuation line
> (`"PYTHONPATH=..."`) as a separate, unrecognized command. All multi-line shell
> commands in kickoff prompts must be written as single lines.

```
ls, ls -la, ls -lah, ls -l, pwd, cat, head, tail, echo, print, true, false, seq, wc, file, stat, which, date, basename, dirname, printf, jq, sort, uniq, tr, awk, sed, perl, cut, xargs, tee, rg, grep, find, source, mkdir, cp, mv, rm, touch, ln -s, rsync, rsync -a, cd, sleep, break, continue, exit, exit 0, exit 1, mapfile, read, IFS=, python3, python3 -c, python3 -m, open, check_issue_deps, check_deps, base64, openssl, REPO=, WTNAME=, DEV_SHA=, WT=, NUM=, TITLE=, BRANCH=, PRTREES=, WORKTREE=, ENTRY=, FOLLOW_UP_URL=, ISSUE_URL=, GH_REPO=, export GH_REPO=, declare -a, declare -A, declare -a ISSUES=, declare -a PRS=, for entry in, for NUM in, for WT in, for i in, for label in, for n in, N=$(grep, N=$(printf, BRANCH=$(grep, MERGE_AFTER=$(grep, LABELS_TO_APPLY=$(grep, CLOSES_ISSUES=$(grep, STATE=$(gh, PR_BRANCH=$(gh, PR_FILES=$(gh, CLOSES_ISSUE=$(gh, OPEN_PRS=$(gh, REMAINING=$(gh, NUM=$(echo, TITLE=$(echo, LABELS=$(echo, PHASE=$(echo, BATCH=$(echo, SHORT=$(grep, git, gh, docker compose, docker compose ps, docker compose logs, docker compose config, docker compose -f, docker ps, docker inspect, docker logs, docker exec, docker compose exec agentception mypy, docker compose exec agentception pytest, docker compose exec agentception sh -c, docker compose exec agentception python -m coverage, docker compose exec agentception python -c, docker compose exec agentception python3 -m, docker compose exec agentception ps, docker compose exec agentception ls, docker compose exec agentception cat, docker compose exec agentception sed, docker compose exec agentception head, docker compose exec agentception tail, docker compose exec agentception wc, docker compose exec agentception awk, docker compose exec agentception rg, docker compose exec agentception grep, docker compose exec agentception find, docker compose exec agentception alembic history, docker compose exec agentception alembic current, docker compose exec agentception alembic heads, docker compose exec agentception alembic upgrade head, docker compose exec agentception python3, docker compose exec agentception mypy, docker compose exec agentception pytest, docker compose exec agentception sh -c, docker compose exec agentception ls, docker compose exec agentception cat, docker compose exec agentception rg, docker compose exec agentception grep, docker compose exec agentception python3, docker compose exec agentception mypy, docker compose exec agentception pytest, docker compose exec agentception sh -c, docker compose exec agentception ls, docker compose exec agentception cat, docker compose exec agentception rg, docker compose exec agentception grep, docker compose exec agentception python3, docker compose exec agentception python3 -m, docker compose exec agentception python -c, docker compose exec agentception tail, docker compose exec agentception head, docker compose exec agentception wc, docker compose exec postgres psql, npm run, npm ci, npx, sass, brew install, pip3, ps aux, ps -ef, pgrep, nc -z, curl, REPO=$(git, WTNAME=$(basename, DEV_SHA=$(git, WT=$HOME, BRANCH=$(git, cd $HOME/.cursor, cd $HOME/dev
```

---

## Tier 1 — Green (On the Allowlist — runs immediately, no prompt)

### Shell — Read / Inspect
```
ls / ls -la / ls -lah / ls -l
pwd
cat <file>
head -n N <file>              ← file inspection ONLY (never to filter test output)
tail -n N <file>              ← file inspection ONLY (never to filter test output)
echo
print                         ← zsh shell builtin (equivalent to echo); also appears as Python builtin
                              ← token when Cursor parses multi-line agent scripts or Python heredocs
true                          ← null-op; used as || true for safe error suppression in scripts
false                         ← null-op complement; used in boolean control flow
seq <N>                       ← integer sequence; used in merge-gate polling loops: for i in $(seq 1 15)
wc / wc -l
file <path>
stat <path>                   ← file/directory existence and metadata checks
which <cmd>
date
basename / dirname
printf
jq <filter> <file>            ← JSON parsing
sleep <N>                     ← harmless pause; agents use between push and gh pr create
```

### Shell — Worktree cd prefixes (combined `cd /path && docker compose ...` commands)

> **Personalize these entries before pasting the allowlist.**
> Replace `$HOME` below with your actual home directory path (e.g. `/Users/yourname`),
> since Cursor's allowlist does not expand shell variables — literal paths only.

```
cd /Users/<you>/.cursor   ← worktree-relative combined commands (e.g. cd $WORKTREE && docker compose exec ...)
cd /Users/<you>/dev       ← main workspace combined commands
```

### Shell — Bash control flow (coordinator setup scripts and polling loops)
```
break                         ← bash loop exit; used in merge-gate polling loops
continue                      ← bash loop skip; used in for loops
exit / exit 0 / exit 1        ← script exit; used in merge-gate escalation and idempotency gates
mapfile -t <ARRAY> < <(...)   ← bash builtin for reading command output into an array
read                          ← bash builtin; used in IFS-split and while-read loops
IFS=',' read -ra <ARRAY> <<< ← bash IFS-split; used to iterate LABELS_TO_APPLY and similar CSV fields
source <file>                 ← load env vars from /tmp/ files (e.g. qa_batch_env, agent-task exports)
check_issue_deps              ← coordinator helper; checks issue dependency gates before merging
check_deps                    ← agentception helper; checks dependency gates before claiming an issue
declare -a ISSUES=(...)       ← indexed array declaration in coordinator setup scripts
declare -a PRS=(...)          ← indexed array declaration in coordinator setup scripts
declare -A <MAP>=(...)        ← associative array declaration; used in coordinator setup scripts
for entry in "${ARRAY[@]}"    ← iteration over indexed arrays
for i in $(seq 1 <N>)        ← counted loop; used in merge-gate polling
for label in "${LABELS[@]}"  ← iteration over label arrays (label pre-flight scripts)
for NUM in <list>             ← iteration over explicit issue/PR number lists
for n in <list>               ← iteration over explicit number lists
PRTREES=<path>                ← variable assignment
WORKTREE=<path>               ← variable assignment
ENTRY=<value>                 ← variable assignment
FOLLOW_UP_URL=<value>         ← B-grade follow-up issue URL
ISSUE_URL=<value>             ← captured issue URL in scripts
GH_REPO=cgcardona/agentception     ← hardcoded repo slug — NEVER derive from local path
export GH_REPO=cgcardona/agentception
```

### Shell — Variable assignments from command substitution (task-file parsing)
These patterns appear in every agent kickoff when parsing `.agent-task` KEY=value fields.
Cursor treats each `VAR=$(cmd)` prefix as a command token — all must be on the allowlist.
```
N=$(grep                      ← parse PR_NUMBER / ISSUE_NUMBER from .agent-task
N=$(printf                    ← zero-pad batch number: N=$(printf "%02d" $i)
BRANCH=$(grep                 ← parse PR_BRANCH from .agent-task
MERGE_AFTER=$(grep            ← parse MERGE_AFTER gate from .agent-task
LABELS_TO_APPLY=$(grep        ← parse comma-separated label list from .agent-task
CLOSES_ISSUES=$(grep          ← parse linked issue numbers from .agent-task
STATE=$(gh                    ← capture live PR/issue state: STATE=$(gh pr view ...)
PR_BRANCH=$(gh                ← fetch PR branch name from GitHub
PR_FILES=$(gh                 ← fetch PR file list from GitHub
CLOSES_ISSUE=$(gh             ← fetch close-links from PR body
OPEN_PRS=$(gh                 ← list open PRs for overlap check
REMAINING=$(gh                ← count remaining open issues in phase closure audit
NUM=$(echo                    ← extract issue/PR number from pipe: NUM="${entry%%|*}"
TITLE=$(echo                  ← extract title from pipe: TITLE="${entry##*|}"
LABELS=$(echo                 ← extract label string from pipe output
PHASE=$(echo                  ← extract phase label from comma-separated labels
BATCH=$(echo                  ← extract batch label from comma-separated labels
SHORT=$(grep                  ← extract short slug for branch naming
```

### Shell — Text Processing (read-only transforms)
```
sort / sort -u
uniq
tr
awk '{...}'
sed 's/.../.../'              ← read-only transforms ONLY — never sed -i (in-place edit)
perl -pi -e 's/.../.../'      ← in-place multi-file regex substitution (mass rename sweeps)
perl -i -CSD -0777 -pe '...'  ← same with Unicode and slurp-mode flags
perl -e '...'                 ← one-liner (read-only transforms, no file writes unless -i)
cut -d... -f...
xargs (read-only pipelines)
```

### Search
```
rg <pattern> [flags]          ← ripgrep preferred over grep
grep -r / grep -n / grep -l [flags]
find <path> -name "..." -type f   ← read-only discovery only
```

### Filesystem — Safe Creates (within worktree or /tmp only)
```
mkdir <path>                  ← with or without -p; within worktree or /tmp only
cp <src> <dst>                ← within worktree only
mv <src> <dst>                ← within worktree only
touch <file>                  ← within worktree only
ln -s <target> <link>         ← within worktree only
rsync <flags> <src> <dst>     ← any rsync invocation (bare `rsync` covers all flag variants)
rsync -a <src> <dst>          ← file sync between /tmp/ staging and ~/.agentception/worktrees/
                              ← agents write files to /tmp/ then rsync to their worktree when
                              ← the worktree path is outside the sandbox filesystem scope.
                              ← Without this on the allowlist, the entire command block (including
                              ← docker compose exec) is sandboxed and the Docker socket is blocked.
```

### Temporary Files
```
echo "..." > /tmp/<file>      ← always safe; /tmp is ephemeral
cat /tmp/<file>
rm /tmp/<file>                ← temp file cleanup only (no -r, no -f on real dirs)
```

### Git — Read Only
```
git status
git log [any flags: --oneline, -n N, --graph, --stat, --name-only, --format=...]
git diff [any variant: --staged, origin/dev...HEAD, HEAD~N, specific files]
git show <ref>
git branch / git branch -a / git branch -r
git worktree list
git ls-remote origin
git rev-parse <ref>
git fetch origin              ← fetch is always safe; no local changes
git pull origin dev           ← shorthand for fetch + merge; coordinator pre-flight only
git stash list
git ls-files
git merge-base HEAD origin/dev
git describe [--tags]
git cat-file -t/-p <object>
```

### Git — Bisect (full suite for regression hunting)
```
git bisect start
git bisect bad [<rev>]
git bisect good [<rev>]
git bisect log
git bisect reset
```

### Git — Config (rerere only — limited to this one safe key)
```
git config rerere.enabled true || true   ← coordinator setup only; enables conflict-resolution cache
                                          ← the || true is intentional: when this command runs as
                                          ← part of a multi-statement script block, Cursor sandboxes
                                          ← the whole block, and the sandbox blocks .git/config writes
                                          ← with EPERM. rerere is an optimization (not critical path),
                                          ← so failure must be non-fatal. ALL other git config writes
                                          ← are Yellow tier.
```

### Git — Safe Writes (worktree scope only — never touching dev/main directly)
```
git checkout -b <feature-branch>   ← creating new branches
git checkout <existing-branch>     ← switching within own worktree
git fetch origin && git merge origin/dev   ← syncing from remote dev
git add <files>                    ← staging specific files
git add -A / git add .             ← staging all changes
git commit -m "..."                ← committing within own worktree
git merge origin/dev               ← integrating latest dev into feature branch
git stash                          ← shelving uncommitted work temporarily
git stash pop                      ← restoring stashed work
git stash apply                    ← same as pop but keeps stash entry
git restore <file>                 ← discarding local changes to a tracked file
git restore --staged <file>        ← unstaging staged changes (used in cleanup)
git worktree add --detach <path> <sha>   ← creating detached worktrees
git worktree remove --force <path>       ← removing OWN worktree at end of task
git worktree prune                       ← pruning stale worktree refs
```

### Git — Push (feature branches only)
```
git push origin <feature-branch>          ← non-main, non-dev branches ONLY
git push -u origin <feature-branch>       ← same as above with upstream tracking; -u writes to .git/config
                                           ← must be on the allowlist so it runs outside sandbox
git push origin --delete <feature-branch> ← remote branch cleanup after PR merge
git branch -D <feature-branch>            ← delete LOCAL branch ref after PR squash-merge
                                           ← worktree remove destroys the directory but NOT the local ref
```

### GitHub CLI — Read Only
```
gh auth status
gh repo view
# ⚠️  PREFER MCP for these — use gh only when MCP is unavailable in this context:
# pull_request_read(pullNumber=N)           replaces: gh pr view <N>
# list_pull_requests(state=...)             replaces: gh pr list
gh pr diff <N>                              ← keep as gh — no MCP equivalent (diff content)
# issue_read(issue_number=N)               replaces: gh issue view <N>
# list_issues(label=..., state=...)        replaces: gh issue list
gh label list [--limit N]                  ← keep as gh — no MCP equivalent
gh run list
gh run view <id>
gh release list
gh release view [<tag>]
gh api <GET-endpoint>                       ← read-only API calls only
```

### GitHub CLI — Label Management (Label Pre-flight scripts)
```
gh label create <name> --color <hex> --description "..."  ← idempotent; pre-flight before any batch
gh label delete <name> --yes                              ← removes stale/conflicting labels
gh label edit <name> --color <hex> --description "..."    ← updates existing label metadata
# ⚠️  Label pre-flight MUST run before worktrees are created for any batch.
#    Without it, gh issue edit --add-label fails silently and issues are mislabeled.
```

### GitHub CLI — Allowed Writes (prefer MCP equivalents instead)
```
gh pr checkout <N>                 ← ONLY allowed write with no MCP equivalent; use for branch checkout
# All other writes — use MCP tools instead:
# create_pull_request(base, head, title, body)     replaces: gh pr create
# update_pull_request(pullNumber, ...)             replaces: gh pr edit
# add_issue_comment(issue_number, body)            replaces: gh pr comment / gh issue comment
# merge_pull_request(pullNumber, merge_method="squash")  replaces: gh pr merge
# issue_write(title, body)                         replaces: gh issue create
# issue_write(issue_number, state="closed")        replaces: gh issue close
# github_add_label(issue_number, label)            replaces: gh issue edit --add-label
# github_remove_label(issue_number, label)         replaces: gh issue edit --remove-label
```

### Docker — Inspection
```
docker compose             ← general prefix; covers any subcommand not listed below
docker compose ps
docker compose -f <file> ps   ← explicit compose file; used by coordinator pre-flight
docker compose logs <service>
docker compose config
docker ps
docker inspect <container>
docker logs <container> [--tail N]   ← direct container log access (e.g. docker logs agentception --tail 50)
docker exec <container> <cmd>        ← direct container exec (e.g. docker exec agentception tail -50 /proc/...)
```

### Docker — exec agentception (tests, type checks, migrations, inspection)
```
docker compose exec agentception mypy <paths>
docker compose exec agentception pytest <file> -v [flags]
docker compose exec agentception sh -c "<any command>"
docker compose exec agentception python -m coverage run ...
docker compose exec agentception python -m coverage report ...
docker compose exec agentception python3 -m <module>       ← stdlib modules inside container
docker compose exec agentception ps aux                    ← process inspection inside container
docker compose exec agentception ls <path>
docker compose exec agentception cat <file>
docker compose exec agentception sed <args>
docker compose exec agentception head <args>
docker compose exec agentception tail <args>
docker compose exec agentception wc <args>
docker compose exec agentception awk <args>
docker compose exec agentception rg <pattern>
docker compose exec agentception grep <pattern>
docker compose exec agentception find <path>
docker compose exec agentception python -c "<one-liner>"   ← Python one-liner inside container
docker compose exec agentception python3 <script>          ← explicit python3 binary
docker compose exec agentception alembic history
docker compose exec agentception alembic current
docker compose exec agentception alembic heads
docker compose exec agentception alembic upgrade head   ← forward migration only
```

### Docker — exec agentception (tests, type checks, inspection)
```
docker compose exec agentception mypy agentception/
docker compose exec agentception pytest <file> -v [flags]
docker compose exec agentception sh -c "<any command>"
docker compose exec agentception python3 -m <module>
docker compose exec agentception python -c "<one-liner>"
docker compose exec agentception python3 <script>
docker compose exec agentception ls <path>
docker compose exec agentception cat <file>
docker compose exec agentception tail <args>
docker compose exec agentception head <args>
docker compose exec agentception wc <args>
docker compose exec agentception rg <pattern>
docker compose exec agentception grep <pattern>
```

### Docker — exec postgres (read-only queries only)
```
docker compose exec agentception-postgres psql -U agentception -c "SELECT ..."
docker compose exec agentception-postgres psql -U agentception -c "\dt"
docker compose exec agentception-postgres psql -U agentception -c "\d <table>"
```

### Process Inspection
```
ps aux
ps -ef
pgrep <name>
```

### Python — Host-side scripts and one-liners
```
python3                        ← allowed for: agentception scripts, resolve_arch.py --fingerprint,
                               ← and any other scripts in scripts/gen_prompts/ or agentception/
                               ← NOT for arbitrary business logic — use docker compose exec for that
python3 -c "<one-liner>"      ← JSON parsing, string manipulation of gh/git output ONLY
                               ← No file writes, no imports beyond stdlib, no network calls
python3 -m json.tool           ← pretty-printing JSON output from curl or gh commands
python3 -m <stdlib-module>     ← stdlib modules only (json, base64, hashlib, etc.)
open                           ← (1) macOS shell command — opens files/URLs in the default GUI app
                               ← (2) Python builtin — appears as a standalone token when Cursor
                               ←     parses a Python heredoc: open('.agent-task', 'rb')
                               ← Both are safe; neither writes to disk or makes network calls
pip3 install                   ← agentception host-side dependency installs ONLY
                               ← All other package installs must use docker compose exec
```

### Node.js / CSS toolchain — Host-side asset builds
```
npm run <script>               ← run package.json scripts (build:js, build:css, build, watch:js)
                               ← ONLY for agentception static asset compilation on the host
npm ci --include=dev           ← clean install from lockfile; use in worktrees before building
                               ← NEVER inside Docker — add to Dockerfile instead
npx <pkg> [args]               ← run Node.js tools without a global install (e.g. prettier, postcss)
sass <src> <dst> [flags]       ← SCSS → CSS compilation for agentception static assets
                               ← canonical invocation: sass --style=compressed --no-source-map <src> <dst>
brew install <pkg>             ← host-side macOS tool installs ONLY (e.g. sass, node, jq)
                               ← NEVER use inside Docker — add to Dockerfile instead
```

### Shell — Crypto / Hashing / Encoding
```
openssl dgst -sha256 <file>    ← SHA-256 digest; used for SRI hash generation (CDN asset integrity)
openssl dgst -sha256 -binary   ← binary digest output piped to base64 for SRI hash generation
base64                         ← base64 encode/decode; used in two contexts:
                               ← (1) with openssl dgst to produce SRI hashes
                               ← (2) decoding file content returned by gh api (GitHub API returns
                               ←     file content as base64-encoded strings — decode with base64 -d)
```

### Network — Read-only probes
```
curl -s <local-url>            ← GET only; local health checks only (e.g. localhost:10001/health)
nc -z <host> <port>            ← port connectivity check only
```

---

## Tier 2 — Yellow (NOT on Allowlist — runs in sandbox, may prompt once)

These commands are NOT in the Cursor allowlist. They run in the sandbox automatically
but will prompt if they trip a sandbox restriction. Agents should justify why they're
needed and confirm scope is correct before running.

| Command | When OK | When NOT OK |
|---------|---------|-------------|
| `rm <single-file>` | Removing a specific tracked file the agent is deleting as part of its task | Any file not created or explicitly owned by this task |
| `docker compose build <service>` | `requirements.txt`, `Dockerfile`, or `entrypoint.sh` changed | After code-only changes — bind mounts make this unnecessary |
| `docker compose up -d` | After a confirmed necessary rebuild | Restarting to "flush state" without a real reason |
| `docker compose restart <service>` | Service is confirmed unhealthy after checking logs | Routine troubleshooting — use logs to diagnose first |
| `git cherry-pick <sha>` | Moving a specific commit explicitly requested by user | As a substitute for properly merging dev |
| `git rebase <branch>` | Cleaning up local commits before PR (own worktree, not shared) | Any rebase that rewrites shared/pushed history |
| `git stash drop` | Discarding a stash explicitly created by this agent | Any stash not created by this agent |
| `curl -s <url>` POST/PUT | Probing a local endpoint for a diagnostic reason | Any POST/PUT/DELETE to production or external services |
| `docker compose exec agentception alembic downgrade` | Explicitly requested DB rollback | Routine use — destructive, should always be questioned |
| `docker compose exec postgres psql` with mutations | Explicitly requested data fix | Any ad-hoc data modification without explicit user request |
| `sed -i` (in-place file edit via shell) | Never — use the StrReplace tool instead | Always |
| `python3 <script.py>` on the host | Allowed for `scripts/gen_prompts/` and `agentception/` scripts | Any other business logic — use `docker compose exec` instead |
| `python3 -c "..."` with file I/O or non-stdlib | Use `docker compose exec` instead | When the one-liner writes files, imports non-stdlib, or makes network calls |

---

## Tier 3 — Red (Never run — regardless of context or seeming necessity)

These commands are **forbidden**. If an agent believes one is necessary, it must
stop, explain why to the user, and wait for explicit approval. A paused agent
is infinitely better than a destructive one.

### Recursive deletion
```
rm -rf <anything>
rm -r <anything>
rmdir -p <anything>
```

### Force-pushing or pushing to protected branches
```
git push --force
git push -f
git push --force-with-lease     ← still forbidden without explicit user approval
git push origin dev             ← NEVER push directly to dev
git push origin main            ← NEVER push directly to main
```

### Checking out PR branches in the main repo
```
gh pr checkout <N>              ← FORBIDDEN in any context
```
`gh pr checkout` runs `git checkout` against the main repo's working directory, not
against the current worktree. This silently moves the main repo onto the feature branch,
poisoning every subsequent `git worktree list` result and leaving dirty state that
cascades across all concurrent agents. **Always use `git checkout "$BRANCH"` from within
your worktree directory instead.** If you think you need `gh pr checkout`, you don't —
the reviewer worktree is already at the correct branch tip when it is created.

### Hard reset (discards history / working state)
```
git reset --hard <anything>
git clean -fd                   ← deletes untracked files
git clean -fdx                  ← deletes untracked + ignored files
```

### Destructive Docker operations
```
docker system prune
docker volume prune
docker container prune
docker image prune
docker rm -f <container>        ← use restart/stop, not rm
docker volume rm <volume>
docker compose down -v          ← -v flag destroys named volumes (including Postgres data)
```

### System-level operations
```
chmod -R <anything>
chmod 777 <anything>
chown -R <anything>
sudo <anything>
su <anything>
```

### Secrets exposure
```
env | grep -i "key\|secret\|token\|password"
printenv
cat .env
```

### Alembic destructive
```
docker compose exec agentception alembic downgrade base   ← drops all tables
alembic stamp <rev>                                  ← rewrites migration pointer without migrating
```

### Production / external mutations
```
curl -X POST/PUT/DELETE <external-url>
gh release create               ← creating releases requires explicit user intent
npm publish / pip publish       ← publishing packages
```

### Polluting the main repo working tree
```
# NEVER cd into $REPO and edit files there — that pollutes dev
# Your worktree is your sandbox — stay in it
```

### Merging without grade output
```
merge_pull_request(N)   ← without having first output "Grade: X" and "Approved for merge"
```

### Closing a PR without a mandatory explanation comment
```
gh pr close <N>   ← FORBIDDEN unless gh pr comment with the exact reason runs FIRST.
                  ← For D/F grade: do NOT close the PR — leave it open for the engineer to fix.
                  ← The only valid reason to close (not merge) a PR is if it is fundamentally
                  ← broken and must be re-opened from scratch. Even then, comment first.
```

---

## Hard Rules (apply regardless of tier)

1. **Never pipe `mypy` or `pytest` output through `grep`, `head`, or `tail`.**
   The exit code is the authoritative signal. Filtering hides failures.

2. **Limit host-side Python to pipeline tooling.** `python3` on the host is allowed for
   `scripts/gen_prompts/` scripts (e.g. `resolve_arch.py --fingerprint`) and `agentception/`
   scripts. All other Python — business logic, tests, migrations — must use
   `docker compose exec <service> ...`. `python3 -c "..."` one-liners are allowed for
   read-only stdlib-only data processing (JSON parsing of `gh` output, etc.).

3. **Never `sed -i` to edit files.** Use the StrReplace tool — it's safer, tracked, and shows a diff.

4. **Never copy files into the main repo** (`$REPO`) for testing.
   Your worktree is already live inside Docker via bind-mount.

5. **`git worktree remove --force <path>`** is allowed — but ONLY for your OWN worktree,
   at the end of your task. Never remove another agent's worktree.

6. **When in doubt, stop and ask.** Explain exactly what you need and why.
   A paused agent is infinitely better than a destructive one.

7. **Never use `while read` or `xargs gh issue close` in pipelines for issue/PR data.** Use MCP
   instead — `issue_write` and `add_issue_comment` are direct tool calls with no subprocess overhead
   and no sandbox prompt risk.
   ```bash
   # ✅ Correct — use MCP to close linked issues (no shell subprocesses needed):
   # For each issue number in the "Closes #N" list from the PR body:
   # MCP: issue_write(issue_number=N, state="closed")
   #      add_issue_comment(issue_number=N, body="✅ Closed by PR #<N> (merged).")

   # ❌ Wrong — while read triggers a sandbox prompt:
   | while read ISSUE_NUM; do gh issue close "$ISSUE_NUM" ...; done
   ```

8. **Never run `gh pr close` without first posting a mandatory explanation comment.**
   An agent-closed PR with no comment is untraceable — it leaves the human with no way to
   understand what failed or what to do next. The required sequence is:
   ```bash
   # ✅ Correct — explain before closing:
   gh pr comment "$N" --repo "$GH_REPO" --body "❌ Closing PR without merge.
   Reason: <exact reason — D/F grade, fundamental design flaw, etc.>
   Grade (if reviewed): <grade>
   Next steps: <what the engineer or human should do>"
   gh pr close "$N" --repo "$GH_REPO"

   # ❌ Wrong — silent close, leaves humans with no context:
   gh pr close "$N" --repo "$GH_REPO"
   ```
   **For D/F grade: do NOT close the PR.** Leave it open so the engineer can push fixes
   to the existing branch. File a GitHub issue describing the failures instead and self-destruct.

9. **`GH_REPO` is always `cgcardona/agentception` — hardcoded, never derived.**
   The local path (e.g. `/Users/<you>`) contains the local repo directory which is
   NOT the GitHub org. Using `gh` without `--repo "$GH_REPO"` or with a derived slug causes
   "Forbidden" / "Repository not found" errors.
   ```bash
   export GH_REPO=cgcardona/agentception   # always set this first; all gh commands inherit it
   ```
