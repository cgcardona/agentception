# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

See [CONTRIBUTING](docs/guides/contributing.md) for commit conventions and PR guidelines.

## [Unreleased]

## [0.3.0] – 2026-03-04 <!-- TODO: fill in release date -->

### Added

- CI pipeline (GitHub Actions) with mypy, pytest, and coverage steps.
- Zero-Any mypy typing ceiling enforced in CI — `tools/typing_audit.py` blocks merge on any `Any` introduction.
- Branch protection rules: `feature/*` → `dev` → `main`, PRs required for all merges.

## [0.2.0] – 2026-03-04 <!-- TODO: fill in release date -->

### Changed

- `AC_` environment variable prefix applied to all configuration keys for namespace isolation.
- All paths made portable across host operating systems — no hardcoded absolute paths remain in configuration or tooling.

## [0.1.0] – 2026-03-04 <!-- TODO: fill in release date -->

### Added

- Standalone extraction from the maestro monorepo; `cgcardona/agentception` established as an independent repository.
- Initial project scaffold: FastAPI application with Jinja2/HTMX/Alpine.js build dashboard.
- Docker Compose setup with bind mounts for fast development iteration (no rebuild required for code changes).
- Base agent infrastructure: `.agentception/` configuration directory, cognitive architecture scripts, and dispatcher prompt.

---

[Unreleased]: https://github.com/cgcardona/agentception/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/cgcardona/agentception/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cgcardona/agentception/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cgcardona/agentception/releases/tag/v0.1.0
