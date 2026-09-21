"""The docs, checked against the code they describe.

Every one of these pins a mistake that actually shipped. `sf agents` was a command no
document mentioned; the changelog sat a version behind `pyproject.toml`; a flag deleted from
the CLI went on being documented in four places, one of them executable. Prose rots silently
because nothing runs it - so run the parts that can be run.

These read files from the repository, not from the installed package. They are dev tests and
do not pass from a wheel.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from software_factory import pipeline as pl
from software_factory.__main__ import app

ROOT = Path(__file__).parent.parent


def test_every_command_is_documented():
    """A command nobody wrote down is a command nobody finds. `agents` shipped that way."""
    readme = (ROOT / "README.md").read_text()
    commands = [c.name or c.callback.__name__ for c in app.registered_commands]
    missing = [c for c in commands if "sf %s" % c not in readme]
    assert not missing, "not in README.md: %s" % ", ".join(missing)


def test_the_changelog_has_an_entry_for_this_version():
    """Bumping the version and forgetting the entry is the easiest release mistake there is.

    Only once the changelog is being kept: a file with no version headings at all is one
    nobody is maintaining yet, and failing every run over that teaches people to ignore the
    suite. The moment one entry exists, the current version has to be among them.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text()
    entries = re.findall(r"^## \[([^\]]+)\]", changelog, re.M)
    versions = [e for e in entries if e.lower() != "unreleased"]
    if not versions:
        pytest.skip("CHANGELOG.md lists no releases yet")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert version in versions, \
        "pyproject says %s; CHANGELOG.md lists %s" % (version, ", ".join(versions))


def test_the_schema_matches_the_loader():
    """`pipeline-schema.yaml` is documentation, not a second validator - so nothing but this
    stops a key added to the loader from being missing there, or the reverse."""
    schema = yaml.safe_load((ROOT / "docs" / "pipeline-schema.yaml").read_text())
    assert set(schema["$defs"]["step"]["properties"]) == pl.STEP_KEYS
