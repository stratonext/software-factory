**What this changes, and why.**

**How you know it works.** For a code change, `task test`. For a pipeline or a prompt, a
real request through it — paste `sf replay <id>`.

- [ ] `task test`, `task lint` and `task compile` pass
- [ ] `task build` passes, if this touches packaging or what the wheel ships
- [ ] A test covers the change, or the change is not logic
- [ ] Docs updated if this contradicts a line in `README.md` or `docs/`
