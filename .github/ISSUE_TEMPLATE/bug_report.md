---
name: Bug report
about: Something the factory did that it should not have
labels: bug
---

**What happened, and what you expected instead**

**The run.** Output of `sf replay <id>` — that is the timeline of every step, its
verdict and its cost, and it is what actually diagnoses a failed run. If one step is the
problem, add `sf replay <id> --step N` for that step's prompt and reply.

```
paste here
```

**The pipeline file.** The whole YAML of the pipeline the request was on, plus any prompt or
questions file the failing step names. A step is only as reproducible as the file that
defined it.

```yaml
paste here
```

**How to reproduce it**, if you can — the `sf submit` line is usually enough.

**Your installation.** Output of `sf doctor` and `sf config` (the checks, the
settings in effect and the pipelines on disk), plus your OS.
