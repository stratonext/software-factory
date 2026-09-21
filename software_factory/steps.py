"""The two kinds of step the factory runs itself: an agent call and a shell command.

Which agent is not decided here - `runners.py` holds the runner definition that says what
to invoke and how to read the reply. What stays here is everything that is the same
whatever the tool: the prompt, the retries, the verdict, and the artifacts.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import runners

# A reply the engine cannot use is usually a blip - an empty response, an overloaded
# API - so the same prompt is sent again before the whole request is failed. These are
# the defaults for a caller that passes nothing; `sf config` shows what is in effect.
AGENT_ATTEMPTS = 3
RETRY_WAIT = 2  # seconds, times the attempt number

# Every step's declared input:/output: lives here, inside the workspace the agents
# already work in. Self-ignoring, so the worktree diff stays the deliverable.
SCRATCH = "_FACTORY"
# The running step's pid, so `sf cancel` can reach a step already in flight.
PID_FILE = "step.pid"
MAX_INPUT = 20000

VERDICT_SUFFIX = """

---
End your reply with a single JSON object on its own line:

{"verdict": "pass" | "fail" | "human", "notes": "one paragraph"}

  pass  - this stage is complete, move on
  fail  - send the work back with your notes attached
  human - you need a human decision (ambiguous requirement, destructive or
          irreversible action, missing credential). The request stops here and
          waits; put your question in notes. Prefer this over guessing.
"""

VERDICTS = ("pass", "fail", "human")


def _find_verdict(text):
    """The last JSON object in the reply that carries a verdict, and its exact text.

    Decoded from each `{` rather than matched with a pattern: an agent's notes routinely
    contain braces - `examples/{docs-only,bugfix}.yaml` - and any flat-object regex stops
    dead at the first one, parking a request whose agent did everything right.
    """
    body, found, decoder = text or "", (None, None), json.JSONDecoder()
    for i, char in enumerate(body):
        if char != "{":
            continue
        try:
            obj, end = decoder.raw_decode(body[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("verdict") in VERDICTS:
            found = (obj, body[i:i + end])
    return found


def parse_verdict(text):
    return _find_verdict(text)[0]


def strip_verdict(text):
    """The reply without its closing verdict object - what a later step should read.

    A plan or a review handed to the next stage should not end in factory machinery.
    """
    body = text or ""
    match = _find_verdict(body)[1]
    if match:
        cut = body.rfind(match)
        body = body[:cut] + body[cut + len(match):]
    return body.rstrip().removesuffix("---").rstrip()


def ensure_scratch(workspace):
    """Create the scratch directory that carries files between steps."""
    d = Path(workspace) / SCRATCH
    d.mkdir(parents=True, exist_ok=True)
    # Ignores itself rather than the factory editing the repo's own .gitignore: a repo
    # needs no preparation to be worked in, and the change under review stays clean.
    (d / ".gitignore").write_text("*\n")
    return d


def write_scratch(workspace, name, text):
    path = Path(workspace) / SCRATCH / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = text or ""
    path.write_text(text if text.endswith("\n") else text + "\n")
    return path


def read_inputs(workspace, names, max_input=None):
    """[(name, text)] for the files this step declared, skipping ones not written yet.

    Missing is normal, not an error: on a first pass the reviewer has not run, so its
    output does not exist. A name no step ever produces is rejected at load instead.
    """
    max_input = max_input or MAX_INPUT
    out = []
    for name in names or ():
        path = Path(workspace) / SCRATCH / name
        if not path.exists():
            continue
        text = path.read_text()
        # ponytail: flat char cap. Summarise or chunk if a real input ever outgrows it.
        if len(text) > max_input:
            text = text[:max_input] + "\n\n[... truncated at %d characters]" % max_input
        out.append((name, text))
    return out


def build_prompt(body, item, stage, inputs=()):
    parts = [
        "# Request\n%s" % item["request"],
        "# Your stage\n%s (pass %d of this request)" % (stage, item["passes"]),
        body,
    ]
    for name, text in inputs:
        parts.append("# Input: %s\n%s" % (name, text))
    if item["notes"]:
        parts.append(
            "# Notes you must address\n"
            + "\n".join("- [%s] %s" % (n["stage"], n["text"]) for n in item["notes"])
        )
    if item["history"]:
        parts.append(
            "# What earlier stages did\n"
            + "\n".join(
                "- %s -> %s: %s" % (h["stage"], h["verdict"], h.get("notes", ""))
                for h in item["history"][-6:]
            )
        )
    return "\n\n".join(parts) + VERDICT_SUFFIX


def run_agent(runner, prompt_file, item, stage, workspace, timeout, inputs=(),
              resume=None, effort=None, model=None, env=None, attempts=None, retry_wait=None):
    """Run one agent stage, retrying a reply the engine cannot use.

    ``runner`` is the definition the step's `uses:` resolved to - the argv to build and
    the reply to read. ``resume`` is the session id this stage left behind last time it
    ran, so an agent re-entering a stage on rework still remembers what it wrote before;
    ``effort`` and ``model`` are the stage's declared levers. A runner that declares no
    template for one of the three drops it and records that it did.

    Returns a result whose ``artifacts`` are the whole story of the step: the exact
    prompt that went in, what came back, and the untouched tool response.
    """
    attempts = attempts or AGENT_ATTEMPTS
    retry_wait = RETRY_WAIT if retry_wait is None else retry_wait
    prompt = build_prompt(prompt_file.read_text(), item, stage, inputs)
    for attempt in range(1, attempts + 1):
        result, retryable = _agent_attempt(runner, prompt, workspace, timeout, resume, effort,
                                           model, env)
        if resume and result["verdict"] == "error":
            # A dead session id - the workspace was re-created, or the state moved to
            # another machine - must not fail the request. Drop it and call cold.
            resume = None
            retryable = True
        if not retryable or attempt == attempts:
            if attempt > 1:
                result["artifacts"]["attempts.txt"] = str(attempt)
                if result["verdict"] == "error":
                    result["notes"] += " (after %d attempts)" % attempt
            return result
        time.sleep(retry_wait * attempt)


def _agent_attempt(runner, prompt, workspace, timeout, resume=None, effort=None, model=None, env=None):
    """One call to the runner's CLI. Returns (result, worth retrying)."""
    tool = runner["name"]
    # The prompt is still rebuilt and re-sent in full when resuming: the earlier session's
    # context is added, it does not replace the notes that say why work came back.
    args, dropped = runners.argv(runner, prompt, {"model": model, "effort": effort, "session": resume})
    proc = _exec(args, workspace, timeout, env)
    artifacts = {
        "input.md": prompt,
        "raw.json": proc.stdout or "",
        "exit_code.txt": str(proc.returncode),
        # The invocation, with the prompt elided - an agent that printed nothing can
        # still be reproduced by hand from the artifacts alone.
        "command.txt": "cd %s && %s" % (
            workspace, " ".join('"$(cat input.md)"' if a == prompt else a for a in args)),
    }
    if dropped:
        # Degraded, not failed - and never silently: a stage that asked for `effort: high`
        # and got a runner that cannot express it should say so where replay will show it.
        artifacts["capabilities.txt"] = "\n".join(
            "%s: %s declares no %s option - ignored" % (o, tool, o) for o in dropped
        )
    if proc.stderr:
        artifacts["stderr.txt"] = proc.stderr
    resumed = bool(resume) and "resume" not in dropped

    if proc.returncode != 0:
        # Not retried: a non-zero exit is configuration - auth, or bypassPermissions
        # under root - not a blip, and three identical failures say nothing extra.
        failed = proc.stderr or proc.stdout or ""
        artifacts["output.md"] = failed
        return _result(
            "error", "%s exited %d: %s" % (tool, proc.returncode, failed[-2000:].strip()), None,
            artifacts, failed, resumed=resumed,
        ), False
    if not (proc.stdout or "").strip():
        # Exit 0 and silence. Without saying so, this reads as "non-JSON output" and
        # sends you looking at a parser instead of at the agent CLI.
        artifacts["output.md"] = ""
        return _result(
            "error",
            "%s exited 0 and printed nothing - see command.txt, stderr.txt (%d bytes)"
            % (tool, len(proc.stderr or "")),
            None, artifacts, "", resumed=resumed,
        ), True
    try:
        reply = runners.read(runner, proc.stdout)
    except ValueError:
        artifacts["output.md"] = proc.stdout
        return _result(
            "error", "%s printed non-JSON: %s" % (tool, proc.stdout[:200].strip()), None,
            artifacts, proc.stdout, resumed=resumed,
        ), True

    text = reply["text"]
    # The id of the conversation that just happened, so the next run of this stage can
    # resume it instead of starting the agent cold.
    session_id = reply["session"]
    if reply["error"]:
        artifacts["output.md"] = text
        return _result(
            "error", "%s reported an error: %s" % (tool, (text or proc.stdout[:200]).strip()),
            reply["cost"], artifacts, text, session_id, resumed,
        ), True
    artifacts["output.md"] = text
    cost = reply["cost"]
    # What a later step reads is the reply itself, minus the verdict the engine consumed.
    output = strip_verdict(text)
    verdict = parse_verdict(text)
    if verdict is None:
        # Never guess a verdict, and never retry one: the agent did reply, it just did
        # not decide. Park it instead of looping or silently passing.
        return _result(
            "human", "agent returned no parseable verdict", cost, artifacts, output,
            session_id, resumed,
        ), False
    return _result(
        verdict["verdict"], verdict.get("notes", ""), cost, artifacts, output, session_id, resumed
    ), False


def run_command(command, workspace, timeout, env=None):
    proc = _exec(["sh", "-c", command], workspace, timeout, env)
    output = (proc.stdout or "") + (proc.stderr or "")
    artifacts = {
        "input.sh": command,
        "output.txt": output,
        "exit_code.txt": str(proc.returncode),
    }
    # A command may also emit a verdict object - that is how a non-agent step
    # flags for a human rather than just passing or failing.
    verdict = parse_verdict(proc.stdout)
    if verdict:
        return _result(verdict["verdict"], verdict.get("notes", ""), None, artifacts, strip_verdict(output))
    return _result(
        "pass" if proc.returncode == 0 else "fail",
        "`%s` exited %d\n%s" % (command, proc.returncode, output[-2000:]),
        None,
        artifacts,
        strip_verdict(output),
    )


def _result(verdict, notes, cost_usd, artifacts, output="", session_id="", resumed=False):
    """``output`` is the text a step's ``output:`` file gets; ``artifacts`` is the record."""
    return {
        "verdict": verdict,
        "notes": notes,
        "cost_usd": cost_usd,
        "artifacts": artifacts,
        "output": output,
        # Empty for a command step and for a call that never got a reply back.
        "session_id": session_id,
        # Whether a session was actually picked up, not whether one was offered: a dead id
        # falls back to a cold call, and a runner without `--resume` never had one.
        "resumed": resumed,
    }


def _exec(args, cwd, timeout, env=None):
    """Run one step to completion, leaving its pid where `sf cancel` can find it."""
    # stdin=DEVNULL: the agent CLI otherwise waits 3s for piped input on every call.
    # start_new_session: the step gets its own process group, so one killpg takes down
    # `claude` (or `sh -c`) *and* its children - on cancel and on timeout alike.
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        # A runner is an arbitrary CLI, so a step may need to say where its key or its
        # PATH is. Layered over the engine's own environment, never replacing it.
        env={**os.environ, **env} if env else None,
    )
    pid_file = ensure_scratch(cwd) / PID_FILE
    pid_file.write_text(str(proc.pid))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # it already had its 30 minutes
        except OSError:
            pass  # cancelled out from under us; nothing left to kill
        proc.communicate()
        return subprocess.CompletedProcess(args, 124, "", "timed out after %ss" % timeout)
    finally:
        # Unlinked per step, so a stale pid can only ever name a process from the same
        # workspace's own last step, not an arbitrary reuse of that number.
        pid_file.unlink(missing_ok=True)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)
