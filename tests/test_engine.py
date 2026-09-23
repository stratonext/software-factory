"""Engine checks. Command steps only - no agents, no tokens, runs in under a second."""

import subprocess
import time

import yaml

import json
from pathlib import Path
from software_factory.__main__ import main
from software_factory import judge
from software_factory import engine
from software_factory import steps
from software_factory.backend import LocalBackend

from conftest import a_repo, ASKS_HUMAN, build, drain, FLAKY, sh


def test_rework_edge_costs_a_pass(tmp_path):
    backend, pipelines = build(
        tmp_path,
        max_passes=3,
        steps={
            "a": sh("true", next="b"),
            "b": sh(FLAKY, on={"pass": "c", "fail": "a"}),
            "c": sh("true", next="done"),
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]
    assert item["status"] == "done", item
    assert item["stage"] == "done"
    assert item["passes"] == 2, "the b -> a edge points backwards, so it is rework"


def test_max_passes_still_counts_rework_round_trips(tmp_path):
    """`passes` is 1-based, so the gate has to subtract: max_passes=1 allows one rework."""
    # Fails twice, so it wants two rework round-trips - one more than allowed.
    twice = "n=$(cat n 2>/dev/null || echo 0); n=$((n+1)); echo $n > n; [ $n -ge 3 ]"
    backend, pipelines = build(
        tmp_path,
        max_passes=1,
        steps={
            "a": sh("true", next="b"),
            "b": sh(twice, on={"pass": "c", "fail": "a"}),
            "c": sh("true", next="done"),
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]
    assert item["status"] == "needs_human" and item["reason"] == "max_passes"
    assert item["passes"] == 3, "pass 1, then the two reworks it asked for"


def test_too_many_passes_parks_for_a_human(tmp_path):
    backend, pipelines = build(
        tmp_path,
        max_passes=0,
        steps={
            "a": sh("true", next="b"),
            "b": sh("false", on={"pass": "c", "fail": "a"}),
            "c": sh("true", next="done"),
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]
    assert item["status"] == "needs_human"
    assert item["reason"] == "max_passes"


def test_a_step_can_flag_for_a_human_and_be_resumed(tmp_path):
    backend, pipelines = build(
        tmp_path,
        max_passes=3,
        steps={
            "a": sh("true", next="b"),
            "b": sh(ASKS_HUMAN, next="c"),
            "c": sh("true", next="done"),
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]
    assert item["status"] == "needs_human"
    assert item["reason"] == "agent flagged"
    assert item["stage"] == "b", "halts at its own stage, no routing"
    assert item["passes"] == 1, "asking a human is not a rework round-trip"
    assert "which table?" in item["notes"][-1]["text"]

    # Resume re-runs that same stage with the human's answer in context.
    item["notes"].append({"stage": "human", "text": "the users table"})
    item["status"] = "queued"
    backend.save(item)
    item = drain(backend, pipelines)[0]
    assert item["status"] == "done"
    assert item["passes"] == 1


def test_items_run_in_parallel(tmp_path):
    backend, pipelines = build(
        tmp_path,
        concurrency=4,
        steps={"a": sh("sleep 0.3", next="done")},
    )
    for _ in range(4):
        backend.create("r", "t", "a")
    start = time.monotonic()
    items = drain(backend, pipelines)
    assert all(i["status"] == "done" for i in items)
    assert time.monotonic() - start < 1.0, "4 x 0.3s sleeps ran concurrently"


def test_every_step_records_its_input_output_and_route(tmp_path):
    backend, pipelines = build(
        tmp_path,
        max_passes=3,
        steps={
            "a": sh("echo hello-from-a", next="b"),
            "b": sh(FLAKY, on={"pass": "done", "fail": "a"}),
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]

    assert item["status"] == "done"
    assert [h["step"] for h in item["history"]] == [1, 2, 3, 4], "b fails once, so a and b run twice"

    first = item["history"][0]
    assert first["kind"] == "command"
    assert first["routed_to"] == "b" and first["rework"] is False
    assert set(first["artifacts"]) == {"input.sh", "output.txt", "exit_code.txt"}
    assert backend.read_artifact(item["id"], first["artifacts"]["input.sh"]) == "echo hello-from-a"
    assert "hello-from-a" in backend.read_artifact(item["id"], first["artifacts"]["output.txt"])

    failed = item["history"][1]
    assert failed["verdict"] == "fail" and failed["rework"] is True and failed["routed_to"] == "a"
    assert failed["duration_s"] >= 0

    # Artifacts of the two `a` runs are separate on the backend, not overwritten.
    assert item["history"][0]["artifacts"]["input.sh"] != item["history"][2]["artifacts"]["input.sh"]


def test_output_writes_to_the_scratch_dir(tmp_path):
    backend, pipelines = build(
        tmp_path, steps={"a": sh("echo hello", output="a.md", next="done")}
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]

    written = backend.workspace(item["id"], "") / steps.SCRATCH / "a.md"
    assert written.read_text() == "hello\n"
    # Also kept per pass on the backend, so every revision stays inspectable.
    assert backend.read_artifact(item["id"], item["history"][0]["artifacts"]["a.md"]) == "hello"


def test_a_later_step_reads_a_wired_input(tmp_path):
    backend, pipelines = build(
        tmp_path,
        steps={
            "a": sh("echo hello", output="a.md", next="b"),
            "b": sh("cat _FACTORY/a.md", input="a.md", next="done"),
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]
    assert item["status"] == "done"
    assert "hello" in backend.read_artifact(
        item["id"], item["history"][1]["artifacts"]["output.txt"]
    )


def test_a_missing_input_is_omitted_not_fatal(tmp_path):
    # `b` reads what `c` writes, so on the first pass the file does not exist yet.
    backend, pipelines = build(
        tmp_path,
        steps={
            "b": sh("true", input="late.md", next="c"),
            "c": sh("echo later", output="late.md", next="done"),
        },
    )
    backend.create("r", "t", "b")
    assert drain(backend, pipelines)[0]["status"] == "done"


def test_verdict_json_is_stripped_from_the_output_file(tmp_path):
    backend, pipelines = build(
        tmp_path,
        steps={
            "a": sh(
                """printf 'the findings\n{"verdict": "pass", "notes": "ok"}\n'""",
                output="a.md", next="done",
            )
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]

    written = (backend.workspace(item["id"], "") / steps.SCRATCH / "a.md").read_text()
    assert written == "the findings\n", "a step reads prose, not the factory's routing object"


def test_scratch_dir_does_not_dirty_the_worktree(tmp_path):
    repo = a_repo(tmp_path / "proj", "t")
    (repo / ".sf" / "pipelines" / "t.yaml").write_text(
        yaml.safe_dump(
            {"name": "t", "steps": {"a": sh("echo hi", output="a.md", next="done")}}
        )
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "p"],
        check=True, capture_output=True,
    )
    backend = LocalBackend(tmp_path / "state", tmp_path / "worktrees")
    backend.create("r", "t", "a", repo=str(repo))
    item = engine.run(backend, tmp_path / "pipelines")[0]

    ws = backend.workspace(item["id"], str(repo))
    assert (ws / steps.SCRATCH / "a.md").exists()
    porcelain = subprocess.run(
        ["git", "-C", str(ws), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert porcelain == "", "_FACTORY ignores itself, so the diff stays the deliverable"


def test_a_review_step_parks_after_routing(tmp_path):
    backend, pipelines = build(
        tmp_path,
        steps={
            "a": sh("true", next="b", review=True),
            "b": sh("true", next="done"),
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]
    assert item["status"] == "needs_human"
    assert item["reason"] == "review after a"
    assert item["stage"] == "b", "routed first, so the next run picks up at the next stage"
    assert item["passes"] == 1, "a review gate is not a rework round-trip"
    assert item["history"][0]["gated"] is True

    # Approving is a plain `run <id>` - no new command, and the gated step does not re-run.
    item["status"] = "queued"
    backend.save(item)
    item = drain(backend, pipelines)[0]
    assert item["status"] == "done"
    assert [h["stage"] for h in item["history"]] == ["a", "b"]


# --- when the agent CLI misbehaves ---------------------------------------------


def test_a_stage_resumes_the_previous_stage_s_session_by_default():
    """The line is one conversation: the coder picks up the planner's thread, so the
    plan it is implementing is context it does not pay for twice."""
    item = {"history": [
        {"stage": "plan", "session_id": "sess-plan"},
        {"stage": "test", "session_id": ""},          # a command step leaves none
        {"stage": "code", "session_id": "sess-code"},
    ]}
    assert engine._resume(item, {}) == "sess-code"
    assert engine._resume(item, {"resume": "plan"}) == "sess-plan"
    assert engine._resume(item, {"resume": False}) is None
    assert engine._resume(item, {"resume": "review"}) is None, "a stage that never ran"
    assert engine._resume({"history": []}, {}) is None



def test_one_unloadable_pipeline_fails_its_own_item_only(tmp_path):
    """A deleted or broken pipeline file used to abort the whole run before it started."""
    backend, pipelines = build(tmp_path, steps={"a": {"uses": "shell", "with": {"run": "true"}, "next": "done"}})
    gone = backend.create("r", "vanished", "a")
    broken = backend.create("r", "half-written", "a")
    (Path(pipelines) / "half-written.yaml").write_text("steps: {a: {uses: shell, with: {run: true}}}\n")
    good = backend.create("r", "t", "a")

    worked = {i["id"]: i for i in engine.run(backend, pipelines)}
    assert worked[good["id"]]["status"] == "done", "a broken neighbour is not its problem"
    assert worked[gone["id"]]["status"] == "failed"
    assert "vanished" in worked[gone["id"]]["reason"]
    assert worked[broken["id"]]["status"] == "failed"
    assert "next" in worked[broken["id"]]["reason"], "the loader's own complaint, verbatim"



def test_a_stage_the_pipeline_does_not_declare_parks(tmp_path):
    """It used to raise out of the worker and leave the item claimed and `running`."""
    backend, pipelines = build(tmp_path, steps={"a": sh("true", next="done")})
    item = backend.create("r", "t", "a")
    item["stage"] = "nope"          # `--stage nope`, or a stage edited out mid-flight
    backend.save(item)

    worked = engine.run(backend, pipelines)
    assert worked[0]["status"] == "needs_human"
    assert "no stage 'nope'" in worked[0]["reason"]


def test_the_route_marks_a_halt_in_the_middle_of_a_run(tmp_path, capsys):
    """History is cumulative, so a halted step that got no separator of its own had the
    next run's stage concatenated onto it: `test -> reviewtest ~~> code`."""
    backend, pipelines = build(
        tmp_path,
        steps={
            "a": {"uses": "shell", "with": {"run": "true"}, "next": "b"},
            "b": {"uses": "shell", "with": {"run": ASKS_HUMAN}, "next": "c"},
            "c": {"uses": "shell", "with": {"run": "true"}, "next": "done"},
        },
    )
    backend.create("r", "t", "a")
    item = drain(backend, pipelines)[0]
    assert item["status"] == "needs_human"
    item["status"] = "queued"
    backend.save(item)
    assert drain(backend, pipelines)[0]["status"] == "done"

    main(["--backend", str(tmp_path / "state"), "replay", "1", "--json"])
    # The machine consumer reads the same string the terminal does.
    assert json.loads(capsys.readouterr().out)["route"] == "a -> b [halted] b -> c -> done"



def test_the_route_of_a_request_that_never_ran_says_so(tmp_path, capsys):
    backend, _pipelines = build(tmp_path, steps={"a": {"uses": "shell", "with": {"run": "true"}, "next": "done"}})
    backend.create("r", "t", "a")
    main(["--backend", str(tmp_path / "state"), "replay", "1", "--json"])
    assert json.loads(capsys.readouterr().out)["route"] == "(not started)"



def test_concurrency_is_the_flag_then_the_pipeline_then_the_setting(
    installation, tmp_path, monkeypatch, capsys
):
    """The CLI used to collapse the flag into the setting before calling the engine, and
    the setting always has a value - so a pipeline's own `concurrency:` never applied."""
    sizes = []
    pool = engine.ThreadPoolExecutor
    monkeypatch.setattr(
        engine, "ThreadPoolExecutor",
        lambda max_workers: sizes.append(max_workers) or pool(max_workers=max_workers),
    )
    repo = a_repo(tmp_path / "myproject")
    installation.mkdir(parents=True, exist_ok=True)
    (installation / "config.yaml").write_text("concurrency: 3\n")
    monkeypatch.chdir(repo)

    main(["submit", "--description", "one", "--name", "one"])
    main(["run", "--wait"])
    assert sizes[-1] == 3, "the pipeline declares none, so the configured default"

    (repo / ".sf" / "pipelines" / "dev.yaml").write_text(yaml.safe_dump(
        {"name": "dev", "concurrency": 4, "steps": {"a": {"uses": "shell", "with": {"run": "true"}, "next": "done"}}}
    ))
    main(["submit", "--description", "two", "--name", "two"])
    main(["run", "--wait"])
    assert sizes[-1] == 4, "the pipeline's own key, which the setting used to shadow"

    main(["submit", "--description", "three", "--name", "three"])
    main(["run", "--wait", "--concurrency", "2"])
    assert sizes[-1] == 2, "an explicit --concurrency beats both"



def _fake_typesafe(monkeypatch, body):
    """Answer the next POST with this exact body. Nothing here reaches the network."""
    class Response:
        def read(self):
            return body
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(judge.urllib.request, "urlopen", lambda *a, **kw: Response())



def test_a_reply_that_is_not_an_object_parks_instead_of_raising(tmp_path, monkeypatch):
    """`null` is valid JSON and is not an answer. Reading a field off it would be an
    AttributeError out of the one path whose whole job is to park rather than guess."""
    spec = tmp_path / "gate.yaml"
    spec.write_text(yaml.safe_dump({
        "questions": {"risky": {"type": "noul", "instructions": "is it risky"}},
        "route": [{"when": "risky", "above": 0.5, "verdict": "fail"}, {"verdict": "pass"}],
    }))
    _fake_typesafe(monkeypatch, b"null")

    result = judge.run_judge(spec, {"request": "r", "notes": []}, tmp_path, 5)
    assert result["verdict"] == "human"
    assert "risky" in result["notes"], "it says which question went unanswered"
    assert judge.triage("add a flag") == {"cost_usd": 0.0}, "triage reads the same reply"
