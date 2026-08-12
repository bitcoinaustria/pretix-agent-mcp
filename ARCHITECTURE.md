# Architecture

One idea carries the design: **everything a tool must not forget happens in one place**, so
64 tools cannot each get it wrong. That place is
[registry.py](pretix_agent_mcp/registry.py).

## The life of a tool call

```text
MCP client (untrusted)
   │  POST /mcp — streamable HTTP, bearer token
   ▼
BearerAuth              server.py     constant-time compare, else 401
   │
   ▼
MCP SDK                 server.py     protocol 2026-07-28 (stateless), schema validation
   │                                  from the tool function's own signature
   ▼
run_tool()              registry.py   ① capability enabled?          → PermissionError
   │                                  ② `event` arg in allowlist?    → ValidationError
   │                                  ③ live-event guard: live and not testmode
   │                                     → escalate `write` to `write:high-risk`
   │                                  ④ high-risk → build preview, store pending
   │                                     action, return a handle. NO MUTATION.
   │                                  ⑤ otherwise execute
   ▼
tool function           tools/*.py    validates ids, calls app.pretix, shapes the result
   │
   ▼
Pretix                  pretix.py     the only HTTP client; holds the token; path built
   │                                  from validated segments under the pinned organizer
   ▼                                  429 → honour Retry-After, retry (pretix Hosted)
   │
   ├── redact()         redact.py     mask PII unless PII_MODE=full
   └── audit.write()    audit.py      writes only; args redacted; never a token
```

The out-of-band half never touches this path: `pretix-agent-mcp approve <id>` flips the
pending action's state in SQLite, and a later `execute_pending_action` call claims it with
`pending.claim()` — a conditional UPDATE, so an approved action executes at most once.

## Why the seams are where they are

- **The registry, not a base class or a middleware chain.** Tools are plain async functions
  whose first argument is the `App`; `server._bind` strips that parameter and hands the rest
  to the SDK, which derives the JSON Schema from the annotations. So a tool has no framework
  to learn and no hook to forget, and the security properties are testable in one file
  rather than 64.
- **The gate keys on the parameter name `event`.** Cheap and blunt: it means the allowlist
  check and the live guard apply to every event-scoped tool automatically, at the cost of a
  naming convention that AGENTS.md and a test enforce.
- **Validation lives below the tools, not in them.** `Pretix._url()` builds every path from
  `path_segments()` under the pinned organizer, so a tool that forgets to validate still
  cannot escape the organizer's namespace — and a registry-wide test fuzzes all 64 tools
  with escape payloads to prove it.
- **Redaction is key-based, not model-based.** A field is masked because its name looks
  personal, so a pretix field nobody has seen yet is masked if it is called
  `attendee_email` and passes through if it is called `price`. A bare `name` is treated as
  an object label unless the record also carries personal fields — masking every event and
  product name made results useless.
- **Approval state is a server-minted handle passed as an ordinary tool argument.** That is
  the pattern the stateless 2026-07-28 protocol prescribes (SEP-2567), and it happens to be
  exactly what the security model needs: the agent can carry the handle but cannot approve
  it.
- **SQLite, not a file or memory.** Approvals must survive a restart, and the conditional
  UPDATE gives at-most-once execution without a lock.

## What is deliberately absent

No generic request tool. No plugin system. No caching layer. No ORM. No abstraction over
the pretix API beyond the tools themselves — a tool is a function that calls one or two
endpoints and shapes the answer, and that is the whole intended depth.
