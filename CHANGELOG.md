# Changelog

All notable changes to this project are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Qualified pipeline names: `--pipeline local:<name>` searches only the repo's own
  `.sf/pipelines/`, `--pipeline global:<name>` only the installation-wide directory. A bare
  name is unchanged — repo first, then global — because that is how every existing request
  is spelled. The qualifier is recorded on the request, so `sf status` shows `local:dev@1`
  rather than a `dev@1` that could be either file, and the run resolves the one that was
  submitted against. Asking for `local:` where there is no repo, or for a qualifier that
  resolves nowhere, names the one tier that was searched instead of raising.
- `sf pipelines`: every pipeline the factory can see — the installation-wide ones, then the
  current repo's own, each group under the directory it came from, with each pipeline's
  version and its `description:`. A name in both tiers is marked as shadowing (or shadowed
  by) the other, since only one of them is what a bare `--pipeline <name>` has been
  running. `-t`/`--tabular` is the terse form: one padded line each — name, file, flavor —
  so `| grep` reads it. `--json` like every other command.
- `sf reset <ids>`: takes a request back to the start of its pipeline so it can be worked
  again from scratch — stage back to the pipeline's declared `start`, passes back to zero,
  notes emptied, status `queued`. `history` is kept, so `sf replay` and the accumulated
  cost still read the whole life of the request; the worktree and the `sf/<id>` branch are
  kept too — dropping those is what `sf delete` is for. Refuses a `running` request, and
  asks `y/N` first like `prune` does (`--yes` to skip).
- `sf status --monitor` / `-m`: keeps the table on screen and redraws it in place once a
  second until Ctrl-C, re-reading the backend and the pipelines every tick, under a
  heartbeat line that pulses off the tick — so it stops moving exactly when the refresh
  does. Terminal echo is off for the duration: an echoed Enter scrolled the terminal out
  from under the redraw and left a stale copy of the first row behind. It needs a
  terminal, so it refuses `--json` and a stdout that is not one.

### Changed
- The STAGE column now draws its position as a bar in every human view — `code ███░░░░░ 2/5`,
  the finished run in green and the rest dim — the plain table, `--detailed` and `--monitor`
  alike. `--json` keeps the bare `code 2/5`, since that field is something a script reads.

## [0.0.2] - 2026-09-21

Promoted 0.0.2rc1 to 0.0.2

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
