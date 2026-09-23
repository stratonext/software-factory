# Judging with Jev

`typesafe` is the third built-in runner, and the one that is not an agent. A step that
`uses: typesafe` sends the diff as **state**, asks typed questions you wrote, and gets a
probability, a position on a scale or a choice back from [TypeSafe](https://typesafe.ai)'s
System One model, **Jev** — so a pipeline routes on data with thresholds you declare, rather
than an agent's prose. A judgment costs about **$0.0002** against **$1–2** for a review
agent, so it pays for itself the first time it filters a diff before the expensive reviewer
sees it.

It is **opt-in and off by default**: it needs `TYPESAFE_API_KEY`, and without one nothing
here is reached — submit behaves exactly as it always did. A step that does name this runner
and cannot ask — no key, an HTTP error, a question left unanswered — returns `human` and
parks the request rather than guessing.

The same model can also pick the process for you: `--pipeline auto` and `--effort auto` ask
it which pipeline a request belongs in and how hard the agent should think, before anything
is queued, and flag a request too vague for anyone to start on.

[`docs/pipelines.md`](pipelines.md#the-third-kind-of-stage-judge) has the whole shape —
question types, thresholds, the `typesafe:` config block — and
[`examples/secret-gate.yaml`](../examples/secret-gate.yaml) is a working gate to copy.
