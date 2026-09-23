"""Walk work items through their pipeline. Parallelism is across items."""

import time
from concurrent.futures import ThreadPoolExecutor

from . import runners
from . import judge
from . import pipeline as pl
from . import steps
from .backend import now

# The default for a caller that passes nothing; `sf config` shows what is in effect.
STEP_TIMEOUT = 1800


def queued(backend, ids=None, repo=None):
    """The items a run would pick up. `ids` or `repo` narrow it.

    A global queue needs a way to work one slice of it - and a way to say which slice
    it just started, without the caller re-deriving the filter.
    """
    items = [i for i in backend.all() if i["status"] == "queued"]
    if ids:
        items = [i for i in items if i["id"] in set(ids)]
    if repo:
        items = [i for i in items if i.get("repo", "") == str(repo)]
    return items


def run(backend, pipelines_dir, concurrency=None, ids=None, repo=None, default_concurrency=2,
        step_timeout=None, agent_attempts=None, retry_wait=None, max_input=None,
        runners_dir=None, default_runner=None):
    """Drain the queue, blocking until it is worked. Every item carries its own repo.

    `concurrency` is an explicit `--concurrency` and wins; failing that the pipelines being
    worked say how much parallelism they want, and `default_concurrency` is the configured
    fallback for the ones that do not. Whatever that resolves to, items already `running`
    elsewhere shrink it first - unparking one more request must not push the installation
    past the quota it was configured for, so an item this call cannot fit stays `queued`.
    """
    items = queued(backend, ids, repo)
    if not items:
        return []
    pipes, work, failed = [], [], []
    for item in items:
        # Resolved per item, not per run: two requests may name the same pipeline and
        # mean two different files, one from each repo. Runners resolve the same way.
        try:
            pipes.append(pl.load(
                pl.search_path(item.get("repo", ""), pipelines_dir), item["pipeline"],
                runners.search_path(item.get("repo", ""), runners_dir), default_runner,
            ))
        except Exception as e:
            # Its own item's problem, not the queue's: loading them all up front meant
            # one deleted or broken pipeline file aborted the run for everyone else.
            failed.append(_stop(backend, item, "failed", "pipeline '%s': %s" % (item["pipeline"], e)))
            continue
        work.append(item)
    if not work:
        return failed
    workers = concurrency or max(
        (p.concurrency for p in pipes if p.concurrency), default=default_concurrency
    )
    # Another engine - a daemon, or a second `sf run` unparking one more item - may already
    # be spending part of this same quota. A snapshot, not a live semaphore: good enough to
    # stop one call from doubling the concurrency the installation was configured for, not
    # a guarantee under a race. Zero available slots means every item here stays queued for
    # whichever engine polls next, rather than borrowing a slot beyond the quota.
    running = sum(1 for i in backend.all() if i["status"] == "running")
    workers = max(0, workers - running)
    if workers == 0:
        return failed
    # Resolved here rather than deeper down: a step with no timeout at all would wait on a
    # hung `claude` forever, which is not a default anyone asked for.
    step_timeout = STEP_TIMEOUT if step_timeout is None else step_timeout
    with ThreadPoolExecutor(max_workers=workers) as pool:
        walked = pool.map(
            lambda pair: _walk(backend, pair[0], pair[1], step_timeout, agent_attempts,
                               retry_wait, max_input),
            zip(pipes, work),
        )
    return failed + [i for i in walked if i is not None]


def _walk(backend, pipe, item, step_timeout, agent_attempts, retry_wait, max_input):
    # Checked before the claim, not after: an item can sit in the pool's backlog for
    # as long as everything ahead of it takes, and `claim` sets `running` unconditionally
    # - claiming first would overwrite a cancel that landed during the wait.
    if backend.load(item["id"])["status"] != "queued":
        return None  # cancelled while it waited for a worker
    if not backend.claim(item):
        return None  # another engine got there first
    workspace = backend.workspace(item["id"], item.get("repo", ""))
    steps.ensure_scratch(workspace)
    # Recorded so `sf cancel` can find the pid file without calling `workspace()`,
    # which would create a worktree and a branch as a side effect of signalling.
    item["workspace"] = str(workspace)
    # Re-checked for the same reason as the claim: `workspace()` is a real `git worktree
    # add`, seconds not microseconds, and this save would write back the in-memory
    # `running` over a cancel that landed while it ran.
    if backend.load(item["id"])["status"] == "cancelled":
        return _stop(backend, item, "cancelled", "")
    backend.save(item)
    while True:
        # ponytail: cooperative, one small read per step. A step that finishes in the
        # window between this check and the end-of-iteration save overwrites the flag
        # and the item walks on; a second `sf cancel` catches it. Closing that
        # needs compare-and-set on save, which is a change to the Backend contract.
        if backend.load(item["id"])["status"] == "cancelled":
            return _stop(backend, item, "cancelled", "")
        stage = item["stage"]
        if stage not in pipe.steps:
            # `--stage` into a name this pipeline never declared, or one it declared when
            # the request started and no longer does. Park rather than raise: the KeyError
            # this replaces escaped the worker and left the item claimed and `running`,
            # which no command could recover.
            return _stop(backend, item, "needs_human",
                         "pipeline '%s' has no stage '%s'" % (pipe.name, stage))
        step = pipe.steps[stage]
        step_no = len(item["history"]) + 1
        started, clock = now(), time.monotonic()

        kind, with_, env = pipe.kind(stage), pipe.options(stage), pipe.environ(stage)
        effort = _effort(item, step, kind)
        # Recorded before the blocking call, not after: this is what lets `sf show` and
        # `sf replay` say what is in flight and since when, rather than nothing at all
        # until the step lands in history. Popped the moment the call returns, a few
        # lines down, so a finished step is never shadowed by its own stale marker.
        item["running_step"] = {
            "step": step_no, "stage": stage, "kind": kind,
            "label": step.get("name", ""), "uses": pipe.runners[stage]["name"],
            "started": started,
        }
        backend.save(item)
        if kind == "judge":
            # ponytail: dispatched on the kind, not on the runner's name - there is one
            # judge implementation. The day there is a second, the kind gets a name again.
            result = judge.run_judge(
                pipe.dir / with_["questions"], item, workspace, step_timeout,
                steps.read_inputs(workspace, step.get("input"), max_input), max_input, env,
            )
        elif kind == "command":
            # A command step needs no injection - its inputs are simply there on disk.
            result = steps.run_command(with_["run"], workspace, step_timeout, env)
        else:
            result = steps.run_agent(
                pipe.runners[stage], pipe.dir / with_["prompt"], item, stage, workspace,
                step_timeout, steps.read_inputs(workspace, step.get("input"), max_input),
                _resume(item, step), effort, with_.get("model"), env,
                agent_attempts, retry_wait,
            )

        item.pop("running_step", None)
        # The step's output becomes a file later steps can declare as their input, and
        # a per-pass artifact, so every revision of a plan or a review stays inspectable.
        if step.get("output"):
            steps.write_scratch(workspace, step["output"], result["output"])
            result["artifacts"][step["output"]] = result["output"]

        # Every step leaves its input, its output and its raw log on the backend, so a
        # finished run can be replayed and any single step opened up after the fact.
        artifacts = {
            name: backend.write_artifact(item["id"], step_no, stage, name, text)
            for name, text in result["artifacts"].items()
        }
        item["history"].append(
            {
                "step": step_no,
                "stage": stage,
                # Per step, not per request: a pipeline file can be edited while its
                # requests are in flight, so a resumed request records the new version
                # on the steps that ran under it.
                "version": pipe.version,
                "kind": kind,
                # Empty unless the step declared a `name:`; replay shows it instead of
                # the key, which stays what everything routes on.
                "label": step.get("name", ""),
                # How the call was made, not just what it returned: a step that looks
                # expensive, or an answer that looks over-informed, is explained here.
                "uses": pipe.runners[stage]["name"],
                # What the call actually carried, not what the stage asked for: a runner
                # that cannot express an effort level dropped it, and said so in
                # capabilities.txt. Recording it here anyway would be a quiet lie.
                "effort": effort if "effort" in runners.supports(pipe.runners[stage]) else None,
                "resumed": result.get("resumed", False),
                "verdict": result["verdict"],
                "notes": result["notes"],
                "started": started,
                "ended": now(),
                "duration_s": round(time.monotonic() - clock, 1),
                "cost_usd": result["cost_usd"],
                # Metadata, not a log line: this is what the next run of this stage
                # resumes from, so it has to survive in the item itself.
                "session_id": result["session_id"],
                "artifacts": artifacts,
            }
        )
        verdict = result["verdict"]

        if verdict == "error":
            return _stop(backend, item, "failed", result["notes"])
        if verdict != "pass":
            item["notes"].append({"stage": stage, "text": result["notes"]})
        if verdict == "human":
            # Halt at this very stage. Nothing is routed, nothing discarded.
            return _stop(backend, item, "needs_human", "agent flagged")

        route = pipe.route(stage, verdict)
        if route is None:
            return _stop(backend, item, "needs_human", "no route for verdict '%s'" % verdict)
        target, rework = route
        item["history"][-1]["routed_to"] = target
        item["history"][-1]["rework"] = rework
        if rework:
            item["passes"] += 1
            # passes is the 1-based pass number; max_passes counts the rework
            # round-trips allowed, which is one fewer than the pass we are on.
            if item["passes"] - 1 > pipe.max_passes:
                item["stage"] = target
                return _stop(backend, item, "needs_human", "max_passes")
        item["stage"] = target
        if target == pl.DONE:
            # ponytail: a gated step routing to done finishes instead of parking - there
            # is no next stage to wait at. Review a finished run with `sf replay`.
            return _stop(backend, item, "done", "")
        if step.get("review"):
            # Routed first, then parked: `sf run <id>` picks up at the next stage, so
            # approving costs nothing and rejecting is `run <id> --stage <earlier>`.
            item["history"][-1]["gated"] = True
            return _stop(backend, item, "needs_human", "review after %s" % stage)
        backend.save(item)


def _effort(item, step, kind):
    """The stage's declared effort, or the one triage picked for the whole request.

    Precedence is stage, then item, then nothing at all - a pipeline author who wrote
    `with: {effort: low}` on the commit stage meant it, whatever the request looks like.
    """
    return (step.get("with") or {}).get("effort") or (item.get("effort") if kind == "agent" else None)


def _resume(item, step):
    """The session id this agent call picks up, or None to start cold.

    The default is the last session any stage left behind: the whole line is one
    conversation, so the plan the coder resumes is already in context and is not paid
    for again. `resume: <stage>` picks that stage's own last session instead (a
    reviewer that should re-enter its own thread, not the implementer's), and
    `resume: false` starts the stage cold.
    """
    want = step.get("resume", True)
    if want is False:
        return None
    return next(
        (h["session_id"] for h in reversed(item["history"])
         if h.get("session_id") and (want is True or h["stage"] == want)),
        None,
    )


def _stop(backend, item, status, reason):
    # A cancel lands on disk while a step is still in flight, and a killed step reports
    # verdict `error`. Every exit path comes through here, so this one check keeps that
    # from being recorded as `failed` and burying the cancellation.
    on_disk = backend.load(item["id"])
    if on_disk["status"] == "cancelled":
        status, reason = "cancelled", on_disk.get("reason", "")
    item["status"] = status
    item["reason"] = reason
    item["ended"] = now()
    return backend.save(item)
