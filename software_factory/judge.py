"""The third kind of step: a typed judgment from TypeSafe's System One model (Jev).

An agent does the work, a command executes, `uses: typesafe` decides. A judge step sends state
and typed questions in one request and turns the answers into one of the factory's three
verdicts using rules the pipeline author declares - so the decision is data rather than prose,
and costs about $0.0002 instead of an agent call.

Opt-in everywhere: no shipped pipeline uses it, and without TYPESAFE_API_KEY nothing here is
reached. A pipeline that *does* name this runner and cannot ask parks for a human - the
factory never guesses a verdict, and a service it could not reach is a decision nobody made.

The questions are fixed by the pipeline author and only the diff and the scratch files arrive
as state. That separation is the point: an agent writes the diff, so the diff is not fully
trusted input, and it must never be able to reach the model as an instruction.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import config
from .pipeline import EFFORTS
from .steps import MAX_INPUT, _exec, _result

VERDICTS = ("pass", "fail", "human")


def _settings():
    """The `typesafe:` config block - endpoint, model, price, budgets and named thresholds.

    Read here rather than threaded down from the CLI: this step is a network round trip
    either way, so one small YAML read costs nothing and the engine keeps its signature.
    """
    return config.load()["typesafe"]


def run_judge(spec_file, item, workspace, timeout, inputs=(), max_input=None, env=None):
    """Run one judge step. Returns the same result shape as an agent or a command step.

    Which service answers is no longer asked here: the step's `uses:` already selected
    this runner, and the registry is where a second provider would be named.
    """
    ts = _settings()
    try:
        spec = yaml.safe_load(Path(spec_file).read_text()) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return _park("cannot read %s: %s" % (Path(spec_file).name, e), {})

    questions, rules = spec.get("questions") or {}, spec.get("route") or []
    state = _state(spec.get("state") or {}, item, workspace, timeout, inputs,
                   max_input or MAX_INPUT, ts["state_max"], env)
    artifacts = {
        "state.json": json.dumps(state, indent=2),
        "questions.json": json.dumps(questions, indent=2),
    }
    if not questions or not rules:
        return _park("%s declares no questions or no route" % Path(spec_file).name, artifacts)

    key = os.environ.get(ts["api_key_env"])
    if not key:
        return _park("no %s in the environment - this step cannot ask" % ts["api_key_env"], artifacts)
    try:
        data = _post({"state": state, "model": spec.get("model", ts["model"]), "questions": questions}, key, ts)
    except RuntimeError as e:
        return _park("typesafe: %s" % e, artifacts)

    artifacts["answers.json"] = json.dumps(data, indent=2)
    answers = data.get("answers") or {}
    verdict, notes = _route(answers, rules, ts["thresholds"])
    artifacts["route.txt"] = "%s: %s" % (verdict, notes)
    # Output tokens are free, so the whole price of a judgment is what we sent it.
    cost = (data.get("usage") or {}).get("input_tokens", 0) * ts["price_per_token"]
    return _result(verdict, notes, round(cost, 6), artifacts, _report(answers, verdict, notes))


def _state(commands, item, workspace, timeout, inputs, max_input, state_max, env=None):
    """Named JSON fields, which is what the questions refer to by `name`.

    The request and the accumulated notes are always there; declared `input:` files arrive
    under their stem (plan.md -> plan); each `state:` command runs in the worktree and
    contributes its output (git diff, git diff --stat, a coverage report).
    """
    state = {"request": item["request"]}
    if item["notes"]:
        state["notes"] = ["[%s] %s" % (n["stage"], n["text"]) for n in item["notes"]]
    for name, text in inputs:
        state[Path(name).stem] = text
    for name, command in commands.items():
        proc = _exec(["sh", "-c", command], workspace, timeout, env)
        # stderr too: `git diff` in a repo with no commits says so on stderr, and a judge
        # reading "" would call that an empty diff.
        state[name] = (proc.stdout or "") + (proc.stderr or "")
    return _fit(state, max_input, state_max)


def _fit(state, max_input, state_max):
    """Cap each field, then the whole thing, longest field first. Truncation is always visible.

    Two budgets, from two places: `max_input` is the per-field cap every step shares
    (config), `state_max` is what this provider will accept in one request (typesafe block).
    """
    for name, value in state.items():
        if isinstance(value, str) and len(value) > max_input:
            state[name] = value[:max_input] + "\n\n[... truncated at %d characters]" % max_input
    while len(json.dumps(state)) > state_max:
        longest = max(state, key=lambda n: len(json.dumps(state[n])))
        text = json.dumps(state[longest])
        if len(text) < 1000:
            break  # nothing big left to cut; send it and let the API complain
        state[longest] = str(state[longest])[: len(text) // 2] + "\n\n[... truncated to fit]"
    return state


def _post(body, key, ts):
    """One POST, retried on a transient failure. A judgment is cheap and idempotent."""
    request = urllib.request.Request(
        ts["base_url"],
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"},
    )
    attempts = ts["attempts"]
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=ts["timeout"]) as response:
                data = json.loads(response.read())
                # `null` and `[]` are valid JSON and are not answers. Both callers read
                # fields straight off what comes back, and an AttributeError here would
                # sail past the fail-closed path that is supposed to park the step.
                return data if isinstance(data, dict) else {}
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace").strip()
            # 4xx is the key or the questions, not weather: asking again says the same thing.
            if e.code < 500 or attempt == attempts:
                raise RuntimeError("HTTP %d %s" % (e.code, detail))
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt == attempts:
                raise RuntimeError(str(e)[:200])


def _route(answers, rules, thresholds):
    """(verdict, notes) from the first rule that matches. Ordered; a rule with no `when` is the default."""
    try:
        # Every rule up front, not the ones reached: a typo in the last rule is a wiring
        # bug whether or not an earlier one fired today.
        rules = [_pin(rule, thresholds) for rule in rules]
    except KeyError as e:
        return "human", "route names %s, which is not in typesafe.thresholds" % e.args[0]
    for rule in rules:
        name = rule.get("when")
        if name is None:
            return _verdict(rule), _notes(rule, "nothing flagged")
        answer = answers.get(name)
        if answer is None:
            # A rule about a question nobody answered is a decision nobody made.
            return "human", "judge returned no answer for '%s'" % name
        why = _match(rule, answer.get("noul", answer.get("score", answer.get("choice"))),
                     answer.get("confidence"))
        if why:
            return _verdict(rule), _notes(rule, "%s %s" % (name, why))
    return "human", "no rule matched and no default rule"


COMPARISONS = ("above", "below", "confidence_below")


def _pin(rule, thresholds):
    """A comparison may name a threshold (`above: $secret`) instead of pinning a number.

    That is how an installation tunes every gate from one place while a pipeline that
    cares about one number still writes it inline - a literal always wins. An unknown
    name raises: read as zero it would silently fire, or silently never fire.
    """
    pinned = dict(rule)
    for key in COMPARISONS:
        value = pinned.get(key)
        if isinstance(value, str) and value.startswith("$"):
            if value[1:] not in thresholds:
                raise KeyError("'%s'" % value)
            pinned[key] = thresholds[value[1:]]
    return pinned


def _match(rule, value, confidence):
    """The comparison that fired, as text, or None. One rule asks one thing."""
    if "confidence_below" in rule and confidence is not None and confidence < rule["confidence_below"]:
        return "confidence %.2f < %s" % (confidence, rule["confidence_below"])
    if isinstance(value, (int, float)):
        if "above" in rule and value > rule["above"]:
            return "%.2f > %s" % (value, rule["above"])
        if "below" in rule and value < rule["below"]:
            return "%.2f < %s" % (value, rule["below"])
    if "equals" in rule and value == rule["equals"]:
        return "is %s" % value
    return None


def _verdict(rule):
    """Only the three the engine routes. A typo'd verdict parks rather than inventing an edge."""
    return rule["verdict"] if rule.get("verdict") in VERDICTS else "human"


def _notes(rule, why):
    return "%s - %s" % (why, rule["notes"]) if rule.get("notes") else why


def _report(answers, verdict, notes):
    """The step's `output:` file: every number the decision was made from, for the next stage."""
    lines = ["%s: %s" % (verdict, notes), ""]
    for name, answer in answers.items():
        value = answer.get("noul", answer.get("score", answer.get("choice")))
        confidence = answer.get("confidence")
        lines.append("%-24s %-8s%s" % (
            name,
            "%.2f" % value if isinstance(value, (int, float)) else value,
            "  (confidence %.2f)" % confidence if confidence is not None else "",
        ))
    return "\n".join(lines)


def _park(message, artifacts):
    """Every way this step can fail to reach a decision ends here: park, never guess."""
    return _result("human", message, None, artifacts, message)


# --- submit-time triage ------------------------------------------------------------------
# What the user did not say: which pipeline this belongs in, how hard the agents should think,
# and whether the request is specific enough to start at all. Three independent questions cost
# one request, and the last one is the cheap version of finding out at stage five.

TRIAGE = {
    "effort": {
        "type": "score",
        "instructions": "How much thinking does `request` need from the engineer implementing it?",
        # One level per `pipeline.EFFORTS` entry, in the same order: the answer is read as
        # a position in that tuple, so a question with fewer levels puts the top of the
        # range permanently out of reach.
        "criteria": [
            "mechanical - a rename, a constant, a message, an obvious one-line fix",
            "ordinary - one clear change in a known place, no design decisions",
            "substantial - several files, or a design decision with an obvious answer",
            "hard - design decisions with real trade-offs, or an unclear blast radius",
            "open - the approach itself is the question, or the blast radius is the codebase",
        ],
    },
    "specific": {
        "type": "noul",
        "instructions": "`request` says specifically enough what to do that an engineer could "
                        "start without asking a question first.",
    },
}
# Added only when there is more than one described pipeline to pick between: the options are
# the pipelines actually installed, in their own words, so renaming one does not silently
# disable the question and a repo's own line can be chosen like any other.
PIPELINE = {
    "type": "choice",
    "instructions": "Which process does `request` deserve?",
}


def triage(request, pipelines=()):
    """{pipeline, effort, specific, cost_usd} - whatever could be read. {} if it could not ask.

    `pipelines` is {name: description}; an undescribed pipeline is not offered, because the
    description is the only thing the model has to choose by.

    Every failure returns {}: triage is an optimisation, and a request must still queue when
    there is no key, no network, or no confident answer. This is the one part of the
    integration that does not fail closed, deliberately.
    """
    ts = _settings()
    key = os.environ.get(ts["api_key_env"])
    if not key:
        return {}
    questions = dict(TRIAGE)
    choices = {name: text for name, text in dict(pipelines).items() if text}
    if len(choices) > 1:
        questions["pipeline"] = dict(PIPELINE, criteria=choices)
    try:
        data = _post({"state": {"request": request}, "model": ts["model"], "questions": questions}, key, ts)
    except RuntimeError:
        return {}
    answers = data.get("answers") or {}
    read = {"cost_usd": round((data.get("usage") or {}).get("input_tokens", 0) * ts["price_per_token"], 6)}
    choice = answers.get("pipeline") or {}
    if choice.get("choice") in choices and (choice.get("confidence") or 0) >= ts["min_confidence"]:
        # Below the floor the model is genuinely torn; the configured default is a better
        # answer than the more likely half of a coin flip.
        read["pipeline"] = choice["choice"]
    score = answers.get("effort") or {}
    if isinstance(score.get("score"), (int, float)):
        read["effort"] = EFFORTS[max(0, min(int(round(score["score"])), len(EFFORTS) - 1))]
    noul = answers.get("specific") or {}
    if isinstance(noul.get("noul"), (int, float)):
        read["specific"] = noul["noul"]
    return read
