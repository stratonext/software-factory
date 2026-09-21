"""The global `factory` command: submit, status, run, replay, delete, cancel, config."""

import json
import shutil
import subprocess
import threading
import time

from pathlib import Path

import yaml

from software_factory import config
from software_factory import engine
from software_factory import steps
from software_factory.__main__ import main
from software_factory.backend import LocalBackend

from conftest import a_repo, ASKS_HUMAN, build, drain, sh, _fake_claude, _id, _verdict_reply


def test_replay_prints_the_route_and_opens_a_step(tmp_path, capsys):
    backend, pipelines = build(
        tmp_path,
        steps={"a": sh("echo hi", next="done")},
    )
    backend.create("do a thing", "t", "a")
    drain(backend, pipelines)

    main(["--backend", str(tmp_path / "state"), "--pipelines", pipelines, "replay", "1"])
    out = capsys.readouterr().out
    assert "a -> done" in out
    assert "#1" in out and "command" in out and "pass" in out

    main(["--backend", str(tmp_path / "state"), "replay", "1", "--step", "1"])
    out = capsys.readouterr().out
    assert "echo hi" in out, "the step's exact input"
    assert "hi" in out, "and its output"

    main(["--backend", str(tmp_path / "state"), "replay", "1", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["steps"] == 1
    assert data["steps"][0]["artifacts"]["input.sh"].startswith("steps/001-a/")


def test_replay_shows_a_halted_step_as_halted(tmp_path, capsys):
    backend, pipelines = build(
        tmp_path,
        steps={"a": sh(ASKS_HUMAN, next="done")},
    )
    backend.create("r", "t", "a")
    drain(backend, pipelines)

    main(["--backend", str(tmp_path / "state"), "replay", "1"])
    out = capsys.readouterr().out
    assert "HALTED" in out
    assert "[needs_human]" in out
    assert "parked: agent flagged" in out


def test_cost_is_optional_and_totalled_per_pipeline(tmp_path, capsys, monkeypatch):
    """A command step has no cost, not a zero one - "-" beats a free-looking $0.00."""
    backend, pipelines = build(
        tmp_path,
        steps={"a": sh("echo hi", next="done")},
    )
    backend.create("r", "t", "a")
    drain(backend, pipelines)

    main(["--backend", str(tmp_path / "state"), "replay", "1"])
    out = capsys.readouterr().out
    assert "$0.00" not in out and "-" in out

    main(["--backend", str(tmp_path / "state"), "replay", "1", "--json"])
    assert json.loads(capsys.readouterr().out)["totals"]["cost_usd"] is None

    main(["--backend", str(tmp_path / "state"), "status"])
    out = capsys.readouterr().out
    assert "COST" in out and "$0.00" not in out

    # An agent step does report one, and it lands on the line and in the total.
    (tmp_path / "two").mkdir()
    backend2, pipelines2 = build(
        tmp_path / "two",
        steps={"a": {"uses": "claude", "with": {"prompt": "plan.md"}, "next": "done"}},
    )
    (tmp_path / "two" / "pipelines" / "plan.md").write_text("you are a planner")
    _fake_claude(monkeypatch, 0, _verdict_reply())
    backend2.create("r", "t", "a")
    drain(backend2, pipelines2)

    main(["--backend", str(tmp_path / "two" / "state"), "replay", "1"])
    out = capsys.readouterr().out
    assert out.count("$0.10") == 2, "the step's own cost, and the pipeline total"


# --- wiring steps together: input: / output: -----------------------------------


def test_submit_defaults_to_the_current_repo_and_records_it(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "myproject")
    monkeypatch.chdir(repo)
    assert main(["submit", "add a flag", "--name", "add-flag"]) == 0
    item_id = _id(capsys)

    backend = LocalBackend(installation / "state", installation / "worktrees")
    item = backend.load(item_id)
    assert item["repo"] == str(repo), "the repo belongs to the request, not to the factory"
    assert item["pipeline"] == "dev"
    assert item["name"] == "add-flag"


def test_submit_requires_a_name(installation, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    assert main(["submit", "add a flag"]) == 2, "a usage error is an exit code, not a traceback"
    assert "--name" in capsys.readouterr().err


def test_submit_run_queues_and_works_it_in_one_command(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "myproject")  # its dev.yaml is a command step, so no agent runs
    monkeypatch.chdir(repo)
    assert main(["submit", "a thing", "--name", "a-thing", "--run"]) == 0
    out = capsys.readouterr().out

    backend = LocalBackend(installation / "state", installation / "worktrees")
    assert backend.load("1")["status"] == "done", "submitted and worked in one command"
    assert out.count("id: 1") == 2, "the id line, then the run result line"


def test_submit_refuses_an_overlong_name(installation, tmp_path, monkeypatch, capsys):
    """A name is a label for one status column, not a second request."""
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    assert main(["submit", "a thing", "--name", "x" * 40]) == 0, "40 is allowed"
    capsys.readouterr()
    assert main(["submit", "a thing", "--name", "x" * 41]) == 1
    assert "41 characters" in capsys.readouterr().err


def test_submit_reads_the_request_from_a_file(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "myproject")
    monkeypatch.chdir(repo)
    request = tmp_path / "request.md"
    request.write_text("# rewrite the uploader\n\nit must stream, not buffer.\n")
    assert main(["submit", "--file", str(request), "--name", "uploader"]) == 0
    item_id = _id(capsys)

    backend = LocalBackend(installation / "state", installation / "worktrees")
    assert backend.load(item_id)["request"] == request.read_text().strip()

    assert main(["submit", "--file", "", "--name", "empty"]) == 1, "an empty --file must not traceback"
    assert capsys.readouterr().err.startswith("sf: ")


def test_submit_refuses_a_repo_with_no_commits(installation, tmp_path, monkeypatch, capsys):
    bare = tmp_path / "uncommitted"
    bare.mkdir()
    subprocess.run(["git", "-C", str(bare), "init", "-q"], check=True, capture_output=True)
    monkeypatch.chdir(bare)
    assert main(["submit", "do a thing", "--name", "thing"]) == 1, "caught at submit, not halfway through a run"
    assert "at least one commit" in capsys.readouterr().err


def test_status_lists_every_repo_in_flight(installation, tmp_path, monkeypatch, capsys):
    for name in ("alpha", "beta"):
        monkeypatch.chdir(a_repo(tmp_path / name))
        main(["submit", "work on %s" % name, "--name", name])
    capsys.readouterr()

    main([])  # bare `factory`
    out = capsys.readouterr().out
    assert "REPO" in out and "PIPELINE" in out and "STAGE" in out, "the table is the default"
    assert "alpha" in out and "beta" in out, "one installation, many repos"


def test_status_shows_the_stage_position_in_the_pipeline(installation, tmp_path, monkeypatch, capsys):
    """`a 1/1` - which stage, and how far through the pipeline that is."""
    repo = a_repo(tmp_path / "myproject")  # one step, "a"
    monkeypatch.chdir(repo)
    main(["submit", "a thing", "--name", "a-thing"])
    capsys.readouterr()

    main([])
    assert "a 0/1" in capsys.readouterr().out, "queued: it has not worked that stage yet"

    main(["run", "1"])
    capsys.readouterr()
    main([])
    assert "done 1/1" in capsys.readouterr().out, "done is not a step, so it reads N/N"

    (repo / ".sf" / "pipelines" / "dev.yaml").unlink()  # pipeline gone: still lists, bare stage
    main([])
    out = capsys.readouterr().out
    assert "done" in out and "done 1/1" not in out


def test_worktrees_are_centralised_by_repo(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "myproject")
    monkeypatch.chdir(repo)
    main(["submit", "a thing", "--name", "thing"])
    item_id = _id(capsys)

    backend = LocalBackend(installation / "state", installation / "worktrees")
    ws = backend.workspace(item_id, str(repo))
    assert ws == installation / "worktrees" / "myproject" / item_id
    assert (ws / "README.md").exists(), "a real worktree, populated from the repo"


def test_delete_drops_a_request_but_refuses_a_running_one(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "myproject")
    monkeypatch.chdir(repo)
    main(["submit", "cancel me", "--name", "cancel-me"])
    main(["submit", "leave me alone", "--name", "leave-me-alone"])
    capsys.readouterr()
    backend = LocalBackend(installation / "state", installation / "worktrees")

    ws = backend.workspace("1", str(repo))
    (ws / "agent.txt").write_text("work\n")
    for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "agent work"]):
        subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "sf/1"],
                          capture_output=True, text=True).stdout

    assert main(["delete", "1"]) == 0
    assert "id: 1  status: deleted" in capsys.readouterr().out
    assert [i["id"] for i in backend.all()] == ["2"]
    assert not ws.exists(), "the worktree goes with the request"
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                            capture_output=True, text=True).stdout
    assert str(ws) not in listed, "and git is not left advertising it"
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "sf/1"],
                              capture_output=True, text=True).stdout
    assert "sf/1" in branches, "committed work survives a cancel; only the checkout goes"

    backend.claim(backend.load("2"))
    assert main(["delete", "2"]) == 1, "a running step would write the item back"
    assert "--force" in capsys.readouterr().err
    assert backend.load("2")["status"] == "running"

    assert main(["delete", "2", "--force"]) == 0
    assert backend.all() == []
    capsys.readouterr()

    assert main(["delete", "404"]) == 1
    assert "no such request: 404" in capsys.readouterr().err

    main(["submit", "next one", "--name", "next-one"])
    assert _id(capsys) == "3", "a deleted id is never handed out again"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "sf/1"],
                          capture_output=True, text=True).stdout == head, \
        "because reuse would reset the preserved branch to HEAD"


def test_info_reports_the_settings_in_effect(installation, capsys):
    main(["config"])
    out = capsys.readouterr().out
    assert str(installation / "state") in out
    assert str(installation / "worktrees") in out
    assert "not created" in out, "no config.yaml yet, and it says so"


def test_run_can_be_scoped_to_one_repo_or_request(installation, tmp_path, monkeypatch, capsys):
    """A global queue is useless without a way to work one slice of it."""
    for name in ("alpha", "beta"):
        monkeypatch.chdir(a_repo(tmp_path / name))
        main(["submit", "work on %s" % name, "--name", name])
    capsys.readouterr()

    backend = LocalBackend(installation / "state", installation / "worktrees")
    done = engine.run(backend, installation / "pipelines", repo=str(tmp_path / "alpha"))
    assert [i["repo"] for i in done] == [str(tmp_path / "alpha")]
    assert backend.load("2")["status"] == "queued", "beta was left alone"

    done = engine.run(backend, installation / "pipelines", ids=["2"])
    assert [i["id"] for i in done] == ["2"]


def test_run_returns_before_the_work_is_finished(installation, tmp_path, monkeypatch, capsys):
    """A pipeline is minutes of agent time; the CLI must not hold the terminal for it."""
    repo = a_repo(tmp_path / "slow")
    (repo / ".sf" / "pipelines" / "dev.yaml").write_text(
        yaml.safe_dump({"name": "dev", "steps": {"a": sh("sleep 2", next="done")}})
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "p"],
        check=True, capture_output=True,
    )
    monkeypatch.chdir(repo)
    main(["submit", "slow work", "--name", "slow-work"])
    item_id = _id(capsys)

    started = time.monotonic()
    assert main(["run", "--detach"]) == 0
    assert time.monotonic() - started < 1.5, "run --detach blocked on the pipeline"
    assert "id: %s  status: started" % item_id in capsys.readouterr().out

    backend = LocalBackend(installation / "state", installation / "worktrees")
    deadline = time.monotonic() + 60
    while backend.load(item_id)["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, "the detached run never finished"
        time.sleep(0.2)
    assert backend.load(item_id)["status"] == "done", "and it really did the work"
    assert (installation / "state" / "run.log").exists()


def test_run_waits_when_asked(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "quick")
    monkeypatch.chdir(repo)
    main(["submit", "quick work", "--name", "quick-work"])
    item_id = _id(capsys)

    assert main(["run"]) == 0
    backend = LocalBackend(installation / "state", installation / "worktrees")
    assert backend.load(item_id)["status"] == "done", "a blocking run needs no polling"


def test_run_with_an_id_unparks_a_request_and_works_it(installation, tmp_path, monkeypatch, capsys):
    """The whole human loop is one command: answer the agent, the line picks up again."""
    repo = a_repo(tmp_path / "myproject")
    (repo / ".sf" / "pipelines" / "dev.yaml").write_text(
        yaml.safe_dump({"name": "dev", "steps": {"a": sh(ASKS_HUMAN, next="done")}})
    )
    monkeypatch.chdir(repo)
    main(["submit", "add a flag", "--name", "add-flag"])
    item_id = _id(capsys)
    backend = LocalBackend(installation / "state", installation / "worktrees")

    main(["run", item_id])
    assert backend.load(item_id)["status"] == "needs_human"

    assert main(["run", item_id, "--note", "the users table"]) == 0
    item = backend.load(item_id)
    assert item["status"] == "done", "unparked and worked in the same call"
    assert item["passes"] == 1, "a human unblocking an agent is not a rework pass"
    assert item["notes"][-1]["text"] == "the users table"

    assert main(["run", item_id]) == 1, "finished: `done` is not a stage to re-enter"
    assert "--stage" in capsys.readouterr().err
    assert main(["run", "--note", "to whom?"]) == 1, "a note needs a request id"
    assert main(["run", "999"]) == 1, "a mistyped id is a message, not a traceback"
    assert "no such request: 999" in capsys.readouterr().err


def _await_step(tmp_path, item_id):
    """Block until that item's step is actually running, and return its pid file."""
    pid_file = tmp_path / "worktrees" / "_scratch" / item_id / steps.SCRATCH / steps.PID_FILE
    for _ in range(200):
        if pid_file.exists():
            break
        time.sleep(0.05)
    assert pid_file.exists(), "the step never started"
    return pid_file


def test_cancel_kills_the_running_step(tmp_path):
    """Cancel means now: the in-flight step is signalled, not waited out."""
    backend, pipelines = build(tmp_path, steps={"a": sh("sleep 30", next="done")})
    backend.create("r", "t", "a")
    walker = threading.Thread(target=engine.run, args=(backend, pipelines), daemon=True)
    walker.start()

    pid_file = _await_step(tmp_path, "1")

    assert main(["--backend", str(tmp_path / "state"), "--worktrees",
                 str(tmp_path / "worktrees"), "cancel", "1"]) == 0
    walker.join(timeout=10)
    assert not walker.is_alive(), "the step outlived its cancel"

    item = backend.load("1")
    assert item["status"] == "cancelled", item
    assert item["reason"].startswith("cancelled ")
    assert not pid_file.exists(), "the pid file outlived the step"


def test_cancel_reaches_an_item_waiting_in_the_backlog(tmp_path):
    """An item queued behind a busy pool: claiming it must not overwrite the cancel."""
    backend, pipelines = build(tmp_path, steps={"a": sh("sleep 30", next="done")})
    backend.create("first", "t", "a")
    backend.create("second", "t", "a")
    walker = threading.Thread(target=engine.run, args=(backend, pipelines, 1), daemon=True)
    walker.start()
    _await_step(tmp_path, "1")  # the pool's one worker is busy; 2 waits

    cli = ["--backend", str(tmp_path / "state"), "--worktrees", str(tmp_path / "worktrees")]
    assert main(cli + ["cancel", "2"]) == 0  # queued: flagged, nothing to signal
    assert main(cli + ["cancel", "1"]) == 0  # frees the worker, ending the run
    walker.join(timeout=10)
    assert not walker.is_alive(), "the step outlived its cancel"

    waiting = backend.load("2")
    assert waiting["status"] == "cancelled", waiting
    assert waiting["history"] == [], "a cancelled item was walked anyway"


def test_cancel_lands_while_the_worktree_is_being_created(tmp_path):
    """`workspace()` is a real `git worktree add`: a cancel during it must not be lost."""
    entered = threading.Event()

    class SlowWorktree(LocalBackend):
        def workspace(self, item_id, repo=""):
            entered.set()
            time.sleep(1)  # the window the real `git worktree add` leaves open
            return super().workspace(item_id, repo)

    _, pipelines = build(tmp_path, steps={"a": sh("sleep 5", next="done")})
    backend = SlowWorktree(tmp_path / "state", tmp_path / "worktrees")
    backend.create("r", "t", "a")
    walker = threading.Thread(target=engine.run, args=(backend, pipelines), daemon=True)
    walker.start()
    assert entered.wait(timeout=5), "the walk never reached the workspace"

    assert main(["--backend", str(tmp_path / "state"), "--worktrees",
                 str(tmp_path / "worktrees"), "cancel", "1"]) == 0
    walker.join(timeout=10)
    assert not walker.is_alive()

    item = backend.load("1")
    assert item["status"] == "cancelled", item
    assert item["history"] == [], "a cancelled item was walked anyway"


def test_cancel_refuses_a_finished_request(tmp_path):
    backend, pipelines = build(tmp_path, steps={"a": sh("true", next="done")})
    backend.create("r", "t", "a")
    assert drain(backend, pipelines)[0]["status"] == "done"
    assert main(["--backend", str(tmp_path / "state"), "cancel", "1"]) == 1
    assert backend.load("1")["status"] == "done"


def test_a_cancelled_request_is_resumable(tmp_path):
    backend, pipelines = build(tmp_path, steps={"a": sh("true", next="done")})
    backend.create("r", "t", "a")
    assert main(["--backend", str(tmp_path / "state"), "cancel", "1"]) == 0
    assert backend.load("1")["status"] == "cancelled"
    assert drain(backend, pipelines)[0]["status"] == "cancelled", "cancelled is not queued"

    main(["--backend", str(tmp_path / "state"), "--pipelines", pipelines,
          "--worktrees", str(tmp_path / "worktrees"), "run", "1"])
    assert backend.load("1")["status"] == "done", "`run <id>` picks a cancelled request back up"


def test_the_cli_surface_survives(installation, tmp_path, monkeypatch, capsys):
    """The two things the Typer rewrite can break that nothing else covers."""
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    main(["submit", "one thing", "--name", "one-thing"])
    capsys.readouterr()

    assert main(["run", "--repo"]) == 0, "`--repo` with no value still means 'here'"
    assert "id: 1" in capsys.readouterr().out

    assert main(["--help"]) == 0, "help prints and returns, it does not exit the process"
    assert "submit" in capsys.readouterr().out


def test_status_keeps_one_line_per_item(installation, tmp_path, monkeypatch, capsys):
    """A reason longer than the terminal must not be folded: `factory status | grep` reads it."""
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    main(["submit", "one thing", "--name", "one-thing"])
    backend = LocalBackend(installation / "state", installation / "worktrees")
    item = backend.load("1")
    item["status"] = "needs_human"
    item["reason"] = "could not decide whether the flag defaults to true or false, please advise"
    backend.save(item)
    capsys.readouterr()

    main(["status"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2, "a header and one line per item, whatever the reason's length"
    assert lines[0].startswith("REQUEST"), "the table is what `status` prints by default"
    assert lines[1].startswith("1") and lines[1].endswith(item["reason"])
    assert lines[1] == lines[1].rstrip(), "no trailing padding"

    capsys.readouterr()
    main(["status", "--detailed"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 3, "-d is the block form: three lines for the one item"
    assert "repo:" in lines[1] and "request:" in lines[2]


def test_status_shows_every_version_a_request_ran_under(installation, tmp_path, monkeypatch, capsys):
    """A pipeline file can be edited mid-flight, so one request can span two versions."""
    repo = a_repo(tmp_path / "myproject")
    monkeypatch.chdir(repo)
    definition = repo / ".sf" / "pipelines" / "dev.yaml"
    definition.write_text(yaml.safe_dump(
        {"name": "dev", "version": 1,
         "steps": {"a": sh("true", next="b"), "b": sh("true", next="done")}}
    ))
    main(["submit", "one thing", "--name", "one-thing"])
    backend = LocalBackend(installation / "state", installation / "worktrees")

    capsys.readouterr()
    main(["status"])
    assert "dev@" not in capsys.readouterr().out, "nothing has run yet: no version to show"

    main(["run"])
    assert [h["version"] for h in backend.load("1")["history"]] == [1, 1]
    capsys.readouterr()
    main(["status"])
    assert "dev@1" in capsys.readouterr().out

    # Edit the pipeline, then re-enter the finished request at its second stage.
    definition.write_text(definition.read_text().replace("version: 1", "version: 2"))
    main(["run", "1", "--stage", "b"])
    assert [h["version"] for h in backend.load("1")["history"]] == [1, 1, 2]
    capsys.readouterr()
    main(["status"])
    assert "dev@1,2" in capsys.readouterr().out, "both versions, in the order they ran"

    capsys.readouterr()
    main(["config"])
    assert "dev v2" in capsys.readouterr().out, "info lists what is on disk now"


def test_info_prints_paths_verbatim(installation, tmp_path, monkeypatch, capsys):
    """Brackets in a path are data, not markup - Rich would eat them silently."""
    home = tmp_path / "br[x]"
    monkeypatch.setenv("SF_HOME", str(home))
    assert main(["config"]) == 0
    assert str(home / "state") in capsys.readouterr().out


def test_skill_flag_prints_the_skill_for_redirecting_into_claude_skills(capsys):
    assert main(["--skill"]) == 0
    assert capsys.readouterr().out.startswith("---\nname: sf\n")


def test_a_fresh_install_has_no_pipelines_and_says_so(installation, tmp_path, monkeypatch, capsys):
    """Nothing ships: the factory is the engine and the process is the user's. So a fresh
    install cannot work anything yet, and the one thing it owes them is saying where to
    write one - `no pipeline 'quick'` on its own sends people looking for a broken install.
    """
    repo = a_repo(tmp_path / "bare")
    shutil.rmtree(repo / ".sf" / "pipelines")  # none in the repo, and none in ~/.sf either
    monkeypatch.chdir(repo)
    assert main(["submit", "a thing", "--name", "thing", "--pipeline", "quick"]) == 1
    err = capsys.readouterr().err
    assert "no pipelines anywhere yet" in err and ".sf/pipelines" in err, err



def test_init_creates_the_installation_and_is_safe_to_repeat(installation, capsys):
    config_yaml = installation / "config.yaml"
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "created: %s" % config_yaml in out
    assert yaml.safe_load(config_yaml.read_text())["pipeline"] == "dev"

    # The directories the process will live in, made and named. Empty on purpose: no
    # pipeline ships, so there is nothing to seed and nothing of the user's to overwrite.
    for d in ("pipelines", "runners"):
        assert (installation / d).is_dir(), d
        assert not list((installation / d).iterdir()), "%s must be empty: nothing ships" % d

    config_yaml.write_text("concurrency: 9\n")
    assert main(["init"]) == 0
    assert "unchanged: %s" % config_yaml in capsys.readouterr().out
    assert "9" in config_yaml.read_text(), "a second init reports, it does not overwrite"

    assert main(["init", "--force"]) == 0
    assert "concurrency: 9" not in config_yaml.read_text()



def test_every_setting_has_a_default_and_a_starter_config_entry():
    """The configuration surface is one list: what defaults() knows, the example shows."""
    settings = config.defaults()
    assert set(config.ENV) <= set(settings), "an env var for a setting that does not exist"
    written = yaml.safe_load(config.example())
    # The paths are commented out on purpose - left unset they follow SF_HOME.
    assert set(written) == set(settings) - set(config.PATHS)
    assert all(written[k] == settings[k] for k in written), written



def test_the_module_constants_are_the_defaults():
    """Each module keeps its own constant for direct callers; the two must not drift."""
    d = config.defaults()
    assert (d["step_timeout"], d["agent_attempts"], d["retry_wait"], d["max_input"]) == (
        engine.STEP_TIMEOUT, steps.AGENT_ATTEMPTS, steps.RETRY_WAIT, steps.MAX_INPUT)



def test_an_integer_setting_written_as_text_is_still_an_integer(installation, capsys):
    """YAML keeps "2" a string and the environment has nothing else; ThreadPoolExecutor
    and subprocess timeouts want numbers, so the coercion happens once, at load."""
    installation.mkdir(parents=True)
    (installation / "config.yaml").write_text('concurrency: "4"\nstep_timeout: "60"\n')
    assert config.load()["concurrency"] == 4
    assert config.load()["step_timeout"] == 60

    (installation / "config.yaml").write_text("concurrency: two\n")
    assert main(["config"]) == 1
    err = capsys.readouterr().err
    assert "concurrency" in err and str(installation / "config.yaml") in err, \
        "a bad value has to say which key and which file"



def test_a_promoted_setting_reaches_the_step_it_caps(installation, tmp_path, monkeypatch, capsys):
    """The point of promoting step_timeout: it has to arrive at the subprocess that runs
    the step, not just at `factory config`."""
    repo = a_repo(tmp_path / "slow")
    (repo / ".sf" / "pipelines" / "dev.yaml").write_text(
        yaml.safe_dump({"name": "dev", "steps": {"a": {"uses": "shell", "with": {"run": "sleep 30"}, "next": "done"}}})
    )
    installation.mkdir(parents=True, exist_ok=True)
    (installation / "config.yaml").write_text("step_timeout: 1\n")
    monkeypatch.chdir(repo)
    main(["submit", "sit there", "--name", "slow"])
    main(["run", "1"])
    capsys.readouterr()

    item = LocalBackend(installation / "state", installation / "worktrees").load("1")
    assert "timed out after 1s" in item["history"][0]["notes"], item["history"]



def test_the_name_cap_is_a_setting(installation, tmp_path, monkeypatch, capsys):
    installation.mkdir(parents=True)
    (installation / "config.yaml").write_text("name_max: 4\n")
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    assert main(["submit", "add a flag", "--name", "add-flag"]) == 1
    assert "keep it to 4 or fewer" in capsys.readouterr().err




def test_prune_clears_done_requests_and_their_branches(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "myproject")  # its dev.yaml is one command step, so it runs to done
    monkeypatch.chdir(repo)
    main(["submit", "finished", "--name", "one", "--run"])
    main(["submit", "not started", "--name", "two"])
    capsys.readouterr()
    backend = LocalBackend(installation / "state", installation / "worktrees")
    ws = Path(backend.load("1")["workspace"])

    assert main(["prune", "--status", "nonsense"]) == 1
    assert "needs_human" in capsys.readouterr().err, "an unknown status names the ones that exist"

    assert main(["prune", "--older-than", "7d"]) == 0
    assert "nothing to prune" in capsys.readouterr().out, "it ended seconds ago"

    assert main(["prune"]) == 0
    out = capsys.readouterr().out
    assert "id: 1" in out and "state: deleted" in out
    assert "deleted: 1" in out and "freed: " in out and "skipped: 0" in out
    assert [i["id"] for i in backend.all()] == ["2"], "done is the default; queued was not asked for"
    assert not ws.exists(), "the worktree goes with the request"
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "sf/1"],
                              capture_output=True, text=True).stdout
    assert branches == "", "and so does the branch it would otherwise leave behind"



def test_prune_never_deletes_a_running_request(installation, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    main(["submit", "busy", "--name", "busy"])
    backend = LocalBackend(installation / "state", installation / "worktrees")
    backend.claim(backend.load("1"))
    capsys.readouterr()

    assert main(["prune", "--status", "running"]) == 0, "asking for it is not an error, doing it is"
    out = capsys.readouterr().out
    assert "state: skipped" in out and "skipped: 1 running" in out
    assert backend.load("1")["status"] == "running", "a live step would write the item back"



def test_prune_dry_run_deletes_nothing(installation, tmp_path, monkeypatch, capsys):
    repo = a_repo(tmp_path / "myproject")
    monkeypatch.chdir(repo)
    main(["submit", "finished", "--name", "one", "--run"])
    capsys.readouterr()
    backend = LocalBackend(installation / "state", installation / "worktrees")
    ws = Path(backend.load("1")["workspace"])

    assert main(["prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "id: 1" in out and "would delete" in out

    assert backend.load("1")["status"] == "done"
    assert ws.exists()
    assert "sf/1" in subprocess.run(["git", "-C", str(repo), "branch", "--list", "sf/1"],
                                         capture_output=True, text=True).stdout




def test_version_is_the_installed_distributions(capsys):
    """One version for the whole project: pyproject.toml, read back through the metadata."""
    from importlib.metadata import version

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == version("software-factory")



def test_a_global_option_works_after_the_subcommand(installation, tmp_path, monkeypatch, capsys):
    """`factory status --backend X` is what everyone types first, and click binds it to the app."""
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    main(["submit", "one thing", "--name", "one-thing"])
    capsys.readouterr()

    assert main(["status", "--backend", str(tmp_path / "elsewhere")]) == 0
    assert "no work items" in capsys.readouterr().out, "the option took effect where it was typed"

    assert main(["submit", "--name", "dashes", "--", "--backend is a word here"]) == 0
    backend = LocalBackend(installation / "state", installation / "worktrees")
    assert backend.load("2")["request"] == "--backend is a word here", "`--` still ends the options"



def test_an_unknown_id_is_the_same_message_from_every_command(installation, capsys):
    """Not a FileNotFoundError traceback with a backend path in it."""
    for command in (["show", "99"], ["replay", "99"], ["cancel", "99"], ["delete", "99"]):
        assert main(command) == 1, command
        assert "no such request: 99" in capsys.readouterr().err, command



def test_cancel_takes_many_ids_and_delete_emits_one_array(installation, tmp_path, monkeypatch, capsys):
    """Both take a list, and both print one array - not a JSON object per id, which no jq reads."""
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    for label in ("one", "two"):
        main(["submit", label, "--name", label])
    capsys.readouterr()

    assert main(["cancel", "1", "2", "99"]) == 1, "the bad id is reported, the good ones still stop"
    assert "no such request: 99" in capsys.readouterr().err
    backend = LocalBackend(installation / "state", installation / "worktrees")
    assert [i["status"] for i in backend.all()] == ["cancelled", "cancelled"]

    assert main(["delete", "1", "2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"id": "1", "status": "deleted"}, {"id": "2", "status": "deleted"}
    ], "one array, and the same `status` key the other commands use"



def test_run_wait_exits_non_zero_when_nothing_reached_done(installation, tmp_path, monkeypatch, capsys):
    """`factory run` is the CI gate: a queue that ended parked or failed must not exit 0."""
    repo = a_repo(tmp_path / "myproject")
    (repo / ".sf" / "pipelines" / "dev.yaml").write_text(
        yaml.safe_dump({"name": "dev", "steps": {"a": sh("exit 3", on={"pass": "done"})}})
    )
    monkeypatch.chdir(repo)
    main(["submit", "will not pass", "--name", "nope"])

    assert main(["run"]) == 1, "it parked; nothing reached done"
    assert main(["run"]) == 0, "an empty queue is not a failure"



def test_replay_says_no_instead_of_quietly_doing_something_else(installation, tmp_path, monkeypatch, capsys):
    """--artifact without --step was ignored, and --step 0 fell through to the whole run."""
    monkeypatch.chdir(a_repo(tmp_path / "myproject"))
    main(["submit", "one thing", "--name", "one-thing"])
    main(["run"])
    capsys.readouterr()

    assert main(["replay", "1", "--artifact", "output.txt"]) == 1
    assert "--step" in capsys.readouterr().err
    assert main(["replay", "1", "--step", "0"]) == 1
    assert "no step 0" in capsys.readouterr().err

    assert main(["replay", "1", "--step", "1"]) == 0
    assert "other artifacts" not in capsys.readouterr().out, "it printed every one of them"



def test_doctor_names_what_is_missing_and_exits_non_zero(installation, tmp_path, monkeypatch, capsys):
    """The command a new user runs when nothing works: one line per check, each with a fix."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    assert main(["doctor"]) == 1, "no token, and no pipelines to run anything through"
    report = capsys.readouterr().out
    assert "claude setup-token" in report, "it says what to do, not just what is wrong"
    assert "pipelines" in report
    assert "TYPESAFE_API_KEY" not in report, "nothing here judges: the key is not needed"

    judging = tmp_path / "judging"
    judging.mkdir()
    (judging / "j.yaml").write_text(yaml.safe_dump(
        {"name": "j", "steps": {"g": {"uses": "typesafe", "with": {"questions": "g.yaml"}, "next": "done"}}}
    ))
    assert main(["doctor", "--pipelines", str(judging)]) == 1
    report = capsys.readouterr().out
    assert "j: v1, 1 step(s), loads" in report, "and which pipelines resolve"
    assert "TYPESAFE_API_KEY" in report, "asked for once a pipeline judges"


def test_an_unreadable_config_is_an_error_message_not_a_traceback(installation, capsys):
    """config.yaml is hand-edited - `factory init` seeds it and says so - so a stray
    indent is a user mistake, and a scanner traceback is the wrong way to report one."""
    installation.mkdir(parents=True)
    (installation / "config.yaml").write_text("pipeline: dev\n  bad indent: [\n")
    assert main(["config"]) == 1
    assert str(installation / "config.yaml") in capsys.readouterr().err

    # A whole document of the wrong shape, which parses cleanly and then is not settings.
    (installation / "config.yaml").write_text("- just\n- a list\n")
    assert main(["config"]) == 1
    assert "mapping" in capsys.readouterr().err
