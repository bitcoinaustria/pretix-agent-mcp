# Contributing

Contributions welcome — issues, tools for pretix features not covered yet, and fixes.

```bash
make setup
make ci      # ruff check + format check + pytest; the same thing CI runs
```

Then open a PR against `main` with an imperative subject and a body that says why.

## Before you write code

Read [AGENTS.md](AGENTS.md). It is written for AI agents, but it is the same set of rules
for everyone: the six non-negotiables, how a tool is written, and how capability classes are
assigned. [ARCHITECTURE.md](ARCHITECTURE.md) explains where the seams are and why;
[TESTING.md](TESTING.md) covers the test harness and what your change has to bring with it.

Two things that get PRs sent back:

- **A change to input validation, PII redaction, the capability gate, the approval store or
  auth without tests.** These are the reason the project exists; see the table in
  TESTING.md.
- **Anything that widens what an agent can reach** — a generic request tool, an unvalidated
  path segment, a default that is open rather than closed. If you think one is necessary,
  open an issue first and let's discuss the threat model rather than the diff.

## Adding a tool for a pretix feature

The recurring case. The recipe is in the `add-pretix-tool` skill
(`.claude/skills/add-pretix-tool/SKILL.md`) — readable as plain Markdown whether or not your
editor loads skills, and [tools/events.py](pretix_agent_mcp/tools/events.py) is the
reference implementation. Verify endpoint paths against the pretix API docs rather than
memory, and if pretix cannot do what you need, document the gap in the README's "Known
limits" instead of working around it.

## Reporting a vulnerability

Not here — see [SECURITY.md](SECURITY.md).
