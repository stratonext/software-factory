---
name: release-cli
description: Release the `sf` CLI to PyPI - a stable version or a release candidate. Asks which, bumps pyproject.toml, writes the CHANGELOG entry, verifies the build, then tags and pushes so the cli-release workflow publishes. Use whenever the user says release, cut a release, ship a version, publish to PyPI, tag a release, or asks for an rc or beta of the CLI.
---

# Releasing the `sf` CLI

The release is tag-driven: pushing `vX.Y.Z` runs `.github/workflows/cli-release.yml`, which
gates on CI, builds, publishes and cuts a GitHub Release. Nothing is uploaded by hand — your
job is to get the version, the changelog and the tag right, then push. The reasoning behind
each step is in [`docs/releasing.md`](../../docs/releasing.md); this is the procedure.

## 1. Ask which kind of release

Read the current version first, so the choice is concrete:

```bash
grep '^version' pyproject.toml
```

Then ask the user with AskUserQuestion — never assume:

- **Stable** → `0.2.0`, published to **PyPI**, a normal GitHub Release.
- **Release candidate** → `0.2.0rc1`, published to **TestPyPI**, marked pre-release.

Offer the next patch, minor and rc as options, computed from the current version. If the user
names a version outright, use it and skip the question.

## 2. Preflight

```bash
git status --short          # must be clean
git rev-parse --abbrev-ref HEAD
git fetch && git status -sb # must not be behind origin
```

A dirty tree or a branch behind origin is a stop: say so and ask, do not "fix" it.
Releases are cut from `main`.

## 3. Bump the version

`pyproject.toml` says it, and `uv lock` copies it into `uv.lock` — the lock records the
project's own version too, so a bump that stops at `pyproject.toml` leaves the tree dirty
the next time anyone runs `uv`. The workflow **checks the tag against `pyproject.toml`** and
fails the release on a mismatch; the version is never derived from the tag.

```toml
version = "0.2.0"
```

```bash
uv lock          # then commit uv.lock with the bump
```

## 4. Write the CHANGELOG entry

`CHANGELOG.md` is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The heading must
carry the exact version — `tests/test_docs.py` fails if `pyproject.toml` has no entry:

```markdown
## [0.2.0] - 2026-09-21
### Added
- ...
### Fixed
- ...
```

Use today's date, and write the entry from `git log <last tag>..HEAD`, in the user's voice:
what changed for someone using the CLI, not a list of commits.

## 5. Verify

```bash
task verify    # lint, type-check, test
task build     # sdist + wheel, then check the wheel is usable
task smoke     # install that wheel into a clean venv and run `sf` from it
```

All three, every time. This package is mostly data files: a green suite says the source
works, and says nothing about the artifact people install.

## 6. Commit, push, tag

```bash
git commit -am "release: v0.2.0"
git push origin main
```

Let CI go green on `main` before tagging — the tag re-runs it, but a red `main` is a release
that fails halfway. Then:

```bash
git tag v0.2.0
git push origin v0.2.0        # this is what triggers the release
```

## 7. Watch it, then check what shipped

```bash
gh run watch $(gh run list --workflow=cli-release.yml -L1 --json databaseId -q '.[0].databaseId')
uv tool install software-factory@0.2.0 && sf --version
```

For an rc, the install comes from the other index:

```bash
uv tool install --index https://test.pypi.org/simple/ software-factory==0.2.0rc1
```

## When it goes wrong

- **Tag does not match `pyproject.toml`** — the `build` job fails and nothing was uploaded.
  Fix `pyproject.toml`, commit, then move the tag:
  `git tag -d v0.2.0 && git push origin :refs/tags/v0.2.0` and tag again.
- **CI failed in `gate`** — nothing published, the tag is just sitting there. Fix `main`,
  move the tag as above.
- **The upload succeeded but something is wrong with it** — PyPI versions are immutable and
  cannot be re-uploaded, even after a delete. Bump to the next patch and release again.
  Never try to reuse a version number that reached the index.
