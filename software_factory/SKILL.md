---
name: sf
description: Drive the `sf` CLI in this repo - submit a request to the software factory, run the queue, see what is in flight, read a run back with replay, answer a request that is parked as needs_human, or write a pipeline YAML. Use whenever the user mentions the factory, `sf submit`, `sf run`, a request id like 7, a parked or needs_human request, or a pipeline stage/verdict.
---

# Using the factory

A **request** (a sentence of intent) is worked through a **pipeline** (YAML you write) one
**stage** at a time. Each stage names the **runner** that performs it — an agent CLI with a
role prompt, a shell command, or a typed judgment. All of them return a verdict; the verdict
picks the next stage.

One global installation (`~/.sf`) serves every repo on the machine, like one Docker
daemon serves every image. Requests outlive your shell: submit here, ask from anywhere.

## Preflight

- `sf config` — settings in effect, known pipelines, repos in flight. Run it first if
  anything looks misconfigured. `sf init` creates `~/.sf` and its directories. **No
  pipeline ships** - if none exists yet, one has to be written before anything can run.
- **Submit from inside the repo the work belongs to**, and it must be a git repo with at
  least one commit — a worktree needs a `HEAD`, so `submit` refuses without one.
- The absolute repo path is recorded at submit time, so **submit and run must agree on it**.
  `--repo <path>` overrides the current directory; whatever it resolves to is what the run
  must be able to reach.

## Commands

| Command | What it does |
|---|---|
| `sf submit --description "<request>" --name <label>` | queue a request; prints its id. `--file <path>` (`-` for stdin) for a long request, `--pipeline <name>` (`local:<name>` / `global:<name>` when the same name exists in both tiers), `--repo <path>`, `--run` to work it immediately, `--paused` to queue it without letting `sf run`/`sf daemon` pick it up yet |
| `sf run` | work the queue, **blocking until it is done**. `--detach` leaves an engine working in the background instead, `--repo` limits to this repo, `<ids>` to only these, `--concurrency N` |
| `sf pause <ids>` | pull queued requests back out of the queue without cancelling them; `sf run <id>` resumes one, same as unparking `needs_human` |
| `sf daemon start` | leave an engine running that keeps working the queue as requests land, polling every `--interval` seconds (default 5) under the same `--concurrency` quota `sf run` uses. `sf daemon status` / `sf daemon stop` |
| `sf` / `sf status` | every request across every repo — the `docker ps` of the factory: one line per request, a table you can `| grep`. `-d`/`--detailed` is the fuller block form, with each request's text. `-m`/`--monitor` keeps the table on screen and refreshes it once a second until Ctrl-C — a terminal only, so never yours: poll `sf status` instead |
| any command | **you get JSON**: output is JSON whenever stdout is not a terminal, which it never is for you. `--json` says so explicitly; `SF_OUTPUT=human` gets the text form |
| `sf show <id>` | the raw item JSON (pipe to `jq`) |
| `sf replay <id>` | play the run back: every step, its route, cost, artifacts. `--step N` opens one up, `--artifact <name>` picks one out, `--json` is the flattened timeline |
| `sf pipelines` | every pipeline the factory can see: one padded line each — name, file, flavor. Says which file a bare `--pipeline <name>` reaches when the name is in both tiers. `-d`/`--detailed` is a block per pipeline with its path and description |
| `sf runners` | every runner a step can `uses:`, whether its binary is on PATH, and what it supports |
| `sf run <id> --note "..."` | answer a parked request; resumes from `item.stage` with your note in context |
| `sf run <id> --stage <stage> --note "..."` | reject: re-enter at an earlier stage |
| `sf cancel <ids>` | stop them now; `sf run <id>` picks one back up. Asks `y/N` first, `--yes` to skip |
| `sf reset <ids>` | take them back to the start of their pipeline and work them from scratch: stage back to the pipeline's `start`, passes back to zero, notes emptied, queued again. `history` stays, so `sf replay` still reads the whole life of the request, and so do the worktree and the `sf/<id>` branch. A `done` request resets; a `running` one is refused. Asks `y/N` first, `--yes` to skip |
| `sf delete <ids>` | drop the items, their artifacts and their worktrees. `--force` if one is running. Asks `y/N` first, `--yes` to skip |
| `sf prune` | housekeeping: delete finished requests in bulk — every `done` one by default. `--status <s>` repeatable (`failed`, `cancelled`, `queued`, `needs_human`), `--repo`, `--older-than 7d`, `--dry-run` to see what would go, `--yes` to skip the `y/N` confirmation. Never touches a `running` request, and takes each one's `sf/<id>` branch with it |
| `sf doctor` | one line per check of this installation: git, the `claude` CLI and its token, `SF_HOME`, the pipelines. Exits non-zero if something essential is missing — run it when a command behaves oddly |

## The loop

1. `sf submit --description "<request>" --name <label>` from inside the repo → note the id.
2. `sf run` (or `sf run <id>`).
3. Poll `sf` until the row is `done`, `failed` or `needs_human`.
4. `sf replay <id>` to read what happened. The work is the diff on branch `sf/<id>`
   in its own worktree — `<worktrees>/<repo name>/<id>`, `~/.sf/worktrees/...` by default,
   and printed as `.workspace` by `sf show <id>`. Not in the repo you submitted from.

`sf run` **blocks** until the queue is worked, exiting non-zero when nothing reached
`done` — so its exit code is a real answer and can gate CI. `--detach` is the opt-in form
that returns at once: after that one its exit code says only that the engine started, so
poll `sf` rather than reporting anything as done.

`sf run <id>` is also the recovery path for a request left `running` by a killed engine.

## When a request is parked (`needs_human`)

`needs_human` means an agent stopped and asked a question, or a `review: true` gate is
waiting. **That is a question for the user, not for you to answer.**

```bash
sf                           # find the parked request and its one-line reason
sf replay 1             # the route, and which step HALTED
sf replay 1 --step 4    # the agent's full reply — the actual question
```

Relay the question to the user, then act on their answer:

```bash
sf run 1                                  # approve a review gate as-is
sf run 1 --note "approved, that key is a test fixture"
sf run 1 --stage plan --note "requirement changed, redo"
```

Where the note lands depends on **why** it parked — the three cases differ:

- **An agent asked** (`human` verdict, no parseable verdict, no route for the verdict, or a
  judge step that could not reach a decision): nothing was routed, so
  `sf run <id> --note "..."` re-runs **that same stage** with the note in context. It does
  not count as a rework pass.
- **`max_passes` exhausted**: the backwards edge was already taken and the pass already counted,
  so the stage has advanced to the rework target. `sf run <id>` runs *that* target (e.g.
  `code`), not the reviewer that failed it. And `passes` stays above `max_passes`, so the next
  backwards edge parks it again immediately — the way out is `--stage` plus a change of scope,
  not another `--note`.
- **A `review: true` gate**: the step's verdict was routed *before* parking, so the stage has
  already advanced. `sf run <id>` resumes at the **next** stage and the note goes forward
  into it — the gated step does not re-run.

To force an earlier stage to run again in either case, name it: `--stage <name>`. `--note` and
`--stage` both require an id; a bare `sf run` deliberately ignores parked requests.

## The verdict contract

Every agent prompt is suffixed with a request for a closing JSON object:

```json
{"verdict": "pass" | "fail" | "human", "notes": "..."}
```

`pass` and `fail` route via the step's edges. `human` halts the request where it stands.
**No parseable verdict also parks it** — the factory never guesses. The engine appends that
block to every agent prompt at run time, so a prompt file under the pipeline's own `prompts/` must not
restate it, and must not tell the agent to end its reply with anything else.

## Writing a pipeline

`.sf/pipelines/<name>.yaml` in the repo wins over `~/.sf/pipelines/<name>.yaml`,
and that is the whole search path - nothing ships. A step says who performs it
(`uses:`) and what to hand them (`with:`).

`sf pipelines` is what is actually on disk in both tiers. When a name exists in both, a
bare `--pipeline dev` is the repo's own; `--pipeline local:dev` and `--pipeline global:dev`
name one tier each, and the qualifier is kept on the request, so `sf status` shows
`local:dev` and the run resolves the same file. `local:` on a request with no repo is an
error, never a quiet fall back to the global file.

```yaml
name: dev
max_passes: 3          # rework round-trips before a human is asked
start: plan            # defaults to the first step

steps:
  plan:
    name: Write the plan       # optional label, shown in status and replay
    uses: claude               # which runner; omitted, the configured default
    with:
      prompt: prompts/plan.md  # path relative to this file
      # effort: low            # low|medium|high|xhigh|max
    output: plan.md            # what later steps can read
    next: code
    # review: true             # park for a human after this step
  gate:
    uses: typesafe             # typed questions + thresholds instead of an agent
    with:
      questions: prompts/review-gate.yaml
    on: { pass: commit, fail: code } # parks for a human without TYPESAFE_API_KEY
    # resume: false            # start cold; the default resumes the previous stage's
                               # session, so the line is one conversation and earlier
                               # context is not billed again. A stage name picks that
                               # stage's own last session.
  test:
    uses: shell
    with:
      run: "pytest -q"         # exit 0 = pass
    output: test.log
    on: { pass: review, fail: code }
  review:
    uses: claude
    with:
      prompt: prompts/review.md
    input: [plan.md, test.log] # injected as `# Input: <name>` sections
    on: { pass: done, fail: code }
```

Rules the loader enforces:

- A step needs **at least one** of `next:`/`on:`, and its `with:` must carry what the runner
  its `uses:` names requires — `prompt:` for an agent, `run:` for `shell`, `questions:` for
  `typesafe`. An unknown runner, or a `with:` key it does not take, fails at load.
- **Declaration order is the rank.** An edge to a later step is the happy path; an edge to an
  equal-or-earlier step is *rework* — it carries notes back and increments `passes`. Past
  `max_passes` the request parks instead of looping forever.
- An `input:` name no step produces is a wiring bug and is rejected at load, as are unknown
  step keys and any name escaping `_FACTORY/`.
- A missing input is fine: on the first pass the reviewer has not run yet.
- `done` is the implicit terminal stage.

## Gotchas

- `_FACTORY/` in a worktree is factory bookkeeping, not the deliverable. It ignores itself.
- Never fabricate a status. Read it from `sf` / `sf show`.
- `--backend` takes a path or `file://`; any other scheme is an error today.
- A step that fails hard: `sf replay <id> --step N` prints its stderr and exit code. To
  rerun it by hand, `--artifact command.txt` for an agent step, `--artifact input.sh` for a
  `uses: shell` one. Ask for an artifact the step never wrote and replay prints nothing.

Depth lives in the software-factory repo's own `README.md` (YAML and command reference) and
its `docs/` directory (why it works this way).
