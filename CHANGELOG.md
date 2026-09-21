# Changelog

All notable changes to this project are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Every destructive command asks `y/N` before it acts, defaulting to No: `sf delete` and
  `sf cancel` now prompt as `sf prune` did, and `sf prune` asks however few requests are
  doomed rather than only past a handful. `--yes`/`-y` skips the question on all three,
  and so does a stdin that is not a terminal, so piped and scripted runs are unaffected.

## [0.0.2rc1] - 2026-09-21

### Changed
- `sf submit` takes the request as `--description "<request>"` rather than as a bare
  argument; `--file` is unchanged. A loose word on the command line is now a usage error.

### Fixed
- Help text, option help and comments called the executable `factory`; it has been `sf`
  since 0.0.1. Every command reference now names `sf`, as does the `sf/<id>` worktree
  branch the docs describe.
- `test_worktree_survives_a_reused_branch_name` pre-created a `factory/1` branch, so the
  name it was meant to collide with was never the one `workspace()` creates. It now
  creates `sf/1`, which is the collision the test claims to cover.

## [0.0.1rc1] - 2026-09-21

First published build, to TestPyPI. The engine works end to end; the interfaces are
still free to change.

### Added
- `sf`, one global command for every repo: `init`, `submit`, `run`, `status`, `show`,
  `replay`, `cancel`, `delete`, `prune`, `config`, `runners`, `doctor`.
- Pipelines as YAML, in a repo's `.sf/pipelines/` or in `~/.sf/pipelines/` for every repo,
  with rework edges, review gates and a pass budget before a request parks for a human.
- Three built-in runners: `claude` (Claude Code, non-interactive), `shell`, and `typesafe`
  (a typed Jev judgment, opt-in, off without `TYPESAFE_API_KEY`).
- A git worktree per request, on its own branch, so several run at once without touching
  the tree you are editing.
- `sf --skill`, the skill that teaches an agent to operate the factory.

## [0.0.1] - 2026-09-21

First stable release. The same build as 0.0.1rc1 - see that entry for what it
contains - published to PyPI rather than TestPyPI.
