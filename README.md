<img src="https://raw.githubusercontent.com/stratonext/software-factory/main/assets/factory.svg" alt="" width="192" height="192">

Maintained by [StratoNext](https://www.stratonext.ai).

[![Listed in Awesome Jev](https://awesomejev.vercel.app/badge.svg)](https://awesomejev.vercel.app)

# Local Software Factory

A software factory is a system that turns software requests into finished work through a repeatable, automated process. Instead of handling every request manually, you define a pipeline of stages that moves the work from request to completion, with human review when needed.

This project aims to build a simple **local software factory**.

You can define multiple pipelines, each made up of multiple stages. A new request enters a pipeline and moves through its stages until it is completed or requires human review. Pipelines are defined in YAML, inspired by GitHub Actions, so the workflow itself can live alongside your code and be version controlled.

A pipeline can combine **coding agents and shell commands**. For example, one stage might ask a coding agent to implement a change, another might run tests, and a later stage might ask another agent to review the result.

Multiple requests can run through pipelines in parallel. By default, each request gets its own Git worktree, keeping work isolated so different requests do not interfere with each other.

The project is designed to run locally and reuse the coding agents you already have installed. Instead of requiring a separate API integration or metered API usage, each stage can use the agent CLI and subscription you already have.

The goal is simple: **bring the basic ideas of a software factory to your local machine, with pipelines defined as code and coding agents as workers.**

![Sample pipelines](https://raw.githubusercontent.com/stratonext/software-factory/main/assets/sample-ss.png)

## Install

`sf` is one global command for all your repos, like `docker`. Install it once:

```bash
uv tool install software-factory     # or: pipx install software-factory
```

Then create the installation — `~/.sf`, a starter `config.yaml` and the directories the
factory uses:

```bash
sf init
```

Stages that use a coding agent run that agent's CLI, so it has to be installed and
authenticated once. For the built-in `claude` runner:

```bash
claude setup-token                    # needs a Claude subscription
export CLAUDE_CODE_OAUTH_TOKEN=...    # the token it printed
sf doctor                             # checks git, the CLI, the token, the paths
```

## Quickstart

The first thing to do is to write a pipeline. Put it in `~/.sf/pipelines/` and every repo on the machine can use it:

```bash
mkdir -p ~/.sf/pipelines/prompts

cat > ~/.sf/pipelines/quick.yaml <<'YAML'
name: quick
description: Implement the request with Claude Code, then commit it.
start: code

steps:
  code:
    uses: claude                    # the agent CLI you already have
    with:
      prompt: prompts/code.md       # relative to this file
    next: commit

  commit:
    uses: shell                     # an ordinary command, run in the request's worktree
    with:
      run: "git add -A && git diff --cached --quiet || git commit -m 'sf: worked by the factory'"
    on: { pass: done }              # `done` is the implicit terminal stage
YAML

cat > ~/.sf/pipelines/prompts/code.md <<'MD'
Implement the request below in this repository.

Make the smallest change that does it. Follow the conventions already in the code, and
leave the working tree clean - no scratch files, no commented-out code.
MD
```

The request itself is not in the prompt file: the engine appends it, along with anything
earlier stages produced and any note a human left. You write the instructions, the factory
fills in the work.

Submit **from inside the repo**:

```bash
sf submit --description "add rate limiting to /upload" --name rate-limit --pipeline quick   # -> id 1
sf run                                                     # work everything that is queued
```

And from anywhere, to see what is happening:

```bash
sf status          # everything in flight, across every repo
sf status -m       # the same table, refreshed once a second until Ctrl-C
sf show 1          # the full state of one request
sf replay 1        # play the run back: every step, its route, its artifacts
```

Each request works in its own git worktree under `~/.sf/worktrees/<repo>/<id>/`, so several
can run at once without stepping on each other or on what you are editing.

## Driving the factory with an agent

The factory is a CLI, so the thing best placed to operate it is another agent. `sf` ships
with a skill that teaches one how — every command, the verdicts, how to answer a parked
request, and the shape of a pipeline file:

Load the skill in the agent (`--skill`), then ask it to break the work up and queue it:

> Read the roadmap in `docs/plan.md`, split it into requests small enough for one pipeline
> pass each, and `sf submit` them against `quick` pipeline. Then run the queue and tell me what
> lands.

Ask it to write the process, not just use it:

> We keep shipping changes with no tests. Write me a `.sf/pipelines/dev.yaml` that plans,
> implements, runs `pytest -q`, and sends the work back to the coder when it fails — two
> rework passes, then park for me.

And then leave it to watch: `sf status` is the whole state of the world in one call, so the
agent can poll until every request is `done`, `failed` or `needs_human`, `sf replay <id>`
the ones that went wrong, answer a parked one with `sf run <id> --note "..."`, re-enter an
earlier stage when a plan needs changing, and re-queue what failed — until the queue is
empty.

That is the whole point of the local factory. The agent you are talking to is the foreman,
not the worker: each request it queues is worked by its own agent, in its own git worktree,
on its own branch, several at a time, and none of them can touch the tree you are editing.
One conversation turns into a queue of parallel work you can watch, interrupt, and replay —
`sf cancel` stops it, and the foreman is never the one grading its own diff, because the
pipeline decides that with a test run or a [Jev judgment](#judging-with-jev).

## Writing a pipeline

A pipeline is a YAML file in the repo's own `.sf/pipelines/<name>.yaml`, or in
`~/.sf/pipelines/` for every repo. The repo's own copy wins, so two repos can both have a
`dev` pipeline and mean different processes.

The Quickstart's `quick` is about as small as one gets. Here is the next step up — implement,
test, and send the work back to the coder if the tests fail:

```yaml
name: dev
description: Implement a change, and only keep it if the tests pass.
start: code            # which step a new request enters; defaults to the first
max_passes: 2          # rework round-trips before a human is asked instead

steps:
  code:
    uses: claude                    # which runner performs this step
    with:
      prompt: prompts/code.md       # prompt file, relative to this pipeline
    next: test                      # unconditional edge

  test:
    uses: shell
    with:
      run: "pytest -q"              # exit 0 = pass, anything else = fail
    on: { pass: done, fail: code }  # a backwards edge is rework, and costs one pass
```

Submit against it with `sf submit --description "..." --pipeline dev` — the name is the file's, so
`dev.yaml` is `--pipeline dev`. `~/.sf/pipelines/` serves every repo; a `.sf/pipelines/` in a
repo wins over it, which is how one repo keeps a process of its own. [`examples/`](examples/)
has four more to copy: implement-and-commit, the full reviewed line, a judged secret
gate, and one that opens the pull request.

A step says **who performs it** (`uses:`) and **what to hand them** (`with:`), and needs at
least one of `next:` or `on:`. `done` is the implicit terminal stage. Three runners ship
built in:

| runner | what it is |
|---|---|
| `claude` | Claude Code, non-interactive. `with: {prompt:, effort:, model:}` |
| `shell` | an ordinary command. `with: {run:}` |
| `typesafe` | a typed judgment instead of an agent. `with: {questions:}` |

`sf runners` lists every runner a step can name, whether its binary is on PATH, and what
each one supports. Adding one is a YAML file too, so a stage can run a different agent CLI.

Everything a pipeline can say — `input:`, `output:`, `review:` gates, judge steps,
concurrency, costs — is in [`docs/pipelines.md`](docs/pipelines.md), and
[`docs/pipeline-schema.yaml`](docs/pipeline-schema.yaml) is the annotated schema your editor
can use for completion.

## Judging with Jev

`typesafe` is the third built-in runner, and the one that is not an agent. A step that
`uses: typesafe` sends the diff and the scratch files as **state**, asks the typed questions
you wrote, and gets typed answers back from [TypeSafe](https://typesafe.ai)'s System One
model, **Jev** — a probability, a position on an ordered scale, one of a set of choices. The
pipeline routes on those numbers with thresholds you declare, so the decision is data rather
than an agent's prose.

```yaml
  gate:
    uses: typesafe
    with:
      questions: prompts/review-gate.yaml   # the questions and the routing, beside the prompts
    input: [plan.md]
    output: gate.md
    on: { pass: review, fail: code }        # a cheap filter before the expensive reviewer
```

```yaml
# prompts/review-gate.yaml
state:      { diff: "git diff" }          # shell commands; stdout becomes a named state field
questions:
  secret: { type: noul, instructions: "`diff` hardcodes a credential, key, token or password." }
  scope:  { type: score, instructions: "How far does `diff` go beyond `request`?",
            criteria: ["exactly the request", "small extras", "large unrelated changes"] }
route:                                     # first match wins; a rule with no `when` is default
  - { when: secret, above: $secret, verdict: human, notes: "possible hardcoded credential" }
  - { when: scope,  above: 0.8,     verdict: fail,  notes: "goes well beyond the request" }
  - { verdict: pass }
```

Why bother, when an agent could be asked the same thing in English: a judgment costs about
**$0.0002** where a review agent costs **$1–2**, it answers in milliseconds, and it is not
the worker grading its own work. The notes the factory records carry the number that fired —
`secret 0.71 > 0.5 - possible hardcoded credential` — so a verdict can be argued with. Use it
as a gate in front of the expensive stages, not as a replacement for them.

`above: $secret` reads `typesafe.thresholds.secret` from `~/.sf/config.yaml`, so a gate that
turns out to be too eager is one edit for every pipeline that uses it; a rule that writes a
literal still wins. A `$name` the config does not define parks the request rather than being
read as zero.

The same model can also pick the process for you. `--pipeline auto` and `--effort auto` ask
Jev which pipeline a request belongs in and how hard the agent should think, in one call
before anything is queued, and flag a request too vague for anyone to start on:

```bash
sf submit --description "the upload endpoint 500s on files over 2MB" --pipeline auto --effort auto
```

All of it is **opt-in and off by default**: it needs `TYPESAFE_API_KEY`, and without one
nothing here is reached — submit behaves exactly as it always did. A step that *does* name
this runner and cannot ask — no key, an HTTP error, a rule about a question that was not
answered — returns `human` and parks the request. The factory never guesses a verdict, and a
service it could not reach is a decision nobody made.

The questions are fixed by the pipeline author, and only the diff and the scratch files
arrive as state. That separation is deliberate: an agent wrote the diff, so the diff is not
trusted input, and it must never be able to reach the model as an instruction.
[`docs/pipelines.md`](docs/pipelines.md) has the whole shape, question types included.


## Commands

```bash
sf init                     # create ~/.sf, its config.yaml and its directories
sf submit --description "..." --name x    # queue a request against this repo (--file, --pipeline, --run, --paused)
sf run                      # work the queue (--repo, <id>..., --detach, --note, --stage)
sf daemon start              # keep working the queue as requests land (--interval, --concurrency, --repo)
sf daemon status             # is a background engine running?
sf daemon stop                # stop it; steps already in flight keep going
sf status                   # what is in flight, across every repo (--detailed for the block form, --monitor to watch it)
sf show 1                   # full state of one request
sf replay 1                 # play a run back (--step, --json)
sf config                   # the settings in effect, and where they would be changed
sf runners                  # every runner a step can use, and whether it is installed
sf pause 1 2                 # pull queued requests back out; `sf run <id>` resumes one
sf cancel 1 2               # stop requests now; `sf run <id>` picks one back up
sf delete 1 2               # drop requests: item, artifacts and worktree
sf reset 1 2                # back to the start of the pipeline, notes dropped, history kept
sf prune                    # clear finished requests in bulk (--status, --older-than)
sf doctor                   # is this installation able to work anything?
sf --version                # what is installed
```

Every command prints dense `key: value` for a person and JSON for a program, decided by
where it is going: a terminal gets the text, a pipe or a redirect gets the JSON, and
`--json` forces it anywhere.
