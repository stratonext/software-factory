"""Fixtures and helpers every test module here shares.

The two fixtures are injected by pytest; the plain helpers are imported by name, which is
why they live beside the tests rather than in any one of them.
"""

import json
import os
import subprocess

import pytest
import yaml

from software_factory import runners
from software_factory import engine
from software_factory import steps
from software_factory.backend import LocalBackend

# Fails on its first attempt, passes on its second. The counter lives in the item's
# workspace, which survives across stages and across runs.

CLAUDE = runners.load("claude")


def sh(command, **rest):
    """A command step. `uses: shell` on every one of them would say nothing 40 times."""
    return {"uses": "shell", "with": {"run": command}, **rest}


def claude(prompt, **rest):
    """An agent step on the default runner."""
    return {"uses": "claude", "with": {"prompt": prompt}, **rest}


FLAKY = "n=$(cat n 2>/dev/null || echo 0); n=$((n+1)); echo $n > n; [ $n -ge 2 ]"
ASKS_HUMAN = FLAKY + """ || echo '{"verdict": "human", "notes": "which table?"}'"""


def _id(capsys):
    """`factory submit` prints `id: 1  name: ...` to a human; the tests want the id."""
    return capsys.readouterr().out.split("id:")[1].split()[0]


@pytest.fixture(autouse=True)
def _no_ambient_git(monkeypatch):
    """Tests run git; a git hook runs the tests. Neither may see the other's context.

    `task verify` is what .githooks/pre-commit runs, so the suite can be a child of `git
    commit` - which exports GIT_INDEX_FILE=.git/index (relative) and friends to its hooks.
    A `git worktree add` that inherits them resolves `.git/index` inside the new worktree,
    where `.git` is a file, and dies with "index file open failed: Not a directory".
    """
    for var in [v for v in os.environ if v.startswith("GIT_")]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _human_output(monkeypatch):
    """capsys is not a terminal, and piped output is JSON - these tests read the text."""
    monkeypatch.setenv("SF_OUTPUT", "human")


def build(tmp_path, **spec):
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir(exist_ok=True)
    spec.setdefault("name", "t")
    (pipelines / "t.yaml").write_text(yaml.safe_dump(spec))
    return LocalBackend(tmp_path / "state", tmp_path / "worktrees"), str(pipelines)


def drain(backend, pipelines):
    engine.run(backend, pipelines)
    return backend.all()


def _fake_claude(monkeypatch, returncode, stdout, stderr=""):
    """Replace the agent CLI with a fixed reply. Returns the list of calls made."""
    calls = []
    def fake(args, cwd, timeout, env=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    monkeypatch.setattr(steps, "_exec", fake)
    monkeypatch.setattr(steps, "RETRY_WAIT", 0)
    return calls


def _verdict_reply(verdict="pass"):
    return json.dumps(
        {"result": 'done\n{"verdict": "%s", "notes": "n"}' % verdict, "total_cost_usd": 0.1}
    )


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """An isolated ~/.sf, so tests never read - or write - the developer's real one.

    The returned path and SF_HOME must be the same directory. They were not, briefly, and
    every test that wrote a config.yaml here was silently configuring nothing.
    """
    home = tmp_path / "sf_home"
    monkeypatch.setenv("SF_HOME", str(home))
    for var in ("SF_BACKEND", "SF_WORKTREES", "SF_PIPELINES", "SF_PIPELINE"):
        monkeypatch.delenv(var, raising=False)
    return home


def a_repo(path, pipeline_name="dev"):
    """A git repo with one commit and its own pipeline."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("hi\n")
    (path / ".sf" / "pipelines").mkdir(parents=True)
    (path / ".sf" / "pipelines" / ("%s.yaml" % pipeline_name)).write_text(
        yaml.safe_dump({"name": pipeline_name, "steps": {"a": sh("true", next="done")}})
    )
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                                  "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path
