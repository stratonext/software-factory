"""Pipeline definition: YAML in, transitions out."""

from pathlib import Path

import yaml

from . import runners

DONE = "done"
# A repo keeps its own process definition under one dotted directory rather than claiming
# `pipelines/` and `runners/` at the top level, where they collide with whatever the repo
# already calls those things.
REPO_DIR = ".sf"
# A step says who performs it (`uses:`), what to hand them (`with:`), and where the work
# goes next. Everything else is about the line rather than the station.
STEP_KEYS = {"uses", "with", "name", "env", "next", "on", "review", "input", "output", "resume"}
# What `claude --effort` accepts. A stage declares how hard its agent should think;
# cheap stages (commit, a mechanical edit) have no business at the top of the range.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class Pipeline:
    def __init__(self, data, path, runner_dirs=None, default_runner=None):
        self.path = Path(path)
        self.dir = self.path.parent
        self.name = data.get("name", self.path.stem)
        self.version = data.get("version", 1)
        # Prose for whoever is choosing a pipeline - a person reading `factory config`,
        # or an agent picking one for a request. Optional, and nothing routes on it.
        self.description = data.get("description") or ""
        self.max_passes = data.get("max_passes", 3)
        # Unset, not defaulted: the engine cannot tell a pipeline that asked for 2 from
        # one that said nothing, and the configured default has to win over the latter.
        self.concurrency = data.get("concurrency")
        self.runner_dirs = list(runner_dirs) if runner_dirs else runners.search_path(None)
        self.default_runner = default_runner or runners.DEFAULT
        # Pipeline-wide environment, under every step's own.
        self.env = data.get("env") or {}
        # Resolved once at load, so a run does not re-read a YAML file per step.
        self.runners = {}
        self.steps = data["steps"]
        for step in self.steps.values():
            # YAML 1.1 reads a bare `on:` key as the boolean True. Users write `on:`.
            if True in step:
                step["on"] = step.pop(True)
            if isinstance(step.get("input"), str):
                step["input"] = [step["input"]]
        self.start = data.get("start") or next(iter(self.steps))
        # ponytail: rank = declaration order. Swap for a topological sort if authors
        # ever mis-order steps badly enough that rework detection lies.
        self.rank = {name: i for i, name in enumerate(self.steps)}
        self._validate()

    def _validate(self):
        # bool first: YAML 1.1 reads `version: yes` as True, and True is an int.
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise ValueError("%s: version must be an integer, not %r" % (self.name, self.version))
        if self.start not in self.steps:
            raise ValueError("%s: start '%s' is not a step" % (self.name, self.start))
        produced = {s["output"] for s in self.steps.values() if "output" in s}
        for name, step in self.steps.items():
            unknown = set(step) - STEP_KEYS
            if unknown:
                # A typo'd `reveiw:` would silently skip a review gate, and `ouput:`
                # would silently wire nothing. Neither is allowed to pass quietly.
                raise ValueError(
                    "%s.%s: unknown key(s): %s" % (self.name, name, ", ".join(sorted(unknown)))
                )
            self._runner(name, step)
            for f in self.files(name):
                # A pipeline file is a trust boundary: `output: ../../.ssh/config` must
                # not escape the scratch directory.
                if not isinstance(f, str) or not f or f != Path(f).name or f in (".", ".."):
                    raise ValueError(
                        "%s.%s: '%s' must be a plain file name" % (self.name, name, f)
                    )
            for f in step.get("input", []):
                if f not in produced:
                    raise ValueError(
                        "%s.%s: no step produces input '%s'" % (self.name, name, f)
                    )
            want = step.get("resume", True)
            if not isinstance(want, bool) and want not in self.steps:
                raise ValueError(
                    "%s.%s: resume '%s' is not a step" % (self.name, name, want)
                )
            if "next" not in step and "on" not in step:
                raise ValueError("%s.%s: needs next: or on:" % (self.name, name))
            for target in self.targets(name):
                if target != DONE and target not in self.steps:
                    raise ValueError("%s.%s: unknown target '%s'" % (self.name, name, target))

    def _runner(self, name, step):
        """Resolve this step's `uses:` and check `with:` carries what that runner needs.

        An unknown runner or a missing argument is a wiring bug and belongs beside the
        other ones: found when the pipeline loads, not three agent calls later.
        """
        try:
            runner = runners.load(step.get("uses") or self.default_runner, self.runner_dirs)
        except (FileNotFoundError, ValueError) as e:
            raise ValueError("%s.%s: %s" % (self.name, name, e))
        self.runners[name] = runner
        given = step.get("with") or {}
        if not isinstance(given, dict):
            raise ValueError("%s.%s: with: must be a mapping" % (self.name, name))
        required = list(runner.get("requires") or ())
        missing = [k for k in required if k not in given]
        if missing:
            raise ValueError(
                "%s.%s: uses: %s needs with: %s"
                % (self.name, name, runner["name"], ", ".join(missing))
            )
        # effort: and model: are allowed on any agent runner whether or not it declares a
        # template for them - a CLI that cannot express one ignores it at run time and says
        # so. They are rejected on `shell` and `typesafe`, where they mean nothing at all.
        allowed = set(required)
        if runner.get("kind", "agent") == "agent":
            allowed |= set(runners.WITH_OPTIONS)
        unknown = set(given) - allowed
        if unknown:
            # Same reflex as an unknown step key: a `promt:` would otherwise produce a
            # call with no prompt in it at all.
            raise ValueError(
                "%s.%s: uses: %s takes no with: %s (it takes %s)"
                % (self.name, name, runner["name"], ", ".join(sorted(unknown)),
                   ", ".join(sorted(allowed)) or "nothing")
            )
        if "effort" in given and given["effort"] not in EFFORTS:
            raise ValueError(
                "%s.%s: effort must be one of %s, not %r"
                % (self.name, name, ", ".join(EFFORTS), given["effort"])
            )
        if not isinstance(step.get("env", {}), dict):
            raise ValueError("%s.%s: env: must be a mapping" % (self.name, name))

    def kind(self, name):
        """Which of the three mechanisms performs this step, for the engine to dispatch on."""
        return self.runners[name].get("kind", "agent")

    def options(self, name):
        """What the step hands its runner - the `with:` block."""
        return self.steps[name].get("with") or {}

    def environ(self, name):
        """The environment this step's subprocess gets: the pipeline's, then its own."""
        merged = {**self.env, **(self.steps[name].get("env") or {})}
        return {k: str(v) for k, v in merged.items()}

    def label(self, name):
        """The step's human label if it declared one, else its key. Nothing routes on it."""
        return self.steps[name].get("name") or name

    def files(self, name):
        """Every scratch file name this step declares, read or written."""
        step = self.steps[name]
        return ([step["output"]] if "output" in step else []) + list(step.get("input", []))

    def targets(self, name):
        step = self.steps[name]
        out = [step["next"]] if "next" in step else []
        return out + list(step.get("on", {}).values())

    def route(self, name, verdict):
        """(target, is_rework), or None when the verdict has nowhere to go.

        A rework edge points at an equal-or-earlier step; forward edges therefore
        form a DAG by construction.
        """
        step = self.steps[name]
        if "on" in step:
            target = step["on"].get(verdict)
        else:
            target = step.get("next") if verdict == "pass" else None
        if target is None:
            return None
        return target, target != DONE and self.rank[target] <= self.rank[name]


def load(where, name, runner_dirs=None, default_runner=None):
    """Load a pipeline by name. `where` is a directory or an ordered list of them.

    The search order is how a repo's own `.sf/pipelines/` takes precedence over the
    installation-wide ones, and those over the shipped copies: first listed wins.
    `runner_dirs` is the same question for the runners its steps name; left out, only the
    packaged ones resolve.
    """
    dirs = [where] if isinstance(where, (str, Path)) else list(where)
    for d in dirs:
        path = Path(d) / ("%s.yaml" % name)
        if path.exists():
            return Pipeline(yaml.safe_load(path.read_text()), path, runner_dirs, default_runner)
    # The remedy belongs in the message: a fresh installation has no ~/.sf at all,
    # and "no pipeline 'dev'" on its own tells nobody what to do about it.
    known = sorted({f.stem for d in dirs if Path(d).is_dir() for f in Path(d).glob("*.yaml")})
    remedy = ("try --pipeline %s" % ", ".join(known)) if known else (
        "no pipelines anywhere yet - write one in %s/pipelines, or copy one out of the\n"
        "  examples/ directory in the software-factory repository" % REPO_DIR)
    raise FileNotFoundError(
        "no pipeline '%s' in %s\n  %s" % (name, ", ".join(str(d) for d in dirs), remedy)
    )


def summary(path):
    """`{name, version, description}` of a pipeline file, for listings. Never raises.

    Deliberately not `load()`: listing the pipelines in a directory must not blow up
    on a half-written one sitting next to the good ones - it lists as version "?".
    The name is the file stem, because that is the name `load()` resolves.
    """
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
        return {
            "name": path.stem,
            "version": data.get("version", 1),
            "description": data.get("description") or "",
        }
    except (OSError, UnicodeDecodeError, yaml.YAMLError, AttributeError):
        return {"name": path.stem, "version": "?", "description": ""}


def search_path(repo, global_pipelines):
    """Where to look for a pipeline for work in `repo`: its own first, then the global one.

    Two tiers, not three: no pipeline ships with the package. The factory is the engine,
    and the process is yours to write - a default `dev` would be an opinion about someone
    else's review policy, shipped to every repo that never asked for one.
    """
    dirs = [repo_pipelines(repo)] if repo else []
    return dirs + [Path(global_pipelines)]


def repo_pipelines(repo):
    """Where a repo keeps its own pipelines. One definition: `factory config` lists this
    directory too, and it used to spell the path itself and go stale when it moved."""
    return Path(repo) / REPO_DIR / "pipelines"
