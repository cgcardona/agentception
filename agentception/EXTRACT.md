# AgentCeption — Extraction Procedure

This document describes the step-by-step process for extracting `agentception/` from the
the monorepo into a standalone GitHub repository and publishing it to PyPI.

---

## Prerequisites

- Git ≥ 2.38 (for `git subtree`)
- A GitHub account with permission to create repositories
- `gh` CLI authenticated (`gh auth status`)
- Python ≥ 3.11 and `pip` for local verification
- PyPI account (for Step 5)

---

## Step 1 — Verify Self-Containment

Before extracting, confirm the package is fully self-contained:

```bash
# Run the self-containment test suite
cd /path/to/monorepo
pytest agentception/tests/test_agentception_extraction.py -v

# Verify pip install works from repo root
pip install -e agentception/ --dry-run
```

All three checks must pass:
- `test_no_maestro_imports_in_agentception` — no cross-package imports
- `test_no_hardcoded_gabriel_paths` — no user-specific paths
- `test_pyproject_toml_valid` — valid TOML with required keys

---

## Step 2 — Split the Subtree

`git subtree split` rewrites history to contain only commits that touched `agentception/`,
producing a branch whose root is the `agentception/` directory contents.

```bash
cd /path/to/monorepo

# Create a branch containing only agentception/ history
git subtree split --prefix=agentception --branch agentception-standalone

# Verify the branch tip looks correct
git log agentception-standalone --oneline | head -10
git show agentception-standalone:pyproject.toml
```

---

## Step 3 — Create the Standalone Repository

```bash
# Create a new GitHub repo
gh repo create agentception \
  --description "Multi-tier AI agent pipeline dashboard" \
  --public \
  --clone

cd agentception

# Pull the extracted history in
git pull /path/to/monorepo agentception-standalone

# Verify the structure
ls -la
# Should show: agentception/, pyproject.toml, README.md, EXTRACT.md, etc.
```

---

## Step 4 — Update `pipeline-config.json` Paths

After extraction, `pipeline-config.json` references in the new repo need updating:

1. Copy your existing `pipeline-config.json` to `.cursor/pipeline-config.json` in the new repo.
2. Update the `projects` array:

```json
{
  "active_project": "agentception",
  "projects": [
    {
      "name": "agentception",
      "gh_repo": "YOUR_GITHUB_USERNAME/agentception",
      "repo_dir": "/path/to/your/agentception",
      "worktrees_dir": "~/.agentception/worktrees/agentception"
    }
  ]
}
```

3. Set the env var or rely on the config:

```bash
export GH_REPO=YOUR_GITHUB_USERNAME/agentception
export REPO_DIR=/path/to/your/agentception
agentception
```

---

## Step 5 — Add GitHub Actions for CI

Create `.github/workflows/ci.yml` in the new repository:

```yaml
name: CI

on:
  push:
    branches: [main, dev]
  pull_request:
    branches: [main, dev]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Install dependencies
        run: |
          pip install -e ".[dev]"

      - name: Run mypy
        run: mypy agentception/

      - name: Run tests
        run: pytest agentception/tests/ -v
```

Commit and push:

```bash
git add .github/
git commit -m "ci: add GitHub Actions workflow"
git push origin main
```

---

## Step 6 — Publish to PyPI

### First Release

```bash
# Install build tools
pip install build twine

# Build the distribution
python -m build

# Upload to PyPI (requires PyPI account and API token)
twine upload dist/*
```

### Subsequent Releases

Bump the version in `pyproject.toml`:

```toml
[project]
version = "0.2.0"
```

Then build and upload:

```bash
python -m build
twine upload dist/*
```

### Automated Releases via GitHub Actions

Add `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI

on:
  push:
    tags:
      - "v*"

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install build
        run: pip install build

      - name: Build
        run: python -m build

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

To release:

```bash
git tag v0.1.0
git push origin v0.1.0
```

---

## Post-Extraction Checklist

- [ ] `pip install agentception` works from PyPI
- [ ] `agentception` CLI launches the dashboard
- [ ] CI passes on both Python 3.11 and 3.12
- [ ] README renders correctly on PyPI and GitHub
- [ ] Close the `agentception/` subtree in the original monorepo (or add a redirect note in its README)

---

## Keeping the Monorepo in Sync

If you continue developing in the monorepo and want to upstream changes to the standalone repo:

```bash
# In the monorepo: regenerate the split branch after new commits
git subtree split --prefix=agentception --branch agentception-standalone

# In the standalone repo: pull the new commits
cd /path/to/agentception
git pull /path/to/monorepo agentception-standalone
```

For the reverse direction (changes made in the standalone repo → monorepo):

```bash
# In the monorepo
git subtree pull --prefix=agentception /path/to/agentception main
```
