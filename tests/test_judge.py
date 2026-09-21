"""The third kind of step. `judge._post` is the only seam that reaches the network, and
every test here stubs it - a suite that can cost money is a suite nobody runs.
"""

import json
from pathlib import Path

import pytest
import yaml

from software_factory import config
from software_factory import pipeline as pl
from software_factory import judge
from software_factory import steps


KEY = config.defaults()["typesafe"]["api_key_env"]
THRESHOLDS = config.defaults()["typesafe"]["thresholds"]


def judge_state_max():
    return config.defaults()["typesafe"]["state_max"]


def _spec(tmp_path, **spec):
    """A judge step's questions file, written where the step would find it."""
    path = tmp_path / "gate.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def _item(notes=()):
    return {"request": "add a flag", "notes": [{"stage": s, "text": t} for s, t in notes]}


def _reply(monkeypatch, answers, input_tokens=10_000):
    """Stub the one HTTP call. Returns the list of (body, key) it was asked to send."""
    sent = []

    def fake(body, key, attempts=2):
        sent.append((body, key))
        return {"answers": answers, "usage": {"input_tokens": input_tokens}}

    monkeypatch.setattr(judge, "_post", fake)
    return sent


def _fails(monkeypatch, message):
    def fake(body, key, attempts=2):
        raise RuntimeError(message)

    monkeypatch.setattr(judge, "_post", fake)


# --- routing the answers -------------------------------------------------------
# No network and no files: a rule is data, and this is the whole decision.


def test_a_rule_fires_on_above_below_equals_or_low_confidence():
    answers = {
        "secret": {"noul": 0.71, "confidence": 0.9},
        "implements": {"noul": 0.2, "confidence": 0.9},
        "pipeline": {"choice": "dev", "confidence": 0.9},
        "scope": {"score": 0.4, "confidence": 0.3},
    }
    # The notes carry the number that fired, because that is what a human reviews.
    assert judge._route(answers, [{"when": "secret", "above": 0.5, "verdict": "human"}], {}) == (
        "human", "secret 0.71 > 0.5")
    assert judge._route(answers, [{"when": "implements", "below": 0.5, "verdict": "fail"}], {}) == (
        "fail", "implements 0.20 < 0.5")
    assert judge._route(answers, [{"when": "pipeline", "equals": "dev", "verdict": "pass"}], {}) == (
        "pass", "pipeline is dev")
    assert judge._route(answers, [{"when": "scope", "confidence_below": 0.5, "verdict": "human"}], {}) == (
        "human", "scope confidence 0.30 < 0.5")


def test_the_first_matching_rule_wins_and_a_rule_with_no_when_is_the_default():
    rules = [
        {"when": "secret", "above": 0.5, "verdict": "human", "notes": "possible credential"},
        {"when": "scope", "above": 1.5, "verdict": "fail", "notes": "beyond the request"},
        {"verdict": "pass"},
    ]
    both = {"secret": {"noul": 0.9}, "scope": {"score": 2.0}}
    assert judge._route(both, rules, {}) == ("human", "secret 0.90 > 0.5 - possible credential")

    neither = {"secret": {"noul": 0.1}, "scope": {"score": 0.0}}
    assert judge._route(neither, rules, {}) == ("pass", "nothing flagged")


def test_a_rule_the_answers_cannot_settle_parks():
    """A rule about a question nobody answered, and a set of answers no rule covers."""
    rules = [{"when": "secret", "above": 0.5, "verdict": "fail"}]
    assert judge._route({}, rules, {}) == ("human", "judge returned no answer for 'secret'")
    assert judge._route({"secret": {"noul": 0.1}}, rules, {}) == (
        "human", "no rule matched and no default rule")


def test_a_verdict_the_engine_cannot_route_parks():
    """A typo'd verdict must park, not invent an edge the pipeline never declared."""
    fired = [{"when": "secret", "above": 0.5, "verdict": "reject"}]
    assert judge._route({"secret": {"noul": 0.9}}, fired, {}) == ("human", "secret 0.90 > 0.5")


# --- one whole judgment --------------------------------------------------------


def test_run_judge_records_what_was_asked_and_which_rule_fired(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, "k")
    spec = _spec(
        tmp_path,
        state={"diff": "echo changed-a-file"},
        questions={"secret": {"type": "noul"}},
        route=[{"when": "secret", "above": 0.5, "verdict": "human", "notes": "possible credential"}],
    )
    sent = _reply(monkeypatch, {"secret": {"noul": 0.9, "confidence": 0.8}})

    result = judge.run_judge(spec, _item(), tmp_path, 5, [("plan.md", "touch two files")])

    assert result["verdict"] == "human"
    assert result["notes"] == "secret 0.90 > 0.5 - possible credential"
    # 10,000 input tokens at $0.042 per million. Output is free, so that is the whole price.
    assert result["cost_usd"] == 0.00042

    state = json.loads(result["artifacts"]["state.json"])
    assert state == {"request": "add a flag", "plan": "touch two files", "diff": "changed-a-file\n"}
    assert json.loads(result["artifacts"]["questions.json"]) == {"secret": {"type": "noul"}}
    assert json.loads(result["artifacts"]["answers.json"])["usage"]["input_tokens"] == 10_000
    assert result["artifacts"]["route.txt"] == "human: secret 0.90 > 0.5 - possible credential"

    assert sent[0][0] == {"state": state, "model": config.defaults()["typesafe"]["model"], "questions": {"secret": {"type": "noul"}}}
    assert sent[0][1] == "k"

    # The `output:` file: the verdict, then every number it was made from.
    report = result["output"].splitlines()
    assert report[0] == "human: secret 0.90 > 0.5 - possible credential"
    assert report[2].split() == ["secret", "0.90", "(confidence", "0.80)"]


def test_state_is_the_request_the_notes_the_inputs_and_the_commands(tmp_path):
    state = judge._state(
        {"diff": "echo changed-a-file"},
        _item(notes=[("code", "the users table")]),
        tmp_path,
        5,
        [("plan.md", "touch two files")],
        steps.MAX_INPUT,
        judge_state_max(),
    )
    assert state["request"] == "add a flag"
    assert state["notes"] == ["[code] the users table"]
    assert state["plan"] == "touch two files", "a wired input arrives under its stem"
    assert state["diff"] == "changed-a-file\n"


def test_a_field_over_the_cap_is_truncated_visibly():
    """Silently halving a diff would make the judge answer about a diff nobody sent."""
    state = judge._fit({"diff": "x" * (steps.MAX_INPUT + 5_000)}, steps.MAX_INPUT, judge_state_max())
    assert state["diff"].startswith("x" * steps.MAX_INPUT)
    assert state["diff"].endswith("[... truncated at %d characters]" % steps.MAX_INPUT)


# --- every way it can fail to decide -------------------------------------------
# All of them park. A service the factory could not reach is a decision nobody made.



def test_an_unreadable_questions_file_parks(tmp_path):
    result = judge.run_judge(tmp_path / "gone.yaml", _item(), tmp_path, 5)
    assert result["verdict"] == "human"
    assert "cannot read gone.yaml" in result["notes"]

    half_written = tmp_path / "broken.yaml"
    half_written.write_text("questions: [oh: no\n")
    assert judge.run_judge(half_written, _item(), tmp_path, 5)["verdict"] == "human"


def test_a_questions_file_with_no_route_parks(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, "k")
    spec = _spec(tmp_path, questions={"secret": {"type": "noul"}})
    result = judge.run_judge(spec, _item(), tmp_path, 5)
    assert result["verdict"] == "human"
    assert "gate.yaml declares no questions or no route" in result["notes"]
    # Recorded anyway: seeing what was about to be asked is how the file gets fixed.
    assert set(result["artifacts"]) == {"state.json", "questions.json"}


def test_no_api_key_parks(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY, raising=False)
    spec = _spec(tmp_path, questions={"secret": {"type": "noul"}}, route=[{"verdict": "pass"}])
    result = judge.run_judge(spec, _item(), tmp_path, 5)
    assert result["verdict"] == "human"
    assert KEY in result["notes"]
    assert result["cost_usd"] is None, "nothing was sent, so nothing was spent"


def test_a_failed_request_parks(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, "k")
    _fails(monkeypatch, "HTTP 401 invalid api key")
    spec = _spec(tmp_path, questions={"secret": {"type": "noul"}}, route=[{"verdict": "pass"}])
    result = judge.run_judge(spec, _item(), tmp_path, 5)
    assert result["verdict"] == "human"
    assert result["notes"] == "typesafe: HTTP 401 invalid api key"
    assert set(result["artifacts"]) == {"state.json", "questions.json"}


@pytest.mark.xfail(reason="a body of `null` raises AttributeError on the cost line instead of parking")
def test_a_reply_that_is_not_an_object_parks(tmp_path, monkeypatch):
    """docs/pipelines.md promises a malformed answer parks; `null` is a valid JSON body."""
    monkeypatch.setenv(KEY, "k")
    monkeypatch.setattr(judge, "_post", lambda body, key: None)
    spec = _spec(tmp_path, questions={"secret": {"type": "noul"}}, route=[{"verdict": "pass"}])
    assert judge.run_judge(spec, _item(), tmp_path, 5)["verdict"] == "human"


# --- submit-time triage --------------------------------------------------------


def test_triage_reads_a_pipeline_an_effort_and_a_specificity(monkeypatch):
    monkeypatch.setenv(KEY, "k")
    _reply(monkeypatch, {
        "pipeline": {"choice": "quick", "confidence": 0.9},
        "effort": {"score": 1.4, "confidence": 0.8},
        "specific": {"noul": 0.8},
    }, input_tokens=1_000)

    assert judge.triage("add a flag", {"dev": "the full line", "quick": "small changes"}) == {
        "pipeline": "quick", "effort": "medium", "specific": 0.8, "cost_usd": 0.000042
    }


def test_triage_drops_a_pipeline_choice_it_is_not_confident_about(monkeypatch):
    """Below the floor the model is torn, and the configured default beats half a coin flip."""
    monkeypatch.setenv(KEY, "k")
    _reply(monkeypatch, {
        "pipeline": {"choice": "dev", "confidence": config.defaults()["typesafe"]["min_confidence"] - 0.1},
        "effort": {"score": 2.0},
    })

    read = judge.triage("add a flag", {"dev": "the full line", "quick": "small changes"})
    assert "pipeline" not in read
    assert read["effort"] == "high", "the rest of the answers still count"


def test_triage_never_blocks_a_submit(monkeypatch):
    """The one part that does not fail closed: a request must queue with no key and no network."""
    monkeypatch.delenv(KEY, raising=False)
    assert judge.triage("add a flag", {"dev": "the full line", "quick": "small changes"}) == {}

    monkeypatch.setenv(KEY, "k")
    _fails(monkeypatch, "no route to host")
    assert judge.triage("add a flag", {"dev": "the full line", "quick": "small changes"}) == {}



def test_a_route_rule_can_name_a_threshold_and_a_literal_still_wins():
    """`above: $secret` is how one config edit retunes every gate that uses it."""
    answers = {"secret": {"noul": 0.7}, "scope": {"score": 1.0}}
    named = [{"when": "secret", "above": "$secret", "verdict": "human", "notes": "credential"}]
    assert judge._route(answers, named, THRESHOLDS) == ("human", "secret 0.70 > 0.5 - credential")

    tuned = [{"when": "secret", "above": "$secret", "verdict": "human"}, {"verdict": "pass"}]
    assert judge._route(answers, tuned, dict(THRESHOLDS, secret=0.9))[0] == "pass"

    pinned = [{"when": "secret", "above": 0.9, "verdict": "human"}, {"verdict": "pass"}]
    assert judge._route(answers, pinned, THRESHOLDS)[0] == "pass", "a literal ignores the config"



def test_an_unknown_threshold_parks_rather_than_reading_as_zero():
    """Read as zero, `above: $typo` would fire on every answer there is."""
    rules = [{"when": "secret", "above": "$sekret", "verdict": "human"}, {"verdict": "pass"}]
    verdict, notes = judge._route({"secret": {"noul": 0.01}}, rules, THRESHOLDS)
    assert verdict == "human"
    assert "$sekret" in notes and "typesafe.thresholds" in notes



def test_a_bad_threshold_anywhere_parks_even_if_an_earlier_rule_matched():
    """A typo in the last rule is a wiring bug whether or not today's diff reaches it."""
    rules = [
        {"when": "secret", "above": "$secret", "verdict": "human"},
        {"when": "scope", "above": "$scop", "verdict": "fail"},
    ]
    verdict, _ = judge._route({"secret": {"noul": 0.99}}, rules, THRESHOLDS)
    assert verdict == "human"



def test_every_judge_file_in_this_repo_names_a_threshold_the_config_defines():
    """The config defaults and the files that reference them have to stay in step: a rule
    saying `above: $secret` resolves against `typesafe.thresholds`, and a name with nothing
    behind it is a gate that cannot decide - it parks every request that reaches it.

    Both directories, because both ship to a user: `.sf/pipelines/` is what this repo runs
    on itself, `examples/` is what people copy. Neither is required to name a threshold at
    all - examples/ pins literals on purpose, to be read rather than tuned - so this checks
    the names that are there, and is content with none.
    """
    root = Path(__file__).parent.parent
    files = sorted(root.glob(".sf/pipelines/**/*.yaml")) + sorted(root.glob("examples/prompts/*.yaml"))
    for f in files:
        spec = yaml.safe_load(f.read_text()) or {}
        named = set()
        for rule in spec.get("route") or []:
            named |= {v[1:] for v in rule.values() if isinstance(v, str) and v.startswith("$")}
        assert named <= set(THRESHOLDS), "%s: %s" % (f.name, sorted(named - set(THRESHOLDS)))



def test_triage_can_reach_every_effort_the_pipelines_accept():
    """The score is read as a position in EFFORTS, so one level per entry or the top of
    the range is unreachable."""
    assert len(judge.TRIAGE["effort"]["criteria"]) == len(pl.EFFORTS)
    assert judge.EFFORTS is pl.EFFORTS, "one definition, not two that drift"



def test_triage_offers_the_pipelines_that_are_actually_installed(monkeypatch):
    """Renaming a pipeline used to disable the question; its description is the criteria."""
    sent = {}
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(judge, "_post", lambda body, key, ts: sent.update(body) or {})

    judge.triage("r", {"fast": "for small things", "careful": "for everything else"})
    assert sent["questions"]["pipeline"]["criteria"] == {
        "fast": "for small things", "careful": "for everything else"
    }

    judge.triage("r", {"fast": "for small things", "undescribed": ""})
    assert "pipeline" not in sent["questions"], "nothing to choose between"



def test_triage_reads_the_configured_model_and_price(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_HOME", str(tmp_path))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    (tmp_path / "config.yaml").write_text("typesafe:\n  model: jev-2\n  price_per_token: 1.0\n")
    sent = {}
    monkeypatch.setattr(
        judge, "_post",
        lambda body, key, ts: sent.update(body) or {"usage": {"input_tokens": 3}, "answers": {}},
    )
    assert judge.triage("r")["cost_usd"] == 3.0
    assert sent["model"] == "jev-2"



def test_a_partial_typesafe_block_keeps_the_other_defaults(tmp_path, monkeypatch):
    """One knob at a time: naming `model:` must not drop the endpoint or the thresholds."""
    monkeypatch.setenv("SF_HOME", str(tmp_path))
    assert config.load()["typesafe"] == config.defaults()["typesafe"]
    (tmp_path / "config.yaml").write_text("typesafe:\n  model: jev-2\n  thresholds:\n    scope: 2.5\n")
    ts = config.load()["typesafe"]
    assert ts["model"] == "jev-2"
    assert ts["base_url"] == config.defaults()["typesafe"]["base_url"]
    assert ts["thresholds"]["scope"] == 2.5
    assert ts["thresholds"]["secret"] == THRESHOLDS["secret"]
