# Changelog

All notable changes to this project are documented here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Qualified pipeline names: `--pipeline local:<name>` / `global:<name>` pick a tier when a
  name exists in both; a bare name still tries repo then global. Recorded on the request,
  so `sf status` and the run agree on which file it means.
- `sf pipelines`: every pipeline the factory can see, both tiers, marking which name
  shadows which. Default is one line each; `-d`/`--detailed` adds version and description.
- `sf reset <ids>`: send a request back to its pipeline's `start` to rework it from
  scratch — passes and notes cleared, history and worktree kept. Refuses a `running`
  request; asks `y/N` first (`--yes` to skip).
- `sf status --monitor` / `-m`: redraws the table in place once a second until Ctrl-C,
  with a heartbeat pulse. Needs a terminal; refuses `--json`.
- `sf daemon start`: a background engine that works the queue as requests land, polling
  every `--interval` seconds under `sf run`'s concurrency quota. `sf daemon status` /
  `stop` manage it. Pairs with `sf submit --paused`, which keeps a request out of its way
  until `sf run <id>` starts it.
- `docs/pipeline-schema.yaml`'s `$id` is now its published raw GitHub URL, so any pipeline
  file - in this repo or any other - can point a `# yaml-language-server: $schema=` comment
  at it directly, not only at a relative path that only resolves inside this checkout.

### Changed
- The STAGE column now draws as a bar in every human view — `code ███░░░░░ 2/5`. `--json`
  keeps the bare `code 2/5`, since that field is something a script reads.
- `sf status` / `--json` always prefixes the PIPELINE column with `local:` or `global:`,
  even for a request that named the pipeline bare - which file it actually resolved to,
  not just which tier it was asked for.
- `sf run` now starts an engine in the background and returns at once by default, the same
  shape `sf daemon` already has; `--wait` is the opt-in form that blocks until the queue is
  worked (what gates CI). Replaces `--detach`, which was the opt-in the other way round.

### Fixed
- Unparking a request (`sf run <id>`, e.g. after `sf pause`) could run it immediately even
  with every worker already spent elsewhere - a daemon, or a second `sf run` - blowing past
  the configured `concurrency`. It now goes back to `queued` and waits its turn like
  anything else in the line.
- A `paused` request's STAGE bar read as fully worked (`a ████████ 1/1`) though it had
  never run - the bar only knew `queued` meant "not yet", so anything else, `paused`
  included, read as "already past this stage". It now reads `0/1`, same as `queued`.

## [0.0.2] - 2026-09-21

Promoted 0.0.2rc1 to 0.0.2

## [0.0.2rc1] - 2026-09-21

### Changed
- `sf submit` takes the request as `--description "<request>"` rather than as a bare
  argument; `--file` is unchanged. A loose word on the command line is now a usage error.

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
