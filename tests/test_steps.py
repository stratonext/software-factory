"""One agent step in isolation, mostly for when the agent CLI misbehaves."""

import json
import subprocess

from software_factory import steps

from conftest import CLAUDE, _fake_claude, _verdict_reply


def test_build_prompt_injects_declared_inputs():
    item = {"request": "add a flag", "passes": 1, "notes": [], "history": []}
    wired = steps.build_prompt("your role", item, "code", [("plan.md", "touch two files")])
    assert "# Input: plan.md\ntouch two files" in wired
    # An unwired pipeline - or one with no plan at all - gets no input section.
    assert "# Input:" not in steps.build_prompt("your role", item, "code")


def test_read_inputs_skips_a_file_its_producer_has_not_written_yet(tmp_path):
    steps.ensure_scratch(tmp_path)
    steps.write_scratch(tmp_path, "plan.md", "the plan")
    assert steps.read_inputs(tmp_path, ["plan.md", "review.md"]) == [("plan.md", "the plan\n")]


def _agent(tmp_path, resume=None):
    (tmp_path / "role.md").write_text("you are a planner")
    item = {"request": "r", "passes": 1, "notes": [], "history": []}
    return steps.run_agent(CLAUDE, tmp_path / "role.md", item, "plan", tmp_path, 5, resume=resume)


def test_an_agent_step_records_its_exit_code_and_invocation(tmp_path, monkeypatch):
    """A step that printed nothing is only debuggable if we kept how it was called."""
    _fake_claude(monkeypatch, 0, "")
    result = _agent(tmp_path)
    assert result["verdict"] == "error"
    assert "printed nothing" in result["notes"]
    assert result["artifacts"]["exit_code.txt"] == "0"
    assert "claude -p" in result["artifacts"]["command.txt"]


def test_a_non_json_reply_is_quoted_in_the_notes(tmp_path, monkeypatch):
    _fake_claude(monkeypatch, 0, "I cannot do that")
    result = _agent(tmp_path)
    assert result["verdict"] == "error"
    assert "I cannot do that" in result["notes"]


def test_a_failing_agent_cli_reports_its_stderr(tmp_path, monkeypatch):
    _fake_claude(monkeypatch, 1, "", "--dangerously-skip-permissions cannot be used with root")
    result = _agent(tmp_path)
    assert result["verdict"] == "error"
    assert "exited 1" in result["notes"] and "root" in result["notes"]
    assert "root" in result["artifacts"]["stderr.txt"]


def test_an_api_error_reply_is_not_treated_as_a_verdict(tmp_path, monkeypatch):
    _fake_claude(monkeypatch, 0, json.dumps({"is_error": True, "result": "overloaded_error"}))
    result = _agent(tmp_path)
    assert result["verdict"] == "error"
    assert "overloaded_error" in result["notes"]


def test_a_blank_reply_is_retried_and_the_attempts_recorded(tmp_path, monkeypatch):
    """The failure that parked 003: exit 0, nothing on either stream, 4 seconds."""
    replies = ["", "", _verdict_reply()]
    calls = []
    def fake(args, cwd, timeout, env=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, replies[len(calls) - 1], "")
    monkeypatch.setattr(steps, "_exec", fake)
    monkeypatch.setattr(steps, "RETRY_WAIT", 0)

    result = _agent(tmp_path)
    assert result["verdict"] == "pass", "a blip must not fail the whole request"
    assert len(calls) == 3
    assert result["artifacts"]["attempts.txt"] == "3"
    assert result["notes"] == "n", "a successful retry leaves the agent's own notes alone"


def test_retrying_stops_after_three_attempts(tmp_path, monkeypatch):
    calls = _fake_claude(monkeypatch, 0, "")
    result = _agent(tmp_path)
    assert len(calls) == steps.AGENT_ATTEMPTS == 3
    assert result["verdict"] == "error"
    assert "after 3 attempts" in result["notes"]


def test_a_configuration_failure_is_not_retried(tmp_path, monkeypatch):
    """Three identical auth or root failures say nothing the first one did not."""
    calls = _fake_claude(monkeypatch, 1, "", "cannot be used with root/sudo privileges")
    assert _agent(tmp_path)["verdict"] == "error"
    assert len(calls) == 1


def test_a_session_id_is_recorded_and_handed_back_on_resume(tmp_path, monkeypatch):
    """The session id is the step's metadata - a stage re-entered picks its thread up."""
    reply = json.loads(_verdict_reply())
    reply["session_id"] = "sess-1"
    calls = _fake_claude(monkeypatch, 0, json.dumps(reply))

    assert _agent(tmp_path)["session_id"] == "sess-1"
    assert "--resume" not in calls[0], "nothing to resume on the first run of a stage"

    result = _agent(tmp_path, resume="sess-1")
    assert result["session_id"] == "sess-1"
    assert calls[1][calls[1].index("--resume") + 1] == "sess-1"


def test_a_dead_session_falls_back_to_a_fresh_call(tmp_path, monkeypatch):
    """A session from another machine must cost one wasted call, not the request."""
    calls = []
    def fake(args, cwd, timeout, env=None):
        calls.append(args)
        if "--resume" in args:
            return subprocess.CompletedProcess(args, 1, "", "No conversation found")
        return subprocess.CompletedProcess(args, 0, _verdict_reply(), "")
    monkeypatch.setattr(steps, "_exec", fake)
    monkeypatch.setattr(steps, "RETRY_WAIT", 0)

    assert _agent(tmp_path, resume="gone")["verdict"] == "pass"
    assert len(calls) == 2
    assert "--resume" not in calls[1]


def test_an_unparseable_verdict_is_not_retried(tmp_path, monkeypatch):
    """The agent did reply, it just did not decide - that parks, it does not loop."""
    calls = _fake_claude(monkeypatch, 0, json.dumps({"result": "I had a think", "total_cost_usd": 0.1}))
    assert _agent(tmp_path)["verdict"] == "human"
    assert len(calls) == 1


def test_a_stage_s_effort_reaches_the_cli(tmp_path, monkeypatch):
    """`effort:` is how a pipeline keeps a cheap stage off the expensive setting."""
    calls = _fake_claude(monkeypatch, 0, _verdict_reply())
    (tmp_path / "role.md").write_text("do it")
    item = {"request": "r", "passes": 1, "notes": [], "history": []}
    steps.run_agent(CLAUDE, tmp_path / "role.md", item, "commit", tmp_path, 5, effort="low")
    assert calls[0][calls[0].index("--effort") + 1] == "low"

    steps.run_agent(CLAUDE, tmp_path / "role.md", item, "commit", tmp_path, 5)
    assert "--effort" not in calls[1], "unset leaves the CLI's own default alone"
