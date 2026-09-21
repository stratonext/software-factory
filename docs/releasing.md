# Releasing the CLI

The `sf` CLI ships to PyPI as [`software-factory`](https://pypi.org/project/software-factory/).
Releases are **tag-driven**: pushing a `v*` tag runs
[`.github/workflows/cli-release.yml`](../.github/workflows/cli-release.yml), which gates on
the full CI suite, builds, publishes and cuts a GitHub Release. Nothing is uploaded from a
laptop.

```
v0.1.0      ->  CI -> build -> PyPI     -> GitHub Release
v0.1.0rc1   ->  CI -> build -> TestPyPI -> GitHub pre-release
```

A tag ending in `aN`, `bN` or `rcN` is a pre-release and goes to the TestPyPI track. Anything
else goes to PyPI.

## The version lives in pyproject.toml

The version is **not** derived from the tag. `pyproject.toml` carries it (hatchling, static),
and the release workflow checks the tag against it — a mismatch fails the release before
anything is uploaded. Deriving it would let a `v9.9.9` tag ship a wheel that says `0.0.1`.

So the version has to be bumped in a commit, *before* the tag.

## Steps

1. **Bump the version.**

   ```bash
   # pyproject.toml
   version = "0.1.0"
   ```

2. **Add the changelog entry.** [`CHANGELOG.md`](../CHANGELOG.md) follows
   [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the heading must be
   `## [0.1.0] - YYYY-MM-DD`. `tests/test_docs.py` fails if the version in `pyproject.toml`
   has no entry, so a forgotten entry is caught by `task verify`, not by a user.

3. **Verify locally.**

   ```bash
   task verify            # lint, type-check, test
   task build             # sdist + wheel into dist/, then check the wheel is usable
   task smoke             # install that wheel into a clean venv and run `sf` from it
   ```

   `build` and `smoke` matter because this package is mostly data files — the built-in
   runners, the shipped pipelines, their prompts. A green suite says the source works; it
   says nothing about the artifact people install.

4. **Commit and push to `main`.**

   ```bash
   git commit -am "release: v0.1.0"
   git push origin main
   ```

   Let CI go green on `main` before tagging. The tag re-runs CI anyway, but a red `main` is a
   release that will fail halfway.

5. **Tag and push the tag.** This is what triggers the release.

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

6. **Watch the run.**

   ```bash
   gh run watch $(gh run list --workflow=cli-release.yml -L1 --json databaseId -q '.[0].databaseId')
   ```

7. **Check what shipped.**

   ```bash
   uv tool install software-factory@0.1.0 && sf --version
   ```

## Release candidates

Same steps, with a PEP 440 pre-release version in `pyproject.toml` and a matching tag:

```bash
# version = "0.1.0rc1"
git tag v0.1.0rc1 && git push origin v0.1.0rc1
```

It publishes to TestPyPI and the GitHub Release is marked as a pre-release. Install it with:

```bash
uv tool install --index https://test.pypi.org/simple/ software-factory==0.1.0rc1
```

## What the workflow does

| Job | What it does |
|-----|--------------|
| `gate` | Calls [`ci.yml`](../.github/workflows/ci.yml) — the real suite on 3.11–3.14, plus lint and the wheel build. Not a second copy of it. |
| `build` | Checks the tag against `pyproject.toml`, runs `task build` and `task smoke`, uploads `dist/` as an artifact. |
| `publish` | Uploads `dist/` to PyPI or TestPyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/). |
| `github-release` | Attaches the sdist and wheel to a GitHub Release with generated notes. |

Publishing uses Trusted Publishing, so there is **no API token to store or rotate**. The
`publish` job picks the GitHub environment (`pypi-cli` or `testpypi-cli`) from whether the tag is a
pre-release; each environment must have a trusted publisher on PyPI/TestPyPI pointing at this
repository and `cli-release.yml`.

## When it goes wrong

**Tag doesn't match pyproject.toml.** The `build` job fails with the two versions printed.
Nothing was uploaded. Fix `pyproject.toml`, commit, then move the tag:

```bash
git tag -d v0.1.0 && git push origin :refs/tags/v0.1.0
git tag v0.1.0 && git push origin v0.1.0
```

**Publish failed after a successful upload.** PyPI versions are immutable and cannot be
re-uploaded, even after a delete. Bump to the next patch version and release again — never
try to reuse a version number that reached the index.

**CI failed in `gate`.** Nothing was published; the tag is just sitting there. Fix the
problem on `main`, then move the tag as above.
