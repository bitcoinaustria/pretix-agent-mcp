# Working in pretix-agent-mcp

An MCP server that lets AI agents administer a pretix instance without the pretix API
token ever reaching the agent. What the project is and where it is going: [PRD.md](PRD.md)
(product spec and north star) and [README.md](README.md) (operator-facing).

This file is the delta — what you cannot get from reading the code quickly.

## Quality gates

```bash
make setup   # once
make ci      # ruff check + ruff format --check + pytest -q
```

`make ci` is exactly what CI runs, on Python 3.11 and 3.13. Line length 110. The test
harness and what a change owes in tests: [TESTING.md](TESTING.md). No new runtime
dependencies without a reason in the PR body — the current four are `mcp`, `httpx`,
`uvicorn`, `starlette`.

## Non-negotiables

These are the reason the project exists. A change that weakens one is wrong even if tests
pass, and each is enforced by tests that will fail loudly:

1. **No generic HTTP/API/request tool, ever.** Not "just for debugging", not behind a
   config flag. Every pretix capability is a named, schema'd tool.
2. **Every agent-supplied value that becomes a URL path segment is validated** through
   [validate.py](pretix_agent_mcp/validate.py) — `app.check_event()` for event slugs,
   `object_id()` for numeric ids, `order_code()` for order codes. This is what makes
   "arbitrary REST access is impossible" true, not a nicety.
3. **The pretix token never leaves the server**: not in a result, an exception message, a
   log line, an audit record, or a tool parameter. Never interpolate the httpx client or
   its headers into a message.
4. **Defaults stay closed**: localhost bind, `read` only, `PII_MODE=redacted`, no writes.
   Every relaxation is an explicit operator config change.
5. **Approval stays out of band.** No tool may approve, or set the approval state of, a
   pending action — a prompt-injected agent would just call it. The CLI on the server is
   the only approval surface.
6. **Changes to validation, redaction, the capability gate, the approval store or auth
   need tests in the same PR.** See [TESTING.md](TESTING.md).

## Layout

| Path | What lives there |
|---|---|
| [registry.py](pretix_agent_mcp/registry.py) | The choke point: capability gate, live-event guard, redaction, audit. Read this first — see [ARCHITECTURE.md](ARCHITECTURE.md) |
| [validate.py](pretix_agent_mcp/validate.py) | Input validation (a security control) |
| [redact.py](pretix_agent_mcp/redact.py) | PII masking, key-based, applied to every result |
| [pretix.py](pretix_agent_mcp/pretix.py) | The only HTTP client. Holds the token; no method takes a caller-supplied path |
| [pending.py](pretix_agent_mcp/pending.py) | Pending high-risk actions (SQLite) |
| [server.py](pretix_agent_mcp/server.py) | MCP wiring, bearer auth, transports |
| [cli.py](pretix_agent_mcp/cli.py) | `serve`, `tools`, `pending`, `approve`, `reject` |
| `tools/*.py` | One pretix domain each. [tools/events.py](pretix_agent_mcp/tools/events.py) is the reference implementation |
| [tools/_shared.py](pretix_agent_mcp/tools/_shared.py) | `i18n`, `pick`, `clean`, `listing` — use these, don't re-derive |

## Writing a tool

Full recipe: the `add-pretix-tool` skill (`.claude/skills/add-pretix-tool/SKILL.md`) — read
it before adding one. The parts that bite:

- **The first parameter is always `app: App`**; the rest become JSON Schema from their
  annotations, so annotate concretely and return `-> dict`. The docstring **is** the
  description the model reads when deciding whether to call — write it for that reader,
  and say what the tool does *not* do.
- **The event-slug parameter must be named `event`.** The registry keys the allowlist
  check and the live-event guard on that exact name; call it `event_slug` and the tool
  silently loses both.
- **Never redact, audit, or check capabilities inside a tool.** The registry does all
  three for every tool. A tool that does it again is a bug, not defence in depth.
- **Shape the output.** Every result lands in an LLM context: return the few fields that
  answer the question via `pick()`/`listing()`, never a raw pretix object. All list tools
  paginate and cap (`page_size()`), and report truncation instead of implying completeness.
- **`preview=` coroutines run before approval, so they must only read.** They also run
  against un-normalized arguments if called outside `run_tool`, so validate the slug there
  too.
- Prices are decimal strings (`"23.00"`). Reject floats rather than rounding them — a
  float price is a rounding bug waiting to be charged to a customer. Sum with `Decimal`.
- pretix i18n fields arrive as `{"en": ..., "de": ...}`; flatten with `i18n()`.

## Capability classes

`read` executes; `write` executes when enabled; `write:high-risk` records a pending action
and returns a preview, mutating nothing. Pick the class by what the operation can destroy,
not by how it is implemented:

- Anything irreversible (delete, cancel, refund, publish) is `write:high-risk`.
- A write that can change price, availability, product structure or existing orders takes
  `live_guard=True`, which escalates it to high-risk **at call time** when the event is
  live and not in test mode.
- `publish_event` is the only tool that may leave test mode — that, not the `live` flag, is
  what makes orders real. Do not add a `testmode` parameter anywhere else; a test asserts
  no other tool has one.

## pretix API

Verify every endpoint path and field name against the real docs
(`https://docs.pretix.eu/dev/api/resources/<resource>.html`) before relying on memory —
a wrong path is the most common failure here, and the docs have a few singular/plural
typos where the router registers the plural form. Never work around a missing API with
scraping, an admin session, or an undocumented endpoint: implement the subset that exists
and list the gap under "Known limits" in the README.

Both self-hosted and pretix Hosted are supported; the client already handles Hosted's rate
limiting, so don't add retry logic to a tool (see "pretix Hosted" in the README).

## Commits and PRs

Imperative subject line saying what changed; body explains *why*, especially for a
security-relevant change (say what an attacker got before the fix). Keep commits scoped —
this repo's history is meant to be readable as a rationale, not a changelog. Run `make ci`
before opening a PR, and never commit `.env`, an audit log, or a real instance URL, token
or person — fixtures use `tickets.example.org` and invented names.
