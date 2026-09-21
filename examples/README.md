# Example pipelines

Four pipelines, each the shortest one that demonstrates a different shape of process.
Nothing here is installed — **no pipeline ships** — so these are files to copy and then
edit, not defaults you inherit.

| pipeline | the line | needs |
|---|---|---|
| [`dev.yaml`](dev.yaml) | implement → commit | the `claude` CLI |
| [`reviewed.yaml`](reviewed.yaml) | plan → implement → test → review, with rework | the `claude` CLI |
| [`secret-gate.yaml`](secret-gate.yaml) | implement → judge the diff for leaked credentials → commit | `TYPESAFE_API_KEY` |
| [`ship.yaml`](ship.yaml) | implement → commit → push and open a PR | `gh`, authenticated |

Copy one into the repo it should work on, or into `~/.sf/pipelines/` to have it on every
repo. The prompts travel with it — a pipeline's `prompt:` and `questions:` paths are
relative to the pipeline file:

## The prompts

[`prompts/`](prompts/) holds what the agent stages are handed. They are deliberately short:
the engine appends the request, whatever earlier stages produced, and any note a human left,
so a prompt file says only what that station's job is.

`prompts/secrets.yaml` is the odd one out — it is not a prompt but a **judge file**: typed
questions, and the rules that turn the answers into a verdict. It pins its thresholds as
literals so it can be read on its own; `above: $secret` would instead resolve against
`typesafe.thresholds` in `~/.sf/config.yaml`, which is how you tune every gate at once.

[`docs/pipelines.md`](../docs/pipelines.md) is the reference for everything a pipeline can
say, and [`docs/pipeline-schema.yaml`](../docs/pipeline-schema.yaml) is the annotated schema
your editor can use for completion.
