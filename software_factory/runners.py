"""The runner registry: which tool performs a step, and how to talk to it.

A step says `uses: <name>`; this resolves that name to a YAML file describing an argv to
build and a reply to read. The seam exists so a pipeline can put Codex on the coding stage
and Claude on the review one - and the shipped `claude` runner is an ordinary entry here,
not a special case, which is the only way to know the seam is real.

What a runner file can describe is argv and response shape, and nothing deeper. A CLI that
streams, needs a TTY, or has its own idea of sessions will need real work rather than a
YAML file - see docs/pipelines.md.
"""

import json
from pathlib import Path

import yaml

DEFAULT = "claude"
# Beside the repo's pipelines, under the same dotted directory. See pipeline.REPO_DIR.
REPO_DIR = ".sf"
# Three mechanisms, not three tools: `agent` is a command line the factory builds, `command`
# is `sh -c`, `judge` speaks a JSON API. A runner file picks one; `uses:` picks a runner.
KINDS = ("agent", "command", "judge")
# The option a step can ask for, and the placeholder that carries its value. `resume` is
# named by the step's own `resume:` key rather than in `with:`, so the value it substitutes
# is a session id someone else resolved.
OPTIONS = {"model": "model", "effort": "effort", "resume": "session"}
# Of those, the ones a step declares in `with:`.
WITH_OPTIONS = ("model", "effort")


def search_path(repo, global_dir=None):
    """Where to look for a runner: the repo's own, the installation's, then the packaged.

    The same repo-first resolution as a pipeline, for the same reason: a repo carries the
    definition of the tools its own process needs.
    """
    dirs = [Path(repo) / REPO_DIR / "runners"] if repo else []
    if global_dir:
        dirs.append(Path(global_dir))
    return dirs + [Path(__file__).parent / "runners"]


def load(name, dirs=()):
    """One runner definition by name. Whoever is listed first wins."""
    for d in dirs or search_path(None):
        path = Path(d) / ("%s.yaml" % name)
        if path.exists():
            try:
                runner = yaml.safe_load(path.read_text()) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
                # One error type out of here, so every caller has one thing to catch.
                raise ValueError("%s: %s" % (path.name, e))
            runner["name"] = runner.get("name") or path.stem
            _check(runner, path)
            return runner
    raise FileNotFoundError(
        "no agent '%s' - known: %s" % (name, ", ".join(known(dirs)) or "none")
    )


def known(dirs=()):
    """Every runner name these directories offer, sorted, each one listed once."""
    names = set()
    for d in dirs or search_path(None):
        if Path(d).is_dir():
            names.update(f.stem for f in Path(d).glob("*.yaml"))
    return sorted(names)


def _check(runner, path):
    kind = runner.get("kind", "agent")
    if kind not in KINDS:
        raise ValueError("%s: kind must be one of %s, not %r" % (path.name, ", ".join(KINDS), kind))
    if kind == "agent" and not isinstance(runner.get("command"), list):
        raise ValueError("%s: an agent runner needs command: as a list, never a string" % path.name)


def argv(runner, prompt, values):
    """(the command line for one call, the options this runner cannot honour).

    An option with no template is dropped rather than failing: a CLI without `--resume` is
    still a usable runner, it just starts cold. The caller records what was dropped.
    """
    fields = dict(values, prompt=prompt)
    args = [_fill(a, fields) for a in runner["command"]]
    dropped = []
    for option, field in OPTIONS.items():
        if not fields.get(field):
            continue
        template = (runner.get("options") or {}).get(option)
        if not template:
            dropped.append(option)
            continue
        args += [_fill(a, fields) for a in template]
    return args, dropped


def _fill(text, fields):
    """`{prompt}` and friends, substituted into one argv element. Never shell-quoted."""
    out = str(text)
    for name, value in fields.items():
        out = out.replace("{%s}" % name, str(value or ""))
    return out


def read(runner, stdout):
    """The reply as {text, session, cost, error}. ValueError if it cannot be read at all.

    The named keys are flat and top-level: a response that buries its reply under a path
    is beyond what a YAML line can describe, and is a runner someone has to write code for.
    """
    spec = runner.get("output") or {}
    if spec.get("format", "json") == "text":
        # Nothing to parse, and so nothing to report: a plain-text CLI has no session to
        # resume and no cost to record.
        return {"text": stdout, "session": "", "cost": None, "error": ""}
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return {
        "text": data.get(spec.get("text", "result"), ""),
        "session": data.get(spec["session"], "") if spec.get("session") else "",
        # Optional, and an absent cost is not a zero one - it stays None so replay shows
        # "-" rather than a free-looking $0.00.
        "cost": data.get(spec["cost"]) if spec.get("cost") else None,
        "error": data.get(spec["error"]) if spec.get("error") else "",
    }


def supports(runner):
    """Which of model/effort/resume/cost this runner can honour - what `doctor` lists."""
    have = [o for o in OPTIONS if o in (runner.get("options") or {})]
    if (runner.get("output") or {}).get("cost"):
        have.append("cost")
    return have


def binary(runner):
    """The executable a step using this runner will invoke, or "" for the Python kinds."""
    return (runner.get("command") or [""])[0] if runner.get("kind", "agent") == "agent" else ""
