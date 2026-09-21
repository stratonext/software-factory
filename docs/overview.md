# Overview

A software factory turns a written request into reviewed, security-checked work without a
human driving each step. You describe the *process* once, as a pipeline; the factory runs
every request through it and tells you where each one is.

## The three nouns

**Request.** A sentence of intent — "add rate limiting to /upload". It is the unit of work.
It has an id, **the repo it belongs to**, a pipeline, a current stage, a count of rework
passes, an accumulating list of notes, and a full history of what every stage did to it.

The repo belongs to the request, not to the factory. One installation — one `~/.sf`,
one queue — serves every repo on the machine, the way one Docker daemon serves every image.
You submit from inside a project and ask `sf` from anywhere what is in flight.

**Pipeline.** The process definition, written by you in YAML: the ordered stages a request
passes through and the rules for moving between them. Different kinds of work deserve
different processes — a docs change should not have to clear the same security gate as an
auth change — so there can be many pipelines, and a request names the one it enters.

**Stage.** One station on the line, and the **runner** that works it. A runner is an agent
CLI given a role prompt, the original request, the accumulated notes and a working copy of
the code; or an ordinary shell invocation like a test suite; or a typed judgment. A stage
names the one it wants with `uses:`, so two stages of the same pipeline can be on two
different tools. Every runner returns the same thing: a verdict.

## What makes it a factory rather than a script

Three properties, and they are the whole design:

*Work flows backwards as well as forwards.* When the security stage finds a hardcoded
secret, the request does not fail — it returns to planning with the finding attached, and
comes back around. Rework is the normal case on a real line, not an error path.

*Rework is counted and bounded.* Every backwards trip increments a counter. When a request
has been round the loop more times than the pipeline allows, it stops and waits for a person.
An agent loop that cannot converge is the expensive failure mode, and this is the brake.

*An agent can stop the line itself.* Any stage can return `human` instead of a verdict: the
request halts exactly where it stands, with the agent's question attached, and waits. This is
what an agent should do when the requirement is ambiguous, the operation is irreversible, or
a decision is not its to make. Asking is cheap; guessing at scale is not.

## What it is not

Not a CI system — it does not react to commits, and the steps are not expected to be
deterministic or cacheable. Not an agent framework — it does not care what an agent is, only
that something can be invoked and will return a verdict, which is what the runner registry
behind `uses:` makes true rather than aspirational. Not a scheduler — one engine
process, one machine, work items in a directory.

It is a state machine with agents in the boxes, a disk to remember, and a human escape hatch.

## Where to go next

The prose is here; the reference is [`../README.md`](../README.md), which is the one place
that lists every command and every YAML key.

| Read this | When |
|---|---|
| [pipelines.md](pipelines.md) | You are **writing** a pipeline: stages, edges, rework, verdicts, judge steps, and what a run costs. |
| [operating.md](operating.md) | You are **running** one. The states a request moves through, what every step records and how to replay it, and the failure modes that actually happen. |
| [internals.md](internals.md) | You are **changing** the code, or writing a storage backend that is not a local directory. |
| [pipeline-schema.yaml](pipeline-schema.yaml) | Your editor wants a schema for pipeline YAML. Documentation, not a second validator. |
