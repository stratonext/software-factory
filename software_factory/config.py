"""CLI settings.

One global factory installation under ~/.sf (override with SF_HOME),
configured by a YAML file, with environment variables and then flags layered on top.
"""

import os
from pathlib import Path

import yaml

from . import runners

PATHS = ("backend", "worktrees", "pipelines", "runners")
# Values that reach a subprocess timeout, a range() or a ThreadPoolExecutor, so text
# from YAML or the environment has to become a number before the engine sees it.
INTS = ("concurrency", "step_timeout", "agent_attempts", "retry_wait", "max_input", "name_max")
# YAML already reads `triage: false` as a boolean; the environment only ever hands us
# text, and "false" is truthy.
FLAGS = ("triage",)
ENV = {
    "backend": "SF_BACKEND",
    "worktrees": "SF_WORKTREES",
    "pipelines": "SF_PIPELINES",
    "pipeline": "SF_PIPELINE",
    "concurrency": "SF_CONCURRENCY",
    "triage": "SF_TRIAGE",
    "step_timeout": "SF_STEP_TIMEOUT",
    "agent_attempts": "SF_AGENT_ATTEMPTS",
    "retry_wait": "SF_RETRY_WAIT",
    "max_input": "SF_MAX_INPUT",
    "name_max": "SF_NAME_MAX",
    "runners": "SF_RUNNERS",
    "runner": "SF_RUNNER",
}


def home():
    return Path(os.environ.get("SF_HOME") or "~/.sf").expanduser()


def config_path():
    return home() / "config.yaml"


def defaults():
    """Every setting and the value a fresh install runs on.

    The operational limits below are also each module's own module-level constant, so
    calling `steps.run_agent` or `engine.run` directly still gets today's behaviour.
    """
    root = home()
    return {
        "backend": str(root / "state"),
        "worktrees": str(root / "worktrees"),
        "pipelines": str(root / "pipelines"),
        "runners": str(root / "runners"),
        "pipeline": "dev",
        # The runner a step gets when it does not name one. Change it here to put a whole
        # installation on another CLI; a step's own `uses:` still wins.
        "runner": runners.DEFAULT,
        "concurrency": 2,
        # Ask TypeSafe which pipeline and how much effort every request deserves, without
        # having to say --pipeline auto each time. Needs TYPESAFE_API_KEY; off by default.
        "triage": False,
        # Half an hour per step: long enough for a real test suite or a wide refactor,
        # short enough that a hung `claude` does not hold a worker all day.
        "step_timeout": 1800,
        "agent_attempts": 3,
        "retry_wait": 2,
        "max_input": 20000,
        "name_max": 40,
        # Everything a judge step and submit-time triage need to reach TypeSafe, in one
        # place rather than as constants in judge.py: an installation retunes its gates
        # here instead of editing every questions file in every repo.
        "typesafe": {
            "model": "jev-latest",
            "base_url": "https://api.typesafe.ai/v1/systemone",
            "timeout": 30,
            "attempts": 2,
            # $0.042 per million input tokens, output free (docs.typesafe.ai/models,
            # 2026-09). A setting and not a constant because it is a dated number that
            # goes stale in silence, and it is written into every judged step's
            # permanent cost record.
            "price_per_token": 0.042 / 1_000_000,
            "api_key_env": "TYPESAFE_API_KEY",
            # Jev allows 32k tokens for state plus the longest question; this is the
            # character budget that keeps a request inside it with room to spare.
            "state_max": 60_000,
            # Under this confidence triage is genuinely torn about which pipeline a
            # request belongs in, and the configured default beats half a coin flip.
            "min_confidence": 0.6,
            # Under this, triage reads a request as too vague for anyone to start on.
            "vague": 0.35,
            # Named thresholds a judge file can reference as `above: $secret` instead of
            # pinning a number, so one edit here retunes every pipeline that uses them.
            # A rule that writes a literal still wins.
            "thresholds": {
                "secret": 0.5,
                "destructive": 0.6,
                "implements": 0.5,
                "scope": 1.5,
                "satisfied": 0.6,
                "stubbed": 0.6,
            },
        },
    }


def example():
    """A starter config.

    The path keys are commented out on purpose: left unset they derive from
    SF_HOME at runtime, so the same file works wherever the installation is
    moved to. Writing absolute paths here pins it to one machine.
    """
    d = defaults()
    d["triage"] = str(d["triage"]).lower()  # YAML reads "False" too; lowercase is the idiom
    return (
        "# Software factory settings. Every key is optional.\n"
        "#\n"
        "# Leave the paths commented and they follow SF_HOME, which keeps this file\n"
        "# portable wherever the installation moves to. Uncomment to pin one.\n"
        "\n"
        "# backend: %(backend)s\n"
        "# worktrees: %(worktrees)s\n"
        "# pipelines: %(pipelines)s\n"
        "# runners: %(runners)s\n"
        "\n"
        "pipeline: %(pipeline)s\n"
        "runner: %(runner)s             # the runner a step gets when it names none\n"
        "concurrency: %(concurrency)s\n"
        "triage: %(triage)s            # let TypeSafe pick the pipeline and the effort\n"
        "\n"
        "# Operational limits. step_timeout is the one to raise first: it caps every agent,\n"
        "# command and judge step, and a slow test suite or a wide refactor is what hits it.\n"
        "step_timeout: %(step_timeout)s       # seconds any one step may take\n"
        "agent_attempts: %(agent_attempts)s        # tries for a reply the engine can use\n"
        "retry_wait: %(retry_wait)s            # seconds before a retry, times the attempt number\n"
        "max_input: %(max_input)s         # characters of each input: file a step is handed\n"
        "name_max: %(name_max)s             # characters allowed in --name\n"
        "\n"
        "# Judge steps and submit-time triage. Tune a gate for the whole installation here\n"
        "# rather than editing the thresholds in every questions file in every repo.\n"
        "typesafe:\n" % d + _block(d["typesafe"], "  ")
    )


def _block(mapping, indent):
    """A nested default rendered as YAML - one line per key, in declaration order."""
    lines = []
    for key, value in mapping.items():
        if isinstance(value, dict):
            lines.append("%s%s:" % (indent, key))
            lines.append(_block(value, indent + "  "))
        else:
            lines.append("%s%s: %s" % (indent, key, value))
    return "\n".join(lines) + "\n"


def _expand(key, value):
    # A backend may be a URL later; Path() would mangle "https://" into "https:/".
    if key in PATHS and "://" not in str(value):
        return str(Path(value).expanduser())
    return value


def _typed(key, value, source):
    """One layer's value, as the type the code behind it needs. `source` names the layer."""
    if key in INTS:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("%s: %s must be a whole number, not %r" % (source, key, value))
    if key in FLAGS and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value


def load(**overrides):
    """Settings in effect: defaults < config.yaml < environment < flags."""
    settings = defaults()
    path = config_path()
    if path.exists():
        # This file is hand-edited - `factory init` seeds it and says so - and a stray
        # indent or an unreadable directory must be the CLI's own error, not a traceback
        # from the YAML scanner. One error type out, the way runners.load does it.
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise ValueError("%s: %s" % (path, e))
        if not isinstance(data, dict):
            raise ValueError("%s: must be a mapping of settings, not %s"
                             % (path, type(data).__name__))
        # `typesafe:` is tuned one knob at a time: a file naming only `model:` must keep
        # every other default, and `thresholds:` merges the same way one level further down.
        block = dict(data.pop("typesafe", None) or {})
        block["thresholds"] = dict(settings["typesafe"]["thresholds"], **(block.get("thresholds") or {}))
        settings.update({k: _typed(k, v, path) for k, v in data.items()})
        settings["typesafe"] = dict(settings["typesafe"], **block)
    for key, var in ENV.items():
        if os.environ.get(var):
            settings[key] = _typed(key, os.environ[var], var)
    settings.update({k: _typed(k, v, "--%s" % k) for k, v in overrides.items() if v is not None})
    return {k: _expand(k, v) for k, v in settings.items()}
