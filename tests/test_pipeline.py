"""Loading a pipeline file. Nothing here runs a step - this is the validation layer."""

import json
from pathlib import Path

import pytest
import yaml

from software_factory import pipeline as pl

from conftest import a_repo, build, sh


def test_bad_pipeline_is_rejected_at_load(tmp_path):
    backend, pipelines = build(tmp_path, steps={"a": sh("true", next="nowhere")})
    with pytest.raises(ValueError, match="unknown target"):
        pl.load(pipelines, "t")


def test_pipeline_version_defaults_to_one_and_must_be_an_integer(tmp_path):
    steps_ = {"a": sh("true", next="done")}
    _, pipelines = build(tmp_path, steps=steps_)
    assert pl.load(pipelines, "t").version == 1, "an undeclared version is version 1"
    assert pl.summary(Path(pipelines) / "t.yaml")["version"] == 1

    _, pipelines = build(tmp_path, version=4, steps=steps_)
    assert pl.load(pipelines, "t").version == 4
    assert pl.summary(Path(pipelines) / "t.yaml")["version"] == 4

    # `version: yes` is True in YAML 1.1, and True is an int - the typo guard has to
    # reject it, as it does `v2`.
    for bad in ("v2", True):
        _, pipelines = build(tmp_path, version=bad, steps=steps_)
        with pytest.raises(ValueError, match="version must be an integer"):
            pl.load(pipelines, "t")

    # Listing a directory must survive a half-written file sitting next to the good ones.
    broken = Path(pipelines) / "broken.yaml"
    broken.write_text("steps: [oh: no\n")
    assert pl.summary(broken) == {"name": "broken", "version": "?", "description": ""}
    assert pl.summary(Path(pipelines) / "nope.yaml")["version"] == "?"


def test_pipeline_description_is_optional_prose(tmp_path):
    steps_ = {"a": sh("true", next="done")}
    _, pipelines = build(tmp_path, steps=steps_)
    assert pl.load(pipelines, "t").description == "", "an undeclared description is empty"
    assert pl.summary(Path(pipelines) / "t.yaml")["description"] == ""

    said = "The short line. Use it for a change small enough to state exactly."
    _, pipelines = build(tmp_path, description=said, steps=steps_)
    assert pl.load(pipelines, "t").description == said
    assert pl.summary(Path(pipelines) / "t.yaml") == {
        "name": "t", "version": 1, "description": said
    }


def test_every_example_says_what_it_is_for():
    # The point of the field: something choosing a pipeline reads this instead of the name,
    # and `--pipeline auto` does not offer one that describes nothing.
    for path in sorted(Path("examples").glob("*.yaml")):
        pipe = pl.load(path.parent, path.stem)
        assert len(pipe.description) > 40, path.name
        # And every prompt it names is really there: these are copied into someone else's
        # repo, so a missing file is their failed run, not ours.
        for name, step in pipe.steps.items():
            prompt = (step.get("with") or {}).get("prompt")
            if prompt:
                assert (pipe.dir / prompt).exists(), "%s: %s" % (path.name, name)


def test_bare_on_key_survives_yaml_booleans(tmp_path):
    """YAML 1.1 reads a bare `on:` as True. Users write `on:`, so it must still route."""
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "t.yaml").write_text(
        "name: t\nsteps:\n  a:\n    uses: shell\n    with: {run: 'true'}\n"
        "    on: {pass: done, fail: a}\n"
    )
    assert pl.load(pipelines, "t").route("a", "pass") == ("done", False)


def test_unwired_input_is_rejected_at_load(tmp_path):
    _, pipelines = build(tmp_path, steps={"a": sh("true", input="nope.md", next="done")})
    with pytest.raises(ValueError, match="no step produces"):
        pl.load(pipelines, "t")


def test_unknown_step_key_is_rejected(tmp_path):
    """A typo'd gate would otherwise let a run proceed unreviewed."""
    _, pipelines = build(tmp_path, steps={"a": sh("true", reveiw=True, next="done")})
    with pytest.raises(ValueError, match="unknown key"):
        pl.load(pipelines, "t")


def test_a_scratch_name_cannot_escape_the_scratch_dir(tmp_path):
    _, pipelines = build(tmp_path, steps={"a": sh("true", output="../x", next="done")})
    with pytest.raises(ValueError, match="plain file name"):
        pl.load(pipelines, "t")


def test_an_unknown_effort_is_rejected_at_load(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(
        {"name": "bad", "steps": {
            "code": {"uses": "claude", "with": {"prompt": "a.md", "effort": "turbo"}, "next": "done"}}}
    ))
    with pytest.raises(ValueError, match="effort must be one of"):
        pl.load(tmp_path, "bad")


# --- the runner registry: `uses:` --------------------------------------------------
# The whole point of the seam: a pipeline can name a different tool per stage, and the
# factory is testable without `claude` for the first time.

# A runner that is not Claude Code and needs nothing installed: the command is the shell
# builtin, and the "reply" is a canned JSON object with a verdict at the end of it.
CANNED = json.dumps({"text": 'worked\n{"verdict": "pass", "notes": "canned"}'})
ECHO = {
    "name": "echo",
    "description": "A canned reply, for tests.",
    "kind": "agent",
    "requires": ["prompt"],
    "command": ["sh", "-c", 'printf %s "$0"', CANNED],
    "output": {"format": "json", "text": "text"},
}


def test_a_repos_own_pipeline_wins_over_the_global_one(installation, tmp_path, monkeypatch, capsys):
    global_pipelines = installation / "pipelines"
    global_pipelines.mkdir(parents=True)
    (global_pipelines / "dev.yaml").write_text(
        yaml.safe_dump({"name": "dev", "steps": {"global_step": sh("true", next="done")}})
    )
    repo = a_repo(tmp_path / "myproject")  # ships its own dev.yaml with step "a"

    found = pl.load(pl.search_path(repo, global_pipelines), "dev")
    assert list(found.steps) == ["a"], "the repo's own .sf/pipelines/ takes precedence"
    assert list(pl.load(pl.search_path(None, global_pipelines), "dev").steps) == ["global_step"]


def test_every_example_pipeline_loads():
    # examples/ is copied into someone's repo before it is ever run, so a broken example
    # is found by them and not by us. Nothing installs these; this test is the only thing
    # standing between a typo here and a pipeline that fails at submit.
    files = sorted(Path("examples").glob("*.yaml"))
    # Every one of them is listed in the README next to it, because an example nobody is
    # pointed at is one nobody copies. No count here: adding an example is a README edit,
    # not a test edit.
    listed = Path("examples/README.md").read_text()
    assert files, "examples/ is empty"
    assert not [f for f in files if f.name not in listed], "not in examples/README.md: %s" % (
        ", ".join(f.name for f in files if f.name not in listed))
    for path in files:
        pipe = pl.load(path.parent, path.stem)  # loading is validating
        assert pipe.name == path.stem, path.name
        assert len(pipe.description) > 40, "%s: an example has to say what it is for" % path.name
        for stage, step in pipe.steps.items():
            for kind in ("agent", "judge"):
                # A prompt path is relative to the pipeline file, and must stay inside
                # examples/ - the point of copying the shipped prompts rather than
                # pointing at them is that this directory travels on its own.
                if kind in step:
                    assert (pipe.dir / step[kind]).exists(), "%s.%s" % (path.name, stage)


def test_nothing_ships_so_the_search_path_is_two_tiers(installation, tmp_path):
    """The factory is the engine; the process is the user's. There is no packaged tier to
    fall back on, and a name nobody wrote is an error rather than a silent default."""
    mine = installation / "pipelines"
    mine.mkdir(parents=True)
    (mine / "dev.yaml").write_text(
        yaml.safe_dump({"name": "dev", "steps": {"mine": {"uses": "shell", "with": {"run": "true"}, "next": "done"}}})
    )
    repo = a_repo(tmp_path / "myproject")  # ships its own dev.yaml with step "a"
    assert pl.search_path(repo, mine) == [pl.repo_pipelines(repo), mine], "repo first, then global"
    assert list(pl.load(pl.search_path(None, mine), "dev").steps) == ["mine"]
    # A name nobody wrote is an error - there is no packaged copy to fall back on.
    with pytest.raises(FileNotFoundError, match="no pipeline 'quick'"):
        pl.load(pl.search_path(None, mine), "quick")


def test_an_empty_installation_says_none_ship(installation):
    """The first thing a new user meets. "no pipeline 'dev'" alone tells them nothing."""
    empty = installation / "pipelines"
    empty.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="no pipelines anywhere yet"):
        pl.load(pl.search_path(None, empty), "dev")
