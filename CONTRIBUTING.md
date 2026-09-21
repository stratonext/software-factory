# Contributing

Building and working on the factory itself. Using it is [`README.md`](README.md).

## Setup

[uv](https://docs.astral.sh/uv/) and [Task](https://taskfile.dev) are the only two things to
have installed; `uv run` syncs the environment, so there is no separate install step to
forget.

```bash
git clone https://github.com/stratonext/software-factory && cd software-factory
task setup             # points git at .githooks/ - do this once per clone
task --list            # every task, with what it does
```

`task setup` sets `core.hooksPath` to [`.githooks/`](.githooks/), so every commit first runs
[`.githooks/pre-commit`](.githooks/pre-commit): a scan for secrets in the staged lines, then
`task verify`. It is the CI job minus the wheel build, so a commit that passes locally is a
pipeline that passes on push. `git commit -n` skips the hook for a commit you know is
broken, and `SKIP_SECRET_SCAN=1 git commit ...` skips only the scan, for a literal that
looks like a key but is not one.

## Tasks

```bash
task verify            # lint, type-check, test - what the hook runs, and what CI runs
task test              # the suite alone
task lint              # ruff, zero warnings required
task compile           # mypy - also the syntax check, since it parses everything
task run -- status     # the CLI from the working tree, arguments after `--`
```

The suite never reaches the network and never needs an API key: the one seam that would is
stubbed in every test that touches it. A suite that can cost money is a suite nobody runs.

## Building

```bash
task build             # sdist + wheel into dist/, then check the wheel is usable
task smoke             # install that wheel into a throwaway venv and run `sf` from it
```

Both matter because this package is mostly data files. A green suite says the code works
from a source checkout; it says nothing about the artifact people install. Both halves have
broken here before — the packaged runners vanished during a rename, and the console script
pointed at a module that no longer existed, so `sf` was not a command at all.
[`scripts/check_dist.py`](scripts/check_dist.py) is what `task build` runs to assert the
wheel carries what the CLI reads at run time.

To use a working copy as your real `sf` while developing:

```bash
uv tool install --editable .
```

## Layout

```
software_factory/            config, engine, backend, steps, judge, pipeline, runners, CLI
software_factory/runners/    the built-in runners: claude, shell, typesafe
examples/                    pipelines to copy into your own repo - never installed
docs/                        the reasoning: the model, operating it, the internals
tests/                       one module per module it covers; no network, no API key
.sf/pipelines/               the factory's own pipelines, used on this repo like any other
.githooks/                   the pre-commit hook `task setup` installs
```

The factory works on itself: `.sf/pipelines/` holds the pipelines this repo uses, so a
change to the engine can be worked by the engine.

## Documentation is tested

[`tests/test_docs.py`](tests/test_docs.py) reads the markdown and checks it against the
code: every CLI command appears in the README, the pipeline
schema matches the loader, and the changelog has an entry for the version in
`pyproject.toml`. Prose rots silently because nothing runs it — so the parts that can be run
are run.

## Releasing

Releases are tag-driven: bump `version` in `pyproject.toml`, add the entry to
[`CHANGELOG.md`](CHANGELOG.md) — the suite fails if they disagree — then push a tag.

```bash
task verify && task build && task smoke
git commit -am "release: v0.1.0" && git push origin main
git tag v0.1.0 && git push origin v0.1.0
```

The tag runs [`.github/workflows/cli-release.yml`](.github/workflows/cli-release.yml), which
gates on CI, builds, publishes to PyPI and cuts the GitHub Release. Nothing is uploaded by
hand. The whole thing — pre-releases, trusted publishing, what to do when it fails — is in
[`docs/releasing.md`](docs/releasing.md).
