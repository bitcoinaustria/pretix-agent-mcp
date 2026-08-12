# PRD — pretix-agent-mcp

Open source (MIT). Self-hosted MCP server that lets Codex and other MCP-compatible
agents operate a self-hosted [pretix](https://pretix.eu) instance through a small
set of purpose-built tools — without the pretix API key ever reaching the agent.

## Goal

Full event administration through agents: the pretix web UI should not be
needed for routine work. Agents create and configure events, manage series
dates, products, quotas, prices, vouchers, orders, and check-in — from natural
language. The only remaining UI tasks are one-time organizer-level setup
(payment provider onboarding such as Stripe/PayPal credentials or OAuth —
which the credential rules forbid agents from touching anyway).

All of this with:

- no pretix credential on the agent's machine or in its context
- no arbitrary REST access
- no PII leaking into agent context unless explicitly enabled
- no unauthorized network access to the server itself

## Security model

```text
Codex / AI agent          (untrusted: may be manipulated via prompt injection)
        │
        │ MCP over streamable HTTP + bearer token   ── or stdio for same-machine dev
        ▼
pretix-agent-mcp          (trusted: holds credential, enforces capabilities,
        │                  redacts PII, gates high-risk writes)
        │ pretix API token (restricted team token)
        ▼
pretix REST API
```

Threat model, in priority order:

1. **Prompt-injected agent** misusing legitimate tool access (exfiltrating PII,
   issuing destructive writes). Mitigations: PII redaction by default, capability
   allowlist, out-of-band approval for high-risk writes.
2. **Unauthorized network client** reaching the MCP endpoint. Mitigations: bearer
   auth required on HTTP transport, bind to localhost by default, TLS via reverse
   proxy documented as the deployment norm.
3. **Credential theft.** Mitigations: token only in server-side env/config, never
   in tool results, logs, errors, or the repo.

Non-goals: protecting against a compromised pretix-agent-mcp host, or against the
pretix instance itself.

## Core requirements

### MCP

- Target MCP spec revision **2026-07-28**
  (<https://modelcontextprotocol.io/specification/2026-07-28>), implemented via
  the official MCP SDK. Keep backward compatibility with clients on earlier
  initialization-based revisions (2025-06-18 / 2025-11-25) per the spec's
  compatibility matrix — Codex may still speak an older revision.
- The 2026-07-28 protocol is **stateless**: no `initialize` handshake, no
  `Mcp-Session-Id`; every request carries its protocol version in `_meta`.
  Consequences for this project:
  - implement `server/discover` (spec MUST)
  - any cross-call state (pending-action IDs, pagination cursors) is a
    **server-minted handle passed as an ordinary tool argument** — the
    spec-blessed pattern (SEP-2567), and exactly how the approval flow works
  - `tools/list` results include the required `ttlMs`/`cacheScope` fields and
    return tools in deterministic order (SDK handles most of this)
- Do not implement the deprecated features: Roots, Sampling, Logging, HTTP+SSE
  transport. Server diagnostics go to stderr; audit records go to the audit log.
- Transports:
  - **streamable HTTP** — the primary transport (single POST endpoint, standard
    `Mcp-Method`/`Mcp-Name` headers). Mandatory bearer token; the server
    refuses to start on a non-localhost bind without a token configured.
  - **stdio** — supported for same-machine development only.
- Work with Codex and other standard MCP clients. Verify Codex streamable-HTTP
  support during MVP; if a gap exists, document a thin local stdio→HTTP proxy
  (credential still never leaves the server).
- Expose structured, purpose-built tools with JSON Schema inputs.
- **No generic HTTP/API-request tool, ever.**

### Client authentication

MCP authorization is OPTIONAL in the spec; when implemented over HTTP it
prescribes full OAuth 2.1 (Protected Resource Metadata per RFC 9728,
authorization-server discovery, resource indicators). That machinery is
overkill for a single-operator self-hosted deployment, so:

- **MVP:** a static bearer token (`Authorization: Bearer <token>`), constant-time
  compared server-side. This is a documented deviation from the spec's OAuth
  framework — acceptable because authorization is optional and the operator
  controls both ends. Follow the spec's token rules regardless: token never in
  the URL query string, invalid/missing token → HTTP 401, insufficient
  capability → 403.
- **Later, if multi-user access is ever needed:** conform to the spec's OAuth
  2.1 authorization (RFC 9728 metadata + an external authorization server).
  Not MVP.

### pretix

- Use only the documented pretix REST API.
- Must work with self-hosted pretix instances.
- Configuration (env vars or a single config file, env takes precedence):
  - `PRETIX_BASE_URL`
  - `PRETIX_API_TOKEN` (restricted team token strongly recommended; README
    documents the minimal permission set per capability)
  - `PRETIX_ORGANIZER` — the organizer slug is **pinned in config**, not supplied
    by the agent
  - optional event allowlist (default: all events under the organizer)
  - `MCP_BEARER_TOKEN` (client credential — distinct from the pretix token)
  - capability allowlist (see Capabilities)
  - PII mode (see Privacy)

### Credential isolation

The pretix API token is held only by pretix-agent-mcp. It must never be:

- returned through MCP (results or error messages)
- written to logs or audit records
- accepted or echoed as a tool parameter
- committed to the repository (ship `.env.example`, gitignore `.env`)
- required on the machine running the agent

### Input validation

All agent-supplied values that become URL path segments (event slugs, order
codes, item/quota/voucher IDs) are validated before use:

- slugs: `^[a-zA-Z0-9][a-zA-Z0-9.\-_]*$`
- order codes: pretix order-code alphabet, uppercased
- numeric IDs: integers only

Anything else is rejected before an HTTP request is built. This is what makes
"arbitrary REST access is impossible" true — treat it as a security control,
with tests.

## Capabilities

Every tool belongs to exactly one capability class. The config allowlist decides
which classes (or individual tools) are exposed; unlisted tools are not
advertised to the client at all.

| Class | Behavior |
|---|---|
| `read` | Executes directly. |
| `write` | Executes directly if enabled in config. |
| `write:high-risk` | Requires out-of-band approval (see below). |

Classes are static per tool, with one dynamic escalation: the **live-event
guard** (see Tools) promotes writes against live events to `write:high-risk`
at call time.

Default configuration: `read` only. Writes are opt-in.

### North-star UX

The measure of done is conversations like these working end to end, with the
human only ever approving the moments that are actually irreversible:

> "Clone this year's conference for 2027, June 12–14, same tickets, early-bird
> until March." → agent clones, adjusts dates and presale windows, reports a
> preview link. Event exists in draft; zero UI visits.
>
> "Add stammtisch dates for every first Thursday until year end, 40 seats
> each." → agent creates the subevents and quotas in one go.
>
> "Sales for the workshop?" → numbers, no PII, no dashboard login.
>
> "Take the 2027 conference live." → agent stages it; you run `approve <id>`
> once on the server. That's the whole ceremony.

Draft-state work is friction-free; the approval ceremony is reserved for the
few genuinely irreversible moments. If routine administration ever sends the
operator back to the pretix UI, that's a gap to close, not an accepted
limitation.

### Tools

Grouped by domain. Every tool carries a capability class (`read`, `write`,
`write:high-risk`). Phases order the implementation; the full surface below is
the committed scope.

**Phase 1 — MVP: read + first writes**

Read:

- `list_events` — events under the configured organizer (respects event allowlist)
- `get_event`
- `list_products` — items with prices, variations, active status
- `list_quotas`
- `get_availability` — quotas with `with_availability=true`
- `search_orders` — filtered + paginated; never returns full unfiltered dumps
- `get_order`
- `search_attendees` — order positions, filtered + paginated
- `sales_summary` — **computed server-side** by paginating orders/positions
  (pretix has no aggregate-stats endpoint). Returns totals/counts only, no PII.
  Document that large events make this slow; cap and report the scan window.

Write:

- `create_voucher` / `create_vouchers_batch` — uses `vouchers/batch_create`
- `resend_order_email` — uses `orders/{code}/resend_link`

**Phase 2 — event lifecycle & catalog** (the "never open the UI" phase)

Event lifecycle:

- `create_event` — from scratch, always created in test mode and non-live;
  the tool description steers agents toward `clone_event` when a prior
  edition exists
- `clone_event` — copies settings, products, quotas from an existing event;
  the primitive for "new edition of X"
- `update_event` — name, dates, location, live/testmode flags (live=true
  escalates, see below)
- `update_event_settings` — the pretix event settings API surface, including
  mail texts, confirmation texts, presale windows, waiting-list toggles
- `set_event_plugins` — enable/disable plugins per event
- tax rules CRUD
- `delete_event` [high-risk]

Series dates (subevents):

- `list_subevents` / `get_subevent` [read]
- `create_subevents` — batch; "every first Thursday until December" is one call
- `update_subevent`
- `delete_subevent` [high-risk once orders exist]

Catalog:

- categories CRUD
- items CRUD — including variations, add-on products, prices
- questions CRUD — attendee questions/form fields
- quotas CRUD

**Phase 3 — order operations & check-in**

- `mark_order_paid` / `extend_payment_deadline` / `add_order_comment`
- `edit_attendee` — name/email/question answers on an order position
- waiting-list: list entries [read], send voucher to entry
- check-in lists CRUD; `list_checkins` [read]
- `cancel_order` [high-risk]
- `refund_order` [high-risk]

**Escalation rule — the live-event guard.** A write that targets a **live**
event and affects price, availability, product structure, or existing orders
is automatically treated as `write:high-risk`, regardless of its static class.
The same operation on a draft/test event executes directly. This single rule
is what makes "fully featured" safe: agents build and reconfigure drafts
without friction, while anything that touches a selling event or a customer's
money goes through the approval queue. `publish_event` (flipping live=true) is
always high-risk — it is the boundary crossing itself.

**Known API limits (documented, not worked around):** payment provider
onboarding (Stripe/PayPal credentials, OAuth connects) is organizer-level,
partly outside the REST API, and involves credentials agents must never
handle — it stays a one-time UI task. Where the pretix settings API exposes
only a subset of UI settings, the tools expose that subset; gaps get listed in
the README rather than papered over with scraping or admin-session tricks.

Not planned: `list_organizers` (a restricted team token sees one organizer;
the organizer is pinned in config), organizer-level team/token management
(agents must not manage their own permissions).

Exact tool names are engineering decisions; the capability classification is not.

## High-risk write approval

A `confirm: true` parameter is not a control — the agent sets it. Mechanism:

1. Agent calls a high-risk tool. The server fetches the current object, records a
   **pending action** (tool, args, object snapshot, expiry ~15 min), and returns a
   human-readable preview plus a pending-action ID. **No mutation happens.**
2. A human approves out-of-band: `pretix-agent-mcp approve <id>` on the server
   (`pretix-agent-mcp pending` lists what's waiting, with previews). No extra
   UI — the CLI is the whole approval surface. The approval lives outside the
   agent's reach on purpose: chat-based confirmation is forgeable by a
   prompt-injected agent; a shell command on the server is not.
   Operators who accept the risk can reclassify individual high-risk tools to
   plain `write` in config (e.g. auto-approve `publish_event` but keep the
   gate on refunds) — an explicit, logged, per-tool decision.
3. After approval, the agent (or the approval itself, config choice) triggers
   execution. Expired or unapproved actions are refused.

This maps cleanly onto the 2026-07-28 protocol: step 1 returns a normal
`"complete"` result containing the pending-action ID (a server-minted handle);
step 3 is a follow-up tool call (`execute_pending_action`) carrying that ID.
The server MAY additionally signal "waiting on approval" via the Multi
Round-Trip Request pattern (`resultType: "input_required"`, which replaced
elicitation in 2026-07-28) or the optional `io.modelcontextprotocol/tasks`
extension for polling — but neither ever substitutes for out-of-band approval
on high-risk tools: client support is uneven and the client is on the
untrusted side of the boundary.

Ordinary `write` tools (vouchers, resend email) do not require approval; they are
gated by the capability allowlist and the pretix token's own permissions.

## Privacy

pretix holds customer PII, and anything a tool returns lands in the agent's
context — potentially in third-party model logs. Therefore:

- **PII mode defaults to `redacted`:** names, emails, addresses, and phone
  numbers in tool results are masked (`j***@example.com`, `M*** K***`). Order
  codes, products, prices, states, and counts pass through — enough for sales
  and operations questions.
- `PII_MODE=full` in server config unmasks results. This is a deployment
  decision by the operator, never a tool parameter.
- All list tools require filters and paginate; default page size ≤ 50, hard
  server-side cap on total results per call.
- `sales_summary` returns aggregates only in either mode.

## Auditability

Append-only audit log (JSONL file is sufficient for MVP) for every write and
every high-risk lifecycle event (proposed / approved / executed / expired /
failed):

- timestamp, tool, arguments (PII-redacted), organizer/event/resource,
  outcome, pending-action ID where applicable

Never log the pretix token, the MCP bearer token, or unredacted PII. Read
operations log at debug level only.

## Deployment

- Fully self-hostable: single binary or `docker run` with env vars. No
  third-party hosted MCP provider involved.
- Designed to run on a separate machine from the agent, so the agent cannot
  reach the credential via filesystem or environment.
- Defaults are closed: binds `127.0.0.1`, `read` capabilities only,
  `PII_MODE=redacted`, no writes. Each relaxation is an explicit config change.
- README documents: reverse-proxy TLS setup, creating the restricted pretix
  team + token with minimal permissions, and Codex client configuration.

## Open source hygiene

- MIT license, `LICENSE` in repo root.
- No secrets or real instance URLs anywhere in the repo, tests, or fixtures.
- Security-sensitive logic (input validation, redaction, approval gate, auth)
  carries tests; CI runs them on every PR.
- `SECURITY.md` with a contact for vulnerability reports.

## MVP success criteria

The MVP is complete when:

- Codex connects over streamable HTTP with a bearer token
- Codex can inspect events, products, quotas, orders and attendees
- Codex can answer basic ticket-sales questions via `sales_summary`
- voucher creation and order-email resend work as controlled writes
- the high-risk approval path works end to end (propose → CLI approve → execute),
  demonstrated with `cancel_order` behind a config flag
- pretix credentials never exist on the Codex machine
- arbitrary pretix REST access is impossible through MCP (validated inputs,
  no generic request tool — with tests proving it)
- PII redaction is on by default and verified by tests
- an unauthenticated network client gets nothing: no tool list, no data
- the whole system is self-hosted end to end

## Full-scope success criterion (north star)

The project is *done* when an operator can run a real recurring event series
for a season — create the next edition by cloning, adjust dates and tickets,
add series dates, go live, answer sales questions, handle a refund — entirely
through an agent, without opening the pretix web UI once, and with the only
manual ceremony being a single `approve` command for publish, refunds,
cancellations, and deletions.

## Product principle

pretix-agent-mcp provides **specific pretix capabilities** to agents, not general
access to pretix. Every tool result is assumed to end up in an LLM context;
design outputs accordingly. Defaults are safe; power is opt-in per deployment.
