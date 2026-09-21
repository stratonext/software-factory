# AGENTS.md

Instructions for coding agents working on this repository. Humans: [`CONTRIBUTING.md`](CONTRIBUTING.md)
covers the same ground with more prose; [`README.md`](README.md) is for *using* the factory
rather than changing it.

## What this is

`sf` — a local software factory. Requests flow through YAML-defined pipelines of agent,
shell and judge steps, each request in its own git worktree. Python 3.11+, packaged as a
CLI installed globally (`uv tool install software-factory`).

## Setup

[uv](https://docs.astral.sh/uv/) and [Task](https://taskfile.dev) are the only prerequisites.
`uv run` syncs the environment, so there is no install step.

```bash
task setup             # once per clone: points git at .githooks/
task --list            # every task, with what it does
```

## Before you finish: run `task verify`

```bash
task verify            # lint + type-check + test - what the pre-commit hook and CI run
task test              # pytest alone
task lint              # ruff, zero warnings required
task compile           # mypy
task run -- status     # the CLI from the working tree; arguments after `--`
```

`task verify` must be green before you hand work back. The pre-commit hook runs it anyway,
plus a secret scan of the staged lines — so a commit that succeeds locally is a CI run that
passes. Do not reach for `git commit -n`; fix the failure instead.

Touching packaging, the console script or the data files under `software_factory/runners/`
also means:

```bash
task build             # sdist + wheel into dist/, then scripts/check_dist.py asserts it is usable
task smoke             # install that wheel into a throwaway venv and run `sf` from it
```

A green suite proves the code works from a checkout; it says nothing about the artifact
people install, and both halves have broken here before.

## Layout

```
software_factory/            config, engine, backend, steps, judge, pipeline, runners, CLI
software_factory/__main__.py the whole `sf` CLI (typer + rich)
software_factory/runners/    built-in runners as data: claude.yaml, shell.yaml, typesafe.yaml
examples/                    pipelines to copy into your own repo - never installed
docs/                        the model, operating it, the internals, the pipeline schema
tests/                       one module per module it covers; no network, no API key
.sf/pipelines/               the factory's own pipelines - it works on itself
.githooks/                   the pre-commit hook `task setup` installs
```

Start from [`docs/overview.md`](docs/overview.md) for the model and
[`docs/internals.md`](docs/internals.md) before changing the engine.

## Conventions

- **Match the surrounding code.** It uses `%`-formatting for strings and wraps its own long
  lines at 100 columns. Ruff selects bug rules only (`A`, `B006`, `E4`, `E7`, `E9`, `F`) —
  the stylistic families are off on purpose, so do not "fix" style the linter ignores.
- **Type new and edited code.** mypy runs over `software_factory` and `tests`.
  `disallow_untyped_defs` is off only because 54 pre-existing functions predate the config;
  anything you write should not add to that pile.
- **Smallest change that does it.** No speculative abstraction, no config for a value that
  never changes, no new dependency for what a few lines cover. Runtime dependencies are
  `pyyaml`, `typer`, `rich` — adding a fourth needs a reason in the commit message.
- **Comments explain why, not what.** The existing ones pin decisions and the bugs behind
  them; that is the bar.
- **Leave the tree clean.** No scratch files, no commented-out code.

## Tests

- One test module per module it covers.
- The suite never reaches the network and never needs an API key — the one seam that would
  is stubbed in every test that touches it. Keep it that way: a suite that can cost money
  is a suite nobody runs.
- [`tests/test_docs.py`](tests/test_docs.py) checks the prose against the code: every CLI
  command appears in `README.md`, every relative link in every markdown file resolves,
  `docs/pipeline-schema.yaml` matches the loader's `STEP_KEYS`, and `CHANGELOG.md` has an
  entry for the version in `pyproject.toml`. So:
  - a new `sf` command means a README mention,
  - a new step key means a schema entry,
  - a markdown link must point at a file that exists.
- Non-trivial logic ships with a test. Trivial one-liners do not need one.

## Commits

Conventional Commits, imperative mood, lowercase after the colon:

```
feat(engine): retry a step whose runner exits non-zero
fix(cli): stop `sf status` widening past 80 columns
docs(pipelines): explain the judge verdicts
build(pypi): ship a source distribution, not a copy of the repository
test(judge): cover a verdict the parser used to drop
refactor(steps): fold the two worktree helpers into one
chore(deps): bump ruff
```

Scope is the area touched (`engine`, `cli`, `judge`, `pipeline`, `steps`, `backend`,
`config`, `runners`, `docs`, `git`, `pypi`, `test`, `deps`). One logical change per commit.
The body, where there is one, says *why*.

Do not mention the agent or tool that produced the change anywhere in the commit — no
"generated with", no co-author trailer, no tool name in the subject. The message describes
the change, not its author.

## Releasing

Tag-driven and never by hand: bump `version` in `pyproject.toml`, add the
[`CHANGELOG.md`](CHANGELOG.md) entry (the suite fails if they disagree), then push the tag.
Full procedure in [`docs/releasing.md`](docs/releasing.md).
