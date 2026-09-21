"""sf - submit requests, run the factory, inspect what is in flight."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, NoReturn, Optional

import typer
from rich.console import Console
from rich.markup import escape

from . import __version__
from . import runners
from . import config
from . import engine
from . import judge
from . import pipeline as pl
from . import steps
from .backend import BRANCH, STATUSES, now, open_backend

# soft_wrap keeps absolute paths on one line: Rich would otherwise fold them at 80
# columns when stdout is not a terminal. Colour switches itself off there too.
out = Console(soft_wrap=True)
err = Console(stderr=True, soft_wrap=True)

STATUS_COLOUR = {
    "done": "green", "failed": "red", "needs_human": "yellow",
    "running": "cyan", "queued": "dim", "cancelled": "magenta",
}
VERDICT_COLOUR = {"pass": "green", "fail": "red", "human": "yellow"}
# One help string per idea, however many commands take it.
JSON_HELP = "machine-readable output (already the default when stdout is not a terminal)"
ID_HELP = "a request id, as `sf submit` printed it"
MISSING = "no such request: %s"
CLAUDE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
# The options that belong to the app rather than to a command, and how many values each
# takes. `_hoist` uses this to accept them after the subcommand as well as before.
GLOBAL = {"--backend": 1, "--worktrees": 1, "--pipelines": 1, "--skill": 0, "--version": 0}
# Command options whose value is words a human typed - `_hoist` steps over it, so a
# request or a note that starts with a dash is not read as one of GLOBAL.
FREE_TEXT = {"--description", "--name", "--note"}

app = typer.Typer(add_completion=False, help=__doc__, rich_markup_mode="rich")


def as_json(json_flag=False):
    """Whether this command should print JSON instead of the human form.

    One rule for every command: `--json` always, a pipe or a redirect by default -
    whoever is reading it there is a program, and the aligned human blocks are for a
    terminal. `SF_OUTPUT=human|json` pins it, which is how the tests read text.
    """
    if json_flag:
        return True
    mode = os.environ.get("SF_OUTPUT", "")
    if mode:
        return mode == "json"
    return not sys.stdout.isatty()


def emit_json(data):
    print(json.dumps(data, indent=2, default=str))  # plain: this is piped into jq


def kv(pairs, indent="  "):
    """`key: value  key: value` - one dense line. Empty values are dropped."""
    return indent + "  ".join(
        "[dim]%s:[/] %s" % (k, escape(str(v))) for k, v in pairs if v not in (None, "")
    )


def fail(message: str) -> NoReturn:
    """One error style for the whole CLI: a red prefix on stderr, exit 1."""
    err.print("[red]sf:[/] %s" % message)
    raise typer.Exit(1)


def _skip(message):
    """The same error, without the exit: `delete` and `cancel` take many ids, and one
    bad id must not hide the ids after it. The caller exits 1 once, at the end."""
    err.print("[red]sf:[/] %s" % message)


def _confirm(question, yes):
    """One y/N gate for everything destructive: `delete`, `cancel`, `prune`.

    `--yes` says it was already meant, and a stdin that is not a terminal says nobody is
    there to answer - a pipe, a script, a cron line. Anything but `y`/`yes`, a bare Enter
    included, is No: the dangerous answer is never the one you fall into.
    """
    if yes or not sys.stdin.isatty():
        return
    if input("%s [y/N] " % question).strip().lower() not in ("y", "yes"):
        fail("aborted")


def _load(backend, item_id):
    """One item. An unknown id is a message, not the backend's FileNotFoundError path."""
    try:
        return backend.load(item_id)
    except FileNotFoundError:
        fail(MISSING % escape(item_id))


def _report(rows, json_):
    """What `delete` and `cancel` printed, once they know all of it.

    One array for a program - not one object per id, which is a stream no `jq` reads -
    and one dense line per id for a person.
    """
    if as_json(json_):
        emit_json(rows)
        return
    for row in rows:
        out.print(kv(row.items(), indent=""))


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    backend: Optional[str] = typer.Option(None, help="where work items live (default: ~/.sf/state)"),
    worktrees: Optional[str] = typer.Option(None, help="where request worktrees live"),
    pipelines: Optional[str] = typer.Option(None, help="installation-wide pipeline definitions"),
    skill: bool = typer.Option(
        False, "--skill", help="print this CLI's Claude Code skill on stdout, to redirect "
        "into .claude/skills/sf/SKILL.md",
    ),
    version: bool = typer.Option(False, "--version", help="print the installed version"),
):
    if version:
        print(__version__)  # plain: one word, and `factory --version | cat` is a version
        raise typer.Exit()
    if skill:
        # Raw stdout, not `out.print`: rich markup would eat the markdown's [links] and tables.
        sys.stdout.write((Path(__file__).parent / "SKILL.md").read_text(encoding="utf-8"))
        raise typer.Exit()
    try:
        ctx.obj = config.load(backend=backend, worktrees=worktrees, pipelines=pipelines)
    except ValueError as e:
        fail(escape(str(e)))
    if ctx.invoked_subcommand is None:
        # json_ spelled out: called as a function, the parameter's default is typer's
        # OptionInfo object, which is truthy.
        status(ctx, detailed=False, json_=False)  # bare `factory` is "what is in flight", like `docker ps`


def _open(ctx):
    """Backend and settings. Opened here, not in the callback: click handles a
    subcommand's --help after the callback, and --help must not create state."""
    settings = ctx.obj
    try:
        return open_backend(settings["backend"], settings["worktrees"]), settings
    except ValueError as exc:
        fail(str(exc))  # an unknown --backend scheme is a typo, not a traceback


@app.command()
def init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="overwrite an existing config.yaml"),
):
    """Create the installation: ~/.sf, a starter config.yaml, and the directories it uses.

    It seeds no pipelines. None ship - the factory is the engine, and the process is yours
    to write. This makes the directories that process will live in and says where they are.
    """
    root = config.home()
    created, existed = [], []

    def put(path, text):
        # Never silently over an edit: once written, config.yaml is the user's.
        if path.exists() and not force:
            existed.append(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        created.append(path)

    root.mkdir(parents=True, exist_ok=True)
    put(config.config_path(), config.example())
    # Empty, but present and named: a directory you can see is a better answer to "where
    # do pipelines go" than a path in a paragraph somewhere.
    dirs = [Path(ctx.obj[k]) for k in ("pipelines", "runners")]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    if as_json():
        emit_json({"home": str(root), "created": [str(p) for p in created],
                   "unchanged": [str(p) for p in existed],
                   "directories": [str(d) for d in dirs]})
        return
    for path in created:
        out.print(kv([("created", path)], indent=""))
    for path in existed:
        out.print(kv([("unchanged", path)], indent=""))
    for d in dirs:
        out.print(kv([("directory", d)], indent=""))


@app.command()
def submit(
    ctx: typer.Context,
    name: str = typer.Option(..., help="short label for this request, shown in listings (40 chars max)"),
    description: Optional[str] = typer.Option(None, "--description", help="the request itself, in plain words (or --file)"),
    file: Optional[str] = typer.Option(None, "-f", "--file", help="read the request from a file ('-' for stdin)"),
    repo: Optional[str] = typer.Option(None, help="the git repo to work in (default: the current directory)"),
    pipeline: Optional[str] = typer.Option(None, help="the pipeline to run it through; 'auto' asks TypeSafe to pick one, which costs a triage call"),
    effort: Optional[str] = typer.Option(None, help="effort for every agent stage that does not set its own (low|medium|high|xhigh|max), or 'auto' to ask TypeSafe in the same call"),
    force: bool = typer.Option(False, "--force", help="queue it even if triage says the request is too vague"),
    run_now: bool = typer.Option(False, "--run", help="work this request now instead of waiting for `sf run`"),
    json_: bool = typer.Option(False, "--json", help=JSON_HELP),
):
    """Queue a new request."""
    backend, settings = _open(ctx)
    # click has no mutually exclusive group; say which one is missing rather than a usage dump.
    if (description is None) == (file is None):
        fail("give the request with --description or with --file, not both")
    request = description
    if len(name) > settings["name_max"]:
        # A label, not a description - `sf status` gives it one column.
        fail("--name is %d characters; keep it to %d or fewer" % (len(name), settings["name_max"]))
    if file is not None:  # not truthiness: `--file ""` must error, not traceback
        try:
            request = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            fail(escape(str(e)))
    request = (request or "").strip()
    if not request:
        fail("the request is empty")
    path = Path(repo or Path.cwd()).expanduser().resolve()
    problem = _repo_problem(path)
    if problem:
        # Escaped like every other value: a repo path with a [bracket] in it is data.
        fail("%s\n  %s" % (escape(problem), escape(str(path))))
    if effort and effort not in pl.EFFORTS + ("auto",):
        fail("--effort must be one of %s, or auto" % ", ".join(pl.EFFORTS))
    read = _triage(request, path, settings, pipeline, effort)
    if read.get("specific", 1) < settings["typesafe"]["vague"] and not force:
        fail("triage reads this request as too vague to start (%.2f) - say what to change, "
             "or --force\n  %s" % (read["specific"], escape(request[:200])))
    if pipeline == "auto":
        pipeline = read.get("pipeline")  # None falls through to the configured default
    try:
        pipe = pl.load(
            pl.search_path(path, settings["pipelines"]), pipeline or settings["pipeline"],
            runners.search_path(path, settings["runners"]), settings["runner"],
        )
    except (FileNotFoundError, ValueError) as e:
        # ValueError too: a pipeline written for 0.1 fails here with the replacement named,
        # and that message is the whole point of the break - a traceback buries it.
        fail(escape(str(e)))
    item = backend.create(request, pipe.name, pipe.start, repo=str(path), name=name)
    chosen = effort if effort and effort != "auto" else read.get("effort")
    if chosen:
        # On the item, not on the stages: a stage with `with: {effort:}` still wins.
        item["effort"] = chosen
        backend.save(item)
    if as_json(json_):
        emit_json({"id": item["id"], "name": name, "pipeline": pipe.name, "repo": str(path)})
    else:
        out.print(kv([("id", item["id"]), ("name", name), ("pipeline", pipe.name),
                      ("effort", chosen or "")], indent=""))
        if read:
            out.print(kv([("triage", "$%.4f" % read.get("cost_usd", 0)),
                          ("specific", "%.2f" % read["specific"] if "specific" in read else "")]))
    if run_now:
        # ponytail: hand off to `run`, so --run prints exactly what `sf run <id>` prints.
        # detach=False: --run means "work it now", in front of me. repo: submit's --repo is
        # where to work, run's is a filter - the id already says which.
        run(ctx, ids=[item["id"]], repo=None, concurrency=None, detach=False, note="",
            stage=None, json_=json_)


def _triage(request, repo, settings, pipeline, effort):
    """Ask TypeSafe what the user did not say. {} unless asked, and {} on any failure.

    Opt in with `--pipeline auto`, `--effort auto`, or `triage: true` in the config. Never
    blocks: a request must still queue with no key, no network, and no confident answer.
    """
    if not (pipeline == "auto" or effort == "auto" or settings.get("triage")):
        return {}
    return judge.triage(request, _known_pipelines(repo, settings))


def _known_pipelines(repo, settings):
    """{name: description} for every pipeline this request could be routed into.

    Descriptions and not just names because that is what the triage judge chooses by, and
    resolved in `pl.load`'s own order, so a repo's own `dev` describes the line that would
    actually run.
    """
    found = {}
    for d in reversed(pl.search_path(repo, settings["pipelines"])):
        for f in sorted(Path(d).glob("*.yaml")) if Path(d).is_dir() else []:
            found[f.stem] = pl.summary(f)["description"]
    return found


def _repo_problem(repo):
    """Why this repo cannot be worked in - checked at submit, not mid-run."""
    if not repo.is_dir():
        return "no such directory"
    probe = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        # A worktree needs a HEAD; without one the agents get an empty directory.
        return "not a git repository with at least one commit (agents would get an empty workspace)"
    return None


@app.command()
def run(
    ctx: typer.Context,
    ids: Optional[List[str]] = typer.Argument(None, help="only these requests; parked ones are unparked first"),
    repo: Optional[str] = typer.Option(None, help="only requests for this repo; a bare --repo means here (default: every repo)"),
    concurrency: Optional[int] = typer.Option(None, help="how many items to work at once"),
    detach: bool = typer.Option(False, "--detach", help="leave an engine running in the background and return at once"),
    note: str = typer.Option("", help="your answer to the agent, added to its context"),
    stage: Optional[str] = typer.Option(None, help="re-enter at this stage instead of the one the request is parked at"),
    json_: bool = typer.Option(False, "--json", help=JSON_HELP),
):
    """Work the queue; by default everything that is queued.

    Blocks until the queue is worked. `--detach` forks an engine that outlives this command
    and returns at once - the right thing for a long line, the wrong thing for anything that
    should stop when you stop watching it. Blocking, the exit code is 1 unless something
    reached `done`, so it can gate CI.

    """
    backend, settings = _open(ctx)
    path = str(Path(repo).expanduser().resolve()) if repo else None
    ids = list(ids or [])
    # Unpark before deciding to detach: a parked request is not queued, so the detached
    # path below would report "nothing queued" and drop --note on the floor.
    problem = _unpark(backend, ids, note, stage, settings)
    if problem:
        fail(problem)
    if detach:
        items = engine.queued(backend, ids, path)
        if not items:
            out.print("[dim]nothing queued[/]")
            return
        log = _detach(settings, ids, path, concurrency)
        if as_json(json_):
            emit_json({"started": [i["id"] for i in items], "log": str(log)})
            return
        for item in items:
            out.print(kv([("id", item["id"]), ("status", "started")], indent=""))
        out.print("  [dim]sf status[/]  what is in flight")
        out.print("  [dim]tail -f %s[/]" % escape(str(log)))
        return
    done = engine.run(
        backend,
        settings["pipelines"],
        # Passed apart, not collapsed: the flag wins, but a pipeline's own
        # `concurrency:` still gets to beat the configured default.
        concurrency,
        ids=ids,
        repo=path,
        default_concurrency=settings["concurrency"],
        step_timeout=settings["step_timeout"],
        agent_attempts=settings["agent_attempts"],
        retry_wait=settings["retry_wait"],
        max_input=settings["max_input"],
        runners_dir=settings["runners"],
        default_runner=settings["runner"],
    )
    if as_json(json_):
        emit_json([_summary(i, settings, {}) for i in done])
    elif not done:
        out.print("[dim]nothing queued[/]")
    else:
        for item in done:
            out.print(kv([("id", item["id"]), ("stage", item["stage"])], indent="")
                      + "  [dim]status:[/] %s" % _status_markup(item["status"], item.get("reason", "")))
    if done and not any(i["status"] == "done" for i in done):
        # Blocking is what a script and a CI job run: a queue that ended `failed` or
        # `needs_human` has to say so in the exit code, not only on stdout.
        raise typer.Exit(1)


def _detach(settings, ids, repo, concurrency):
    """Re-invoke ourselves to work the queue, and leave it running.

    Opt-in, never the default: a process that outlives the command which started it is
    something you should have to ask for. Two engines racing for the same item is already
    safe - `claim` is a compare-and-set - so a detached run needs no lock of its own.
    """
    # __package__, not a literal: the package was renamed once and this line was not,
    # so every detached run failed to start with nothing but a dead engine log.
    argv = [sys.executable, "-m", __package__]
    for flag in ("backend", "worktrees", "pipelines"):
        argv += ["--%s" % flag, str(settings[flag])]
    argv += ["run", *ids]  # the child blocks: it *is* the engine
    if repo:
        argv += ["--repo", repo]
    if concurrency:
        argv += ["--concurrency", str(concurrency)]

    # ponytail: one appended log for the whole installation. Split per run if it ever
    # gets read more often than it gets grepped.
    # Local paths and file:// point at a directory; anything else logs beside the config.
    url = str(settings["backend"])
    root = url.partition("://")[2] if url.startswith("file://") else url
    log = Path(root if "://" not in root else config.home()).expanduser()
    log.mkdir(parents=True, exist_ok=True)
    log = log / "run.log"
    with open(log, "a") as handle:
        handle.write("\n=== %s %s\n" % (now(), " ".join(argv[2:])))
        handle.flush()
        subprocess.Popen(
            argv,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # The package root, so `-m factory` resolves wherever the CLI was called
            # from, and a new session so the engine outlives this terminal.
            cwd=str(Path(__file__).resolve().parents[1]),
            start_new_session=True,
        )
    return log


def _unpark(backend, ids, note, stage, settings):
    """Naming a parked request means "unpark it and work it". Returns a problem, or None.

    `--note` and `--stage` are how you answer the agent, so they need an id: applying a
    note to the whole queue would be worse than failing. A bare `sf run` still
    ignores parked items - nobody wants the unanswered questions re-run behind their back.
    """
    if (note or stage) and not ids:
        return "--note/--stage needs a request id: which request?"
    items, cache = [], {}
    for i in ids:
        try:
            item = backend.load(i)
        except FileNotFoundError:
            return MISSING % escape(i)
        if item["stage"] == pl.DONE and not stage:
            # The engine has no step called `done` to re-enter; say so instead of raising.
            return "%s is finished - `--stage <stage>` to re-enter it" % item["id"]
        # Every named request checked before any of them is unparked, and checked here
        # rather than left to the engine's guard: `--stage` is how an earlier stage is
        # re-entered, so a typo should answer with the stages there are.
        pipe = _pipeline_of(item, settings, cache) if stage else None
        if pipe and stage != pl.DONE and stage not in pipe.steps:
            return "%s: pipeline '%s' has no stage '%s' - one of: %s" % (
                item["id"], pipe.name, escape(stage), ", ".join(pipe.steps))
        items.append(item)
    for item in items:
        if stage:
            item["stage"] = stage
        if note:
            item["notes"].append({"stage": "human", "text": note})
        if item["status"] != "queued":
            # save() drops the claim marker, so this also recovers an engine that was killed.
            item["status"] = "queued"
            item["reason"] = "resumed %s" % now()
        backend.save(item)
    return None


def _status_markup(status, reason=""):
    text = escape(status + (": %s" % reason if reason else ""))
    colour = STATUS_COLOUR.get(status)
    return "[%s]%s[/]" % (colour, text) if colour else text


def _pipeline_cell(item):
    """`dev@1`, or `dev@1,2` when the file was edited mid-flight.

    The versions this request actually ran steps under, in the order it ran them. A
    queued request has none yet, and history written before versions existed has none
    either - both print the bare name.
    """
    seen = dict.fromkeys(str(h["version"]) for h in item["history"] if "version" in h)
    return item["pipeline"] + ("@%s" % ",".join(seen) if seen else "")
def _pipeline_of(item, settings, cache):
    """The request's pipeline as it is on disk now, or None if it cannot be read.

    Keyed by (repo, name): two requests may name the same pipeline and mean two different
    files. None is a real answer - the repo may have moved or the YAML may be broken, and
    neither the status table nor an unpark check may traceback over someone else's repo.
    """
    key = (item.get("repo", ""), item["pipeline"])
    if key not in cache:
        try:
            cache[key] = pl.load(
                pl.search_path(item.get("repo"), settings["pipelines"]), item["pipeline"],
                runners.search_path(item.get("repo"), settings["runners"]), settings["runner"],
            )
        except Exception:
            cache[key] = None
    return cache[key]


def _stage_label(item, settings, cache):
    """`code 2/5` - the stage, and where it sits in its pipeline's declared order.

    Falls back to the bare stage name when the pipeline cannot be read (repo moved,
    YAML broken) or no longer has the stage (`--stage` into a since-edited pipeline):
    status is the read-only overview and must not traceback over someone else's repo.
    """
    pipe = _pipeline_of(item, settings, cache)
    if pipe is None:
        return item["stage"]
    total, rank = len(pipe.steps), pipe.rank.get(item["stage"], -1)
    if item["stage"] == pl.DONE:
        at = total  # done is not a step, so it reads N/N rather than N+1
    elif rank < 0:
        return item["stage"]  # a stage this pipeline no longer declares
    else:
        # The count is what is behind the request, not what it is about to do: a queued
        # request has not worked the stage it is sitting at, so a new one reads `plan 0/5`.
        at = rank if item["status"] == "queued" else rank + 1
    # The step's own `name:` when it declared one - it is the label a person wrote for
    # this station, and the key is only ever the thing that routes.
    shown = pipe.label(item["stage"]) if item["stage"] in pipe.steps else item["stage"]
    return "%s %d/%d" % (shown, at, total)


@app.command()
def status(
    ctx: typer.Context,
    detailed: bool = typer.Option(False, "--detailed", "-d", help="a block per request, with its request text"),
    json_: bool = typer.Option(False, "--json", help=JSON_HELP),
):
    """What is in flight, across every repo - the `docker ps` of the factory."""
    backend, settings = _open(ctx)
    items = backend.all()
    cache: dict[str, Any] = {}  # one pipeline read per pipeline, for this listing only
    if as_json(json_):
        emit_json([_summary(i, settings, cache) for i in items])
        return
    if not items:
        out.print('no work items. [bold]sf submit --description "<request>" --name <label>[/] in a repo to start one.')
        return
    rows = [_summary(i, settings, cache) for i in items]
    if not detailed:
        return _table(items, rows)
    # Two lines per item, the first one aligned so ids and names stay a column you can
    # scan. Colour and markup go on after the padding, never inside it.
    wid, wname = (max(len(str(r[k])) for r in rows) for k in ("id", "name"))
    for i, r in zip(items, rows):
        out.print("[bold]%s[/]  %s  %s" % (
            escape(r["id"].ljust(wid)), escape(r["name"].ljust(wname)),
            _status_markup(r["status"], _clip(" ".join(r["reason"].split()), 80)
                       if r["status"] == "needs_human" else ""),
        ))
        out.print(kv([("repo", r["repo"]), ("pipeline", r["pipeline"]), ("stage", r["stage"])]))
        out.print(kv([("passes", r["passes"]), ("cost", _cost(r["cost_usd"])),
                      ("request", _clip(r["request"], 60))]))
        out.print()


def _table(items, rows):
    """Every request, one padded line each - the whole list, never paged or cropped.

    Hand-padded, not a rich Table: a Table folds a long needs_human reason at the console
    width, and this view promises one line per request so `| grep` reads it. Colour goes
    on after the padding (STATUS is last, so its markup cannot disturb a column), and
    out.print with soft_wrap neither wraps nor crops.
    """
    head = ("REQUEST", "NAME", "REPO", "PIPELINE", "STAGE", "PASSES", "COST")
    keys = ("id", "name", "repo", "pipeline", "stage", "passes", "cost_usd")
    cells = [tuple(_cost(r[k]) if k == "cost_usd" else str(r[k]) for k in keys) for r in rows]
    widths = [max(len(c[n]) for c in [head] + cells) for n in range(len(head))]
    out.print("[bold]%s  STATUS[/]" % "  ".join(v.ljust(w) for v, w in zip(head, widths)))
    for item, row in zip(items, cells):
        out.print("%s  %s" % (
            "  ".join(escape(v.ljust(w)) for v, w in zip(row, widths)),
            _status_markup(item["status"], item.get("reason", "") if item["status"] == "needs_human" else ""),
        ))


def _summary(item, settings, cache=None):
    """One work item as flat fields - what both the JSON and the human form print."""
    return {
        "id": item["id"],
        "name": item.get("name", "") or "-",
        "repo": Path(item.get("repo", "")).name or "-",
        "pipeline": _pipeline_cell(item),
        "stage": _stage_label(item, settings, {} if cache is None else cache),
        "passes": item["passes"],
        "cost_usd": _total_cost(item["history"]),
        "status": item["status"],
        "reason": item.get("reason", ""),
        "request": " ".join(item["request"].split()),
    }


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit] + "..."


@app.command("config")
def config_cmd(ctx: typer.Context, json_: bool = typer.Option(False, "--json", help=JSON_HELP)):
    """The settings in effect, where they would be changed, and what is in flight."""
    backend, settings = _open(ctx)
    path = config.config_path()
    repos: dict[str, list[Any]] = {}
    for i in backend.all():
        repos.setdefault(i.get("repo", ""), []).append(i)
    if as_json(json_):
        emit_json({
            "config": str(path),
            "config_exists": path.exists(),
            "backend": str(settings["backend"]),
            "worktrees": str(settings["worktrees"]),
            "pipelines": str(settings["pipelines"]),
            "known_pipelines": _known(settings["pipelines"]),
            "runners": str(settings["runners"]),
            "runner": settings["runner"],
            "pipeline": settings["pipeline"],
            "concurrency": settings["concurrency"],
            "triage": settings["triage"],
            "step_timeout": settings["step_timeout"],
            "agent_attempts": settings["agent_attempts"],
            "retry_wait": settings["retry_wait"],
            "max_input": settings["max_input"],
            "name_max": settings["name_max"],
            "home": str(config.home()),
            "output": os.environ.get("SF_OUTPUT", ""),
            "typesafe": settings["typesafe"],
            "repos": {
                repo: {
                    "requests": len(items),
                    "active": len([i for i in items if i["status"] not in ("done", "failed", "cancelled")]),
                    "pipelines": _known(pl.repo_pipelines(repo)) if repo else [],
                }
                for repo, items in sorted(repos.items())
            },
        })
        return
    # The same dense `key: value` lines as every other command; the markup that follows
    # a line is appended after kv(), never handed to it - kv escapes its values.
    out.print(kv([("config", path)], indent="")
              + ("" if path.exists() else "  [yellow](not created)[/]"))
    out.print(kv([("backend", settings["backend"])], indent=""))
    out.print(kv([("worktrees", settings["worktrees"])], indent=""))
    known = _known(settings["pipelines"])
    out.print(kv([("pipelines", settings["pipelines"]),
                  ("known", ", ".join(_label(k) for k in known) or "none")], indent=""))
    for k in known:
        # Only the ones that say what they are for; a directory where nothing declares
        # a description prints exactly the line above and nothing more.
        if k["description"]:
            out.print(kv([(k["name"], k["description"])]))
    out.print(kv([("runners", settings["runners"]),
                  ("known", ", ".join(runners.known(runners.search_path(Path.cwd(), settings["runners"]))))],
                 indent=""))
    out.print(kv([("default pipeline", settings["pipeline"]),
                  ("default agent", settings["runner"]),
                  ("concurrency", settings["concurrency"])], indent=""))
    out.print(kv([("triage", "on - TypeSafe picks the pipeline and the effort" if settings["triage"]
                   else "off   (--pipeline auto, --effort auto, or triage: true)")], indent=""))
    out.print(kv([("step_timeout", "%ss" % settings["step_timeout"]),
                  ("agent_attempts", settings["agent_attempts"]),
                  ("retry_wait", "%ss" % settings["retry_wait"]),
                  ("max_input", settings["max_input"]),
                  ("name_max", settings["name_max"])], indent=""))
    # Neither is a config.yaml key, and both change what the CLI does, so a reader
    # comparing this output against their shell needs to see them here.
    out.print(kv([("home", config.home()), ("SF_HOME", "")], indent=""))
    out.print(kv([("output", os.environ.get("SF_OUTPUT") or "auto - json unless a terminal"),
                  ("SF_OUTPUT", "")], indent=""))
    out.print()
    if not repos:
        out.print("[dim]no repos in flight[/]")
        return
    out.print("[bold]repos in flight[/]")
    for repo, items in sorted(repos.items()):
        live = [i for i in items if i["status"] not in ("done", "failed", "cancelled")]
        out.print(kv([("repo", repo or "-"), ("requests", len(items)), ("active", len(live))],
                     indent=""))
        for d in _known(pl.repo_pipelines(repo)) if repo else []:
            out.print(kv([("pipeline", "%s (from the repo)" % _label(d))]))


def _known(pipelines_dir):
    """The pipelines on disk: name, version and description each, sorted by name.

    Objects and not "dev v2" strings because this is what a machine reads out of
    `--json` when it is choosing a pipeline for a request.
    """
    d = Path(pipelines_dir)
    if not d.is_dir():
        return []
    return sorted((pl.summary(f) for f in d.glob("*.yaml")), key=lambda k: k["name"])


def _label(known):
    return "%s v%s" % (known["name"], known["version"])


@app.command("runners")
def runners_cmd(ctx: typer.Context, json_: bool = typer.Option(False, "--json", help=JSON_HELP)):
    """Every runner a step can `uses:` - where it came from, and what it can do.

    A pipeline naming a runner whose binary is not installed loads fine and fails at the
    step, minutes in. This is the cheap way to find that out first.
    """
    _backend, settings = _open(ctx)
    # cwd as the repo: `sf doctor` inside a project should list that project's own
    # .sf/runners/ too, the same ones its pipelines would resolve.
    dirs = runners.search_path(Path.cwd(), settings["runners"])
    rows = []
    for name in runners.known(dirs):
        try:
            runner = runners.load(name, dirs)
        except ValueError as e:
            rows.append({"name": name, "kind": "?", "binary": "", "path": "", "error": str(e),
                         "supports": [], "default": False})
            continue
        binary = runners.binary(runner)
        rows.append({
            "name": runner["name"],
            "kind": runner.get("kind", "agent"),
            "binary": binary,
            "path": shutil.which(binary) or "" if binary else "",
            "error": "",
            "supports": runners.supports(runner),
            "default": runner["name"] == settings["runner"],
        })
    if as_json(json_):
        emit_json(rows)
        return
    if not rows:
        return out.print("[yellow]no runners found in %s[/]" % escape(", ".join(str(d) for d in dirs)))
    # Hand-padded like `status -t`, and for the same reason: one line per runner, so the
    # answer to "is it installed" is a column you can scan rather than a paragraph.
    names = [r["name"] + ("*" if r["default"] else "") for r in rows]
    wheres = [
        r["error"] or ("built in" if not r["binary"] else r["path"] or "%s not on PATH" % r["binary"])
        for r in rows
    ]
    widths = [max(len(v) for v in column) for column in
              (names, [r["kind"] for r in rows], wheres)]
    for name, where, r in zip(names, wheres, rows):
        # Colour goes on after the padding, so markup cannot disturb a column.
        colour = "red" if r["error"] else "yellow" if not r["path"] and r["binary"] else ""
        cells = [name, r["kind"], where]
        padded = [escape(v.ljust(w)) for v, w in zip(cells, widths)]
        if colour:
            padded[2] = "[%s]%s[/]" % (colour, padded[2])
        out.print("%s  [dim]%s[/]" % ("  ".join(padded), escape(" ".join(r["supports"]) or "-")))
    out.print("\n  [dim]* the default for a step that does not name one[/]")


@app.command()
def show(
    ctx: typer.Context,
    id: str = typer.Argument(..., help=ID_HELP),
    json_: bool = typer.Option(False, "--json", help=JSON_HELP),
):
    """Full state of one item. Piped or --json, it is the item's own JSON."""
    backend, settings = _open(ctx)
    item = _load(backend, id)
    if as_json(json_):
        emit_json(item)
        return
    r = _summary(item, settings)
    # Clipped: a failed agent's reason can be a whole JSON error blob, and a note can
    # be a pytest run. `--json` (or a pipe) still has all of it.
    out.print("[bold]%s[/]  %s  %s" % (
        escape(r["id"]), escape(r["name"]),
        _status_markup(r["status"], _clip(" ".join(r["reason"].split()), 120))))
    out.print(kv([("repo", item.get("repo", "")), ("pipeline", r["pipeline"]), ("stage", r["stage"])]))
    out.print(kv([("passes", r["passes"]), ("steps", len(item["history"])),
                  ("cost", _cost(r["cost_usd"])), ("started", item.get("started", "")),
                  ("ended", item.get("ended", ""))]))
    out.print(kv([("workspace", item.get("workspace", ""))]))
    out.print("\n  %s" % escape(item["request"]))
    for n in item["notes"]:
        out.print(kv([("note", "[%s] %s" % (n["stage"], _clip(" ".join(n["text"].split()), 150)))]))
    out.print("\n  [dim]sf replay %s   the run, step by step[/]" % item["id"])


def _total_cost(history):
    """None when no step reported a cost, so a command-only run is not billed at $0.00."""
    costs = [h["cost_usd"] for h in history if h["cost_usd"] is not None]
    return round(sum(costs), 4) if costs else None


def _cost(value, absent="-"):
    """Cost is optional: no "$0.00" for a step nobody priced. ``absent`` is for prose
    lines, where a lone "-" between " - " separators reads as punctuation."""
    return absent if value is None else "$%.2f" % value


def _duration(seconds):
    if seconds < 60:
        return "%.0fs" % seconds
    return "%dm%02ds" % divmod(int(seconds), 60)


def _arrow(h):
    """What follows a step on the route line. A step that routed nowhere halted there."""
    if "routed_to" not in h:
        return " [halted] "
    return " ~~> " if h.get("rework") else " -> "


def _route(item):
    """The path actually taken, rework loops and all."""
    history = item["history"]
    if not history:
        return "(not started)"
    # Every step but the last carries its own separator, halts included: history is
    # cumulative across runs, so a halted step with nothing after it would have the
    # next run's stage concatenated straight onto its name.
    out_ = "".join(h["stage"] + _arrow(h) for h in history[:-1])
    last = history[-1]
    if "routed_to" in last:
        return out_ + last["stage"] + _arrow(last) + last["routed_to"]
    return out_ + last["stage"] + "  [%s]" % item["status"]


@app.command()
def replay(
    ctx: typer.Context,
    id: str = typer.Argument(..., help=ID_HELP),
    step: Optional[int] = typer.Option(None, help="open up one step: its full input and output"),
    artifact: Optional[str] = typer.Option(None, help="print just this artifact of --step"),
    json_: bool = typer.Option(False, "--json", help=JSON_HELP),
):
    """Play back a run: every step, its route and artifacts."""
    backend, _settings = _open(ctx)
    if artifact and step is None:
        # Silently replaying the whole run instead would look like the name was wrong.
        fail("--artifact needs --step: which step's %s?" % escape(artifact))
    item = _load(backend, id)
    history = item["history"]

    if step is not None:  # not truthiness: `--step 0` must say there is no step 0
        matches = [h for h in history if h["step"] == step]
        if not matches:
            fail("no step %d in %s (%d steps)" % (step, id, len(history)))
        return _replay_step(backend, item, matches[0], artifact)

    if as_json(json_):
        # Purpose-built for a visualiser: the timeline, flattened, with totals.
        emit_json(
            {
                "id": item["id"],
                "request": item["request"],
                "pipeline": item["pipeline"],
                "status": item["status"],
                "stage": item["stage"],
                "passes": item["passes"],
                "reason": item.get("reason", ""),
                "route": _route(item),
                "totals": {
                    "steps": len(history),
                    "cost_usd": _total_cost(history),
                    "duration_s": round(sum(h.get("duration_s", 0) for h in history), 1),
                },
                "notes": item["notes"],
                "steps": history,
            }
        )
        return

    total_time = sum(h.get("duration_s", 0) for h in history)
    out.print(
        "[bold]%s  %s  %s[/]   %d steps - %d rework pass(es) - %s - %s"
        % (item["id"], escape(item["pipeline"]), _status_markup(item["status"]), len(history),
           item["passes"] - 1, _cost(_total_cost(history), "no cost"), _duration(total_time))
    )
    out.print("  [cyan]%s[/]\n" % escape(_route(item)))
    out.print("  %s" % escape(item["request"]))
    if item.get("reason"):
        out.print("  [yellow]parked:[/] %s" % escape(item["reason"]))
    out.print()
    for h in history:
        route = ""
        if h.get("gated"):
            route = "  [magenta]REVIEW[/] -> %s" % escape(h["routed_to"])
        elif h.get("rework"):
            route = "  [yellow]REWORK[/] -> %s" % escape(h["routed_to"])
        elif "routed_to" in h:
            route = "  -> %s" % escape(h["routed_to"])
        elif h["verdict"] == "human":
            route = "  [bold red]HALTED[/]"
        verdict = "%-7s" % h["verdict"]
        colour = VERDICT_COLOUR.get(h["verdict"])
        out.print(
            "  #%-3d %-10s %-8s %-7s %s %-7s %6s%s"
            % (h["step"], escape(h.get("label") or h["stage"]), escape(h["kind"]), _how(h),
               "[%s]%s[/]" % (colour, verdict) if colour else verdict,
               _cost(h["cost_usd"]), _duration(h.get("duration_s", 0)), route)
        )
        notes = " ".join((h["notes"] or "").split())
        if notes:
            out.print("       [dim]%s[/]" % escape(notes[:150] + ("..." if len(notes) > 150 else "")))
        if h.get("artifacts"):
            out.print("       [dim]%s[/]" % escape(" - ".join(sorted(h["artifacts"]))))
    out.print("\n  [dim]sf replay %s --step N   to open one up[/]" % item["id"])


def _how(h):
    """How the call was made: its effort, and whether it resumed an earlier session.

    Blank for history written before any of them existed, and the runner name is only
    interesting once a pipeline uses more than one.
    """
    return " ".join(filter(None, [
        h.get("uses") or "", h.get("effort") or "", "resume" if h.get("resumed") else ""]))


def _replay_step(backend, item, h, only):
    # stderr and the exit code are in the default set: when a step fails, they are
    # usually the only artifacts with anything in them.
    wanted = [only] if only else [
        "input.md", "input.sh", "output.md", "output.txt", "stderr.txt", "exit_code.txt",
    ]
    out.print("[bold]# %s step %d: %s (%s) -> %s[/]\n" % (
        item["id"], h["step"], escape(h.get("label") or h["stage"]),
        escape(h["kind"]), escape(h["verdict"])))
    for name in wanted:
        ref = h.get("artifacts", {}).get(name)
        if not ref:
            continue
        out.print("[cyan]--- %s (%s)[/]" % (escape(name), escape(ref)))
        print(backend.read_artifact(item["id"], ref))  # agent output: never markup
        print()
    rest = sorted(set(h.get("artifacts", {})) - set(wanted))
    if not only and rest:
        # The ones this did not just print - "other" has to mean other.
        out.print("[dim]other artifacts: %s[/]" % escape(" ".join(rest)))


@app.command()
def delete(
    ctx: typer.Context,
    ids: List[str] = typer.Argument(..., help=ID_HELP),
    force: bool = typer.Option(False, "--force", help="also delete an item that is running"),
    yes: bool = typer.Option(False, "--yes", "-y", help="do not ask"),
    json_: bool = typer.Option(False, "--json", help=JSON_HELP),
):
    """Drop requests: the item, its artifacts and its worktree all go."""
    backend, _settings = _open(ctx)
    _confirm("delete %d request(s), their artifacts and their worktrees?" % len(ids), yes)
    # _skip(), not fail(): one bad id must not hide the ids after it.
    deleted, bad = [], False
    for item_id in ids:
        try:
            item = backend.load(item_id)
        except FileNotFoundError:
            _skip(MISSING % escape(item_id))
            bad = True
            continue
        if item["status"] == "running" and not force:
            # A running step writes the item back when it finishes, which would
            # resurrect the JSON we just deleted.
            _skip("%s is running; stop the run or use --force" % escape(item_id))
            bad = True
            continue
        backend.delete(item_id)
        deleted.append({"id": item_id, "status": "deleted"})
    _report(deleted, json_)
    if bad:
        raise typer.Exit(1)


@app.command()
def prune(
    ctx: typer.Context,
    status: Optional[List[str]] = typer.Option(None, "--status", help="statuses to clear; repeatable (default: done)"),
    repo: Optional[str] = typer.Option(None, help="only requests for this repo (default: here)"),
    older_than: Optional[str] = typer.Option(None, "--older-than", help="only ones that ended more than this ago (7d, 12h, 30m)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="say what would go, delete nothing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="do not ask"),
    json_: bool = typer.Option(False, "--json", help="machine-readable"),
):
    """Clear finished requests in bulk: every `done` one, unless you name other statuses.

    Each one goes the way `sf delete` sends it - item, artifacts and worktree - and
    its `sf/<id>` branch goes too, which is the part a long-lived repo accumulates.
    """
    backend, _settings = _open(ctx)
    wanted = list(status) if status else ["done"]
    unknown = [s for s in wanted if s not in STATUSES]
    if unknown:
        fail("no such status %s - one of %s" % (", ".join(escape(u) for u in unknown), ", ".join(STATUSES)))
    cutoff = _ago(older_than) if older_than else ""
    path = str(Path(repo).expanduser().resolve()) if repo else None
    items = [
        i for i in backend.all()
        if i["status"] in wanted
        and (path is None or i.get("repo", "") == path)
        and (not cutoff or _ended_before(i, cutoff, bool(status)))
    ]
    # Never, whatever was asked: a running step holds a pid and writes the item back when
    # it finishes, which would resurrect the JSON this had just deleted.
    doomed = [i for i in items if i["status"] != "running"]
    running = len(items) - len(doomed)
    if not items:
        if as_json(json_):
            emit_json([])
        else:
            out.print("[dim]nothing to prune[/]")
        return
    if doomed and not dry_run:
        _confirm("delete %d request(s), their worktrees and their branches?" % len(doomed), yes)

    rows, freed = [], 0
    for item in doomed:
        row = _pruned(item, "would delete" if dry_run else "deleted")
        if not dry_run:
            backend.delete(item["id"])
            problem = _drop_branch(item)
            if problem:
                row["branch"] = problem
        freed += row["freed_bytes"]
        rows.append(row)
    rows += [dict(_pruned(i, "skipped"), freed_bytes=0, reason="running")
             for i in items if i["status"] == "running"]

    if as_json(json_):
        emit_json(rows)
        return
    for r in rows:
        out.print(kv([("id", r["id"]), ("name", r["name"]), ("status", r["status"]),
                      ("freed", _size(r["freed_bytes"]) if r["freed_bytes"] else ""),
                      ("state", r["state"]), ("reason", r.get("reason", ""))], indent=""))
        if r.get("branch"):
            out.print(kv([("branch", r["branch"])]))
    out.print(kv([("would delete" if dry_run else "deleted", len(doomed)), ("freed", _size(freed)),
                  ("skipped", "%d running" % running if running else 0)], indent=""))


def _pruned(item, state):
    """One request as the flat row both the JSON and the human line print."""
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "status": item["status"],
        "repo": item.get("repo", ""),
        "freed_bytes": _disk(item.get("workspace", "")),
        "state": state,
    }


def _ago(spec):
    """`7d`, `12h` or `30m` as the UTC timestamp that far back - `ended` is a string."""
    units = {"m": 60, "h": 3600, "d": 86400}
    if spec[-1:] not in units or not spec[:-1].isdigit():
        fail("--older-than takes a duration like 7d, 12h or 30m, not '%s'" % escape(spec))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - int(spec[:-1]) * units[spec[-1]]))


def _ended_before(item, cutoff, named):
    """Fixed-width UTC timestamps, so a string comparison is the date comparison.

    A request with no `ended` has no age to measure - it never finished, or it predates
    the field. It goes only when its status was named on the command line, never under
    the default `done`, because "older than 7d" must not sweep up something undated.
    """
    ended = item.get("ended", "")
    return ended < cutoff if ended else named


def _drop_branch(item):
    """Remove the `sf/<id>` branch `backend.delete` leaves behind. A problem, or None.

    Only a branch this request made: no recorded workspace means no worktree was ever
    added for it, so a `sf/<id>` in that repo is somebody else's. Git itself refuses
    to delete a branch that is still checked out, which is the other half of the safety -
    and a branch that will not go is reported, never fatal. The prune is the point.
    """
    repo, branch = item.get("repo", ""), BRANCH % item["id"]
    if not repo or not item.get("workspace"):
        return None
    ref = ["git", "-C", repo, "show-ref", "--verify", "--quiet", "refs/heads/%s" % branch]
    if subprocess.run(ref, capture_output=True).returncode != 0:
        return None  # already gone, or the clone fallback never made one
    done = subprocess.run(["git", "-C", repo, "branch", "-D", branch], capture_output=True, text=True)
    if done.returncode == 0:
        return None
    return "%s kept: %s" % (branch, _clip(" ".join(done.stderr.split()), 120))


def _disk(path):
    """Bytes under a directory - what deleting it hands back.

    The worktree only: it is a checkout of a whole repo where the item itself is a few
    kilobytes of JSON, and it is the one path the Backend contract promises is local.
    """
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass  # vanished under us; it is freed either way
    return total


def _size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return "%d%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024
    return "%.1fT" % n


@app.command()
def cancel(
    ctx: typer.Context,
    ids: List[str] = typer.Argument(..., help=ID_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="do not ask"),
    json_: bool = typer.Option(False, "--json", help=JSON_HELP),
):
    """Stop requests now: flag each on disk, then kill whatever step is mid-flight.

    `sf run <id>` re-queues a cancelled item as it stands - cancel undoes nothing.
    """
    backend, _settings = _open(ctx)
    _confirm("cancel %d request(s)?" % len(ids), yes)
    cancelled, bad = [], False
    for item_id in ids:
        try:
            item = backend.load(item_id)
        except FileNotFoundError:
            _skip(MISSING % escape(item_id))
            bad = True
            continue
        was = item["status"]
        if was in ("done", "failed", "cancelled"):
            _skip("%s is already %s" % (escape(item_id), was))
            bad = True
            continue
        item["status"] = "cancelled"
        item["reason"] = "cancelled %s" % now()
        item["ended"] = now()
        # Saved before the kill, deliberately: the engine must already see `cancelled` on
        # disk by the time the killed step unwinds, or it records the failure instead.
        backend.save(item)
        if was == "running" and item.get("workspace"):
            _kill_step(item["workspace"])
        cancelled.append({"id": item["id"], "status": "cancelled", "stage": item["stage"]})
    _report(cancelled, json_)
    if bad:
        raise typer.Exit(1)


@app.command()
def doctor(ctx: typer.Context, json_: bool = typer.Option(False, "--json", help=JSON_HELP)):
    """Check this installation: the tools, the token, the paths, the pipelines.

    Exits non-zero when something the factory needs is missing, so a setup script can
    gate on it. Every line says what was found and, when it is wrong, what to do.
    """
    settings = ctx.obj  # not _open(): nothing here reads a work item
    checks = []

    def check(name, found, fix="", essential=True):
        checks.append({"check": name, "ok": not fix, "found": found, "fix": fix,
                       "essential": essential})

    git = shutil.which("git")
    check("git", git or "not on PATH",
          "" if git else "install git: every request is worked in a git worktree")
    claude = shutil.which("claude")
    check("claude", claude or "not on PATH",
          "" if claude else "install Claude Code: an agent stage is a `claude -p` call")
    check(CLAUDE_TOKEN, "set" if os.environ.get(CLAUDE_TOKEN) else "not set",
          "" if os.environ.get(CLAUDE_TOKEN)
          else "run `claude setup-token` and export %s" % CLAUDE_TOKEN)

    home = config.home()
    # The nearest existing ancestor when the installation is not there yet: the first
    # command creates it, and what matters is whether it will be allowed to.
    exists = next(p for p in [home, *home.parents] if p.is_dir())
    check("SF_HOME", "%s%s" % (home, "" if exists == home else " (not created yet)"),
          "" if os.access(exists, os.W_OK)
          else "%s is not writable; point SF_HOME at a directory you own" % exists)

    known = _known(settings["pipelines"])
    # No pipeline means nothing can be worked, so this is still essential - but the fix is
    # "write one", not "repair your install". None ship: the process is the user's.
    check("pipelines", "%d in %s" % (len(known), settings["pipelines"]),
          "" if known else "none yet - write %s/pipelines/<name>.yaml in a "
                           "repo (it wins), or one here for every repo." % pl.REPO_DIR)
    judged = False
    for k in known:
        try:
            pipe = pl.load(settings["pipelines"], k["name"])
        except Exception as e:  # whatever a half-written YAML raises is the finding
            # A pipeline in the installation-wide directory was almost certainly seeded by
            # `sf init`, which never overwrites - so a stale one from an older version sits
            # there failing to load forever, and re-seeding is the remedy, not hand-editing.
            check(k["name"], "does not load: %s" % e,
                  "`sf init --force` re-seeds the shipped pipelines over it, or edit/remove "
                  "%s" % (Path(settings["pipelines"]) / ("%s.yaml" % k["name"])))
            continue
        judged = judged or any(pipe.kind(stage) == "judge" for stage in pipe.steps)
        check(k["name"], "v%s, %d step(s), loads" % (pipe.version, len(pipe.steps)))

    # Only when this installation would actually ask: a key nobody needs is not a problem.
    # A judged pipeline merely sitting in the directory is not essential either - it is
    # opt-in per request, and `sf doctor` must not fail a perfectly good install.
    if judged or settings.get("triage"):
        triage = bool(settings.get("triage"))
        key_env = settings["typesafe"]["api_key_env"]
        check(key_env, "set" if os.environ.get(key_env) else "not set",
              "" if os.environ.get(key_env)
              else "export it: %s" % ("triage: true is on, and does nothing without a key"
                                      if triage else "a judge: step parks for a human without one"),
              essential=triage)

    if as_json(json_):
        emit_json(checks)
    else:
        for c in checks:
            out.print(("[green]ok[/]  " if c["ok"] else "[red]no[/]  ")
                      + kv([(c["check"], c["found"]), ("fix", c["fix"])], indent=""))
    if not all(c["ok"] for c in checks if c["essential"]):
        raise typer.Exit(1)


def _hoist(args):
    """Move the global options in front of the subcommand, wherever they were typed.

    click binds an option to the command it follows, so `sf status --backend /tmp/x`
    is "No such option" - and nobody types it the other way round first. Cheaper to move
    them than to explain the rule in four help strings. `--` ends the rewriting, and the
    value of a free-text option is skipped over, so `--description "--version of the API"`
    reaches `submit` as text rather than being hoisted out of it.
    """
    front, rest, i = [], [], 0
    while i < len(args):
        if args[i] == "--":
            rest += args[i:]
            break
        if args[i] in FREE_TEXT:  # its value is data, never a global option
            rest += args[i:i + 2]
            i += 2
            continue
        takes = GLOBAL.get(args[i].partition("=")[0])
        if takes is None:
            rest.append(args[i])
            i += 1
            continue
        width = 1 if takes == 0 or "=" in args[i] else 2
        front += args[i:i + width]
        i += width
    return front + rest


def main(argv=None):
    """The CLI as a function: returns an exit code instead of exiting."""
    args = sys.argv[1:] if argv is None else list(argv)
    # `sf run --repo` with no value means "this repo" (README). Typer has no
    # supported way to declare an option whose value is optional, so fill it in here.
    for i in range(len(args) - 1, -1, -1):
        if args[i] == "--repo" and (i + 1 == len(args) or args[i + 1].startswith("-")):
            args.insert(i + 1, ".")
    args = _hoist(args)
    command = typer.main.get_command(app)
    try:
        command(args=args, prog_name="sf")
    except SystemExit as e:  # click exits for us - --help, usage errors, typer.Exit
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


def _kill_step(workspace):
    """SIGTERM the running step's process group - `claude` and everything it spawned.

    ponytail: no SIGKILL escalation. A step that ignores SIGTERM still has the engine's
    step timeout above it, and the item is already cancelled on disk either way.
    """
    try:
        pid = int((Path(workspace) / steps.SCRATCH / steps.PID_FILE).read_text())
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass  # no step in flight, or it finished between the save and here


if __name__ == "__main__":
    sys.exit(main())
