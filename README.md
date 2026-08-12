# pretix-agent-mcp

Run a self-hosted [pretix](https://pretix.eu) instance from an AI agent — without the
pretix API token ever reaching the agent, and without giving it arbitrary REST access.

```text
Codex / AI agent          untrusted: may be manipulated via prompt injection
        │
        │ MCP over streamable HTTP + bearer token   (or stdio for same-machine dev)
        ▼
pretix-agent-mcp          trusted: holds the credential, enforces capabilities,
        │                 masks personal data, gates irreversible writes
        │ restricted pretix team token
        ▼
pretix REST API
```

64 purpose-built tools cover routine event administration: create and configure events,
clone last year's edition, manage series dates, products, quotas, prices, vouchers,
orders and check-in. The web UI is left for one-time organizer setup — notably payment
provider onboarding, which involves credentials an agent must never touch.

> "Clone this year's conference for 2027, June 12–14, same tickets, early-bird until
> March." → the agent clones the event, adjusts dates and presale windows, and hands
> back a preview link. The event exists in draft. Zero UI visits.
>
> "Take the 2027 conference live." → the agent stages it; you run
> `pretix-agent-mcp approve 3f9a1c` on the server. That's the whole ceremony.

## What makes it safe

| Threat | Mitigation |
|---|---|
| Prompt-injected agent exfiltrating personal data | `PII_MODE=redacted` by default: names, emails, addresses and phone numbers are masked in every result. A deployment decision, never a tool parameter. |
| Prompt-injected agent issuing destructive writes | Irreversible operations return a preview and a handle. A human approves them **out of band** on the server. A `confirm: true` parameter would just be set by the attacker. |
| Agent reaching beyond its remit | Capability allowlist; disabled tools are not advertised at all. The organizer slug is pinned in config. Every path segment is validated. **There is no generic HTTP/API tool.** |
| Unauthorized network client | Bearer token required, constant-time compared. Binds `127.0.0.1` by default; refuses a non-localhost bind without a token. |
| Credential theft | The pretix token lives only in the server's environment — never in results, errors, logs, audit records or tool parameters. |

Defaults are closed: localhost, read-only, personal data masked, no writes. Each
relaxation is an explicit config change. See [SECURITY.md](SECURITY.md).

## Install

```bash
git clone https://github.com/bitcoinaustria/pretix-agent-mcp
cd pretix-agent-mcp
uv pip install -e .          # or: pip install -e .
cp .env.example .env         # then edit it
```

Run it:

```bash
set -a && . ./.env && set +a
pretix-agent-mcp serve
```

Or with Docker:

```bash
docker build -t pretix-agent-mcp .
docker run --rm -p 127.0.0.1:8765:8765 --env-file .env \
  -v "$PWD/state:/state" -e STATE_DB=/state/pending-actions.sqlite3 -e AUDIT_LOG=/state/audit.jsonl \
  -e MCP_HOST=0.0.0.0 pretix-agent-mcp
```

Check what a configuration exposes before pointing an agent at it:

```bash
pretix-agent-mcp tools
```

## Connecting a client

Endpoint: `https://pretix-mcp.example.org/mcp` (streamable HTTP), with your
`MCP_BEARER_TOKEN`. No bridge or proxy is needed — Codex and Claude Code both speak
streamable HTTP with a bearer token directly.

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.pretix]
url = "https://pretix-mcp.example.org/mcp"
bearer_token_env_var = "PRETIX_MCP_TOKEN"   # Codex stores the variable NAME, reads it at connect time
tool_timeout_sec = 60                       # sales_summary can be slow on large events
```

Then `export PRETIX_MCP_TOKEN=...` in the shell that launches Codex, or use the CLI:

```bash
codex mcp add pretix --url https://pretix-mcp.example.org/mcp --bearer-token-env-var PRETIX_MCP_TOKEN
```

Gotchas: the key is `url` (there is no `http_url` and no `type = "http"` — transport is
inferred); `bearer_token` is rejected at load in favour of `bearer_token_env_var`; static
headers go in `http_headers = { ... }` and cannot be set from `codex mcp add`.

**Claude Code**:

```bash
claude mcp add --transport http pretix https://pretix-mcp.example.org/mcp --header "Authorization: Bearer $PRETIX_MCP_TOKEN"
```

**Same-machine development** uses stdio, where the parent process is the peer and no
bearer token applies:

```bash
pretix-agent-mcp serve --transport stdio
```

## pretix Hosted (pretix.eu) or self-hosted

Both work — it is the same documented REST API. For pretix Hosted set:

```bash
PRETIX_BASE_URL=https://pretix.eu
```

Two hosted-specific things to know:

- **Rate limits.** pretix Hosted allows 360 requests per minute per organizer for
  token authentication and answers a 429 with `Retry-After`; self-hosted instances do not
  rate-limit by default. The client honours `Retry-After` and retries up to three times,
  so a burst degrades into a slower call rather than an error. `sales_summary` is the tool
  most likely to hit the limit on a big event (one request per 100 orders scanned) — lower
  `SALES_SCAN_CAP`, or ask pretix support for a higher limit, if you run into it.
- **Plugins.** `set_event_plugins` can only enable what the instance actually ships;
  hosted and self-hosted offer different plugin sets. Read the current list with
  `get_event` first.

Nothing else differs: team tokens, the event and order APIs, and the settings API behave
the same, and the shop URLs the tools hand back are correct for either.

## The pretix token

Create a **team** in pretix (Organizer → Teams) with only the permissions your enabled
capabilities need, then add a token to that team. Never use a team that can change teams
and permissions.

| You enable | pretix team permissions needed |
|---|---|
| `read` | *Can view orders*, *Can view vouchers*, plus access to the relevant events |
| `write` (catalog, events, settings) | + *Can create events*, *Can change event settings*, *Can change product settings* |
| `write` (vouchers) | + *Can change vouchers* |
| `write` (orders: mark paid, comment, resend, edit attendee) | + *Can change orders* |
| `write:high-risk` (cancel, refund, delete) | + *Can change orders* / *Can change event settings* |

Restrict the team to specific events where you can, and set `PRETIX_EVENT_ALLOWLIST` as a
second, server-side limit.

## Capabilities

Every tool belongs to exactly one class. `MCP_CAPABILITIES` decides which classes exist;
unlisted tools are never advertised to the client.

| Class | Behaviour |
|---|---|
| `read` | Executes directly. |
| `write` | Executes directly when enabled. |
| `write:high-risk` | Records a pending action and returns a preview. Mutates nothing until a human approves. |

**The live-event guard.** A `write` that targets a **live** event and can affect price,
availability, product structure or existing orders is escalated to `write:high-risk` at
call time. The same operation on a draft or test-mode event executes directly. So agents
build and reconfigure drafts without friction, while anything touching a selling event or
a customer's money goes through approval.

`publish_event` is always high-risk — it is the boundary crossing itself. It sets
`live=true` **and** leaves test mode, because test mode, not the live flag, is what keeps
orders from being real. No other tool can leave test mode, so an agent cannot start real
sales without an approval.

An operator who accepts the risk can reclassify individual high-risk tools to plain
`write` with `MCP_AUTO_APPROVE` (e.g. auto-approve `publish_event`, keep the gate on
refunds). That is an explicit, audited, per-tool decision.

## The approval ceremony

```console
$ pretix-agent-mcp pending

3f9a1c  publish_event  (expires in 812s)
    Take event 'conf27' (Conf 27) LIVE — the public shop opens.
      starts:  2027-06-12T09:00:00+02:00
      presale: 2027-01-15T00:00:00+01:00 → 2027-06-01T00:00:00+02:00
      test mode: True

approve with: pretix-agent-mcp approve <id>   (1 pending)

$ pretix-agent-mcp approve 3f9a1c
approved 3f9a1c (publish_event)
the agent can now call execute_pending_action with this id
```

`approve --run` executes it immediately instead of waiting for the agent. `reject <id>`
throws it away. Nothing here is reachable from the agent: the approval lives on the
server on purpose, because a chat-based confirmation is forgeable by a prompt-injected
agent and a shell command on the server is not.

## Tools

| Domain | read | write | write:high-risk |
|---|---|---|---|
| Events | `list_events`, `get_event`, `get_event_settings`, `list_tax_rules` | `create_event`, `clone_event`, `update_event`¹, `update_event_settings`¹, `set_event_plugins`¹, `unpublish_event`¹, `create_tax_rule`¹, `update_tax_rule`¹ | `publish_event`, `delete_event`, `delete_tax_rule` |
| Series dates | `list_subevents`, `get_subevent` | `create_subevents`¹, `update_subevent`¹ | `delete_subevent` |
| Catalog | `list_products`, `get_product`, `list_quotas`, `get_availability`, `list_categories`, `list_questions` | `create_product`¹, `update_product`¹, `create_product_variation`¹, `update_product_variation`¹, `create_category`¹, `update_category`¹, `create_quota`¹, `update_quota`¹, `create_question`¹, `update_question`¹ | `delete_product`, `delete_quota`, `delete_category`, `delete_question` |
| Orders | `search_orders`, `get_order`, `search_attendees`, `sales_summary` | `mark_order_paid`¹, `extend_payment_deadline`¹, `add_order_comment`, `edit_attendee`¹, `resend_order_email` | `cancel_order`, `refund_order` |
| Vouchers | `list_vouchers` | `create_voucher`, `create_vouchers_batch`¹ | `delete_voucher` |
| Check-in & waiting list | `list_checkin_lists`, `list_checkins`, `list_waiting_list` | `create_checkin_list`, `update_checkin_list`, `send_waiting_list_voucher` | `delete_checkin_list` |
| Approval | `get_pending_action` | `execute_pending_action` | — |

¹ subject to the live-event guard. A single voucher is an ordinary write; a bulk batch is
guarded, because 500 free or quota-blocking vouchers against a selling event is a price and
availability change in all but name (a documented tightening of the PRD, which classed all
voucher tools as plain `write`).

Notes:

- `sales_summary` is computed server-side by paginating orders (pretix has no aggregate
  endpoint). It returns totals and counts only, in either PII mode, and reports its scan
  window — large events are slow and the window is capped (`SALES_SCAN_CAP`).
- All list tools paginate, cap results server-side (≤ 50 per page by default) and mark a
  truncated answer as such.
- `create_subevents` is a batch: "every first Thursday until December, 40 seats each" is
  one call. The agent computes the dates; the server creates them.

## Configuration

Environment variables, or a JSON config file passed with `--config` (env wins). Full list
with comments in [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `PRETIX_BASE_URL` | — | Base URL of your pretix instance |
| `PRETIX_API_TOKEN` | — | Restricted team token. Server-side only |
| `PRETIX_ORGANIZER` | — | Organizer slug, pinned; agents cannot change it |
| `PRETIX_EVENT_ALLOWLIST` | all events | Comma-separated event slugs |
| `MCP_BEARER_TOKEN` | — | Client credential (≥ 24 chars). Required for HTTP |
| `MCP_CAPABILITIES` | `read` | `read`, `write`, `write:high-risk` |
| `MCP_TOOL_ALLOWLIST` | — | Expose exactly these tools |
| `MCP_AUTO_APPROVE` | — | High-risk tools reclassified to `write` |
| `PII_MODE` | `redacted` | `redacted` or `full` |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8765` | Bind address |
| `AUDIT_LOG` | `audit.jsonl` | Append-only audit log |
| `STATE_DB` | `pending-actions.sqlite3` | Pending approvals |
| `APPROVAL_TTL_SECONDS` | `900` | How long a proposal stays approvable |
| `SALES_SCAN_CAP` | `5000` | `sales_summary` scan window |

## Deployment

Put TLS in front of it. Nginx:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8765/mcp;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;              # streamable HTTP may hold the response open
    proxy_read_timeout 300s;
}
```

Keep the bind on `127.0.0.1` and let the proxy be the only route in. If you must bind an
external interface, the server refuses to start without `MCP_BEARER_TOKEN`.

### Alongside a self-hosted pretix container

[docker-compose.example.yml](docker-compose.example.yml) runs this as its **own container**
next to `pretix/standalone`, reaching pretix over the internal network
(`PRETIX_BASE_URL=http://pretix:80`).

Do not extend the pretix image to run both in one container, even though `pip install
pretix-agent-mcp` into a derived image would work. The pretix container holds the database
credentials, the Django secret key and the ticket data; this one holds an API token limited
to a restricted team. Separating them is what keeps a compromise of the agent-facing
service — the component deliberately exposed to an untrusted agent — from becoming a
compromise of the whole instance, which is the difference between "the attacker can do what
that token allows" and "the attacker reads your database". It also keeps `docker pull` an
upgrade instead of an image rebuild, and keeps pretix's own supervisor config untouched.

Two things to keep in mind for that setup:

- Mount a volume for `/state`. Without it, an approved-but-not-executed action and the
  whole audit trail vanish on restart.
- `docker exec` into the MCP container is enough to approve a pending action, so access to
  the Docker socket is equivalent to approval rights. That is the same trust level as shell
  access on the host, which is what the out-of-band approval assumes.

Run the server on a different machine from the agent when you can — that is what makes
"the credential is not on the agent's machine" a structural property rather than a habit.

## Protocol

MCP revision **2026-07-28** via the official Python SDK, which keeps backward
compatibility with the initialization-based revisions (2025-06-18 / 2025-11-25). The
2026-07-28 protocol is stateless: no `initialize` handshake, no session id, protocol
version per request. `server/discover` and the `tools/list` freshness hints
(`ttlMs`/`cacheScope`) come from the SDK; cross-call state — pending-action ids — is a
server-minted handle passed as an ordinary tool argument.

Authorization deviates from the spec's OAuth 2.1 framework (which is optional): a static
bearer token, constant-time compared, appropriate for a single-operator deployment where
the operator controls both ends. The spec's token rules still hold — header only, never
the query string; missing or invalid token gets 401. Multi-user access would mean
implementing RFC 9728 metadata and an external authorization server; that is not built.

Deprecated MCP features are deliberately absent: Roots, Sampling, Logging, HTTP+SSE.
Diagnostics go to stderr; writes go to the audit log.

## Audit log

One JSONL record per write and per high-risk lifecycle event (proposed / approved /
executed / failed), with the tool, PII-redacted arguments, outcome and pending-action id.
Reads are not logged. Neither token ever appears.

```json
{"ts":"2027-01-15T10:22:31Z","event":"executed","tool":"publish_event","outcome":"ok","pending_action_id":"3f9a1c","args":{"event":"conf27"}}
```

## Known limits

These are pretix API limits, not workarounds waiting to happen:

- **Payment provider onboarding** (Stripe/PayPal credentials, OAuth connects) is
  organizer-level, partly outside the REST API, and involves credentials agents must not
  handle. One-time UI task.
- **Event settings** expose the subset the pretix settings API exposes. Where the UI has
  more, the tools have less.
- **Mail routing settings** (`mail_bcc`, `mail_from`, `mail_reply_to`, `smtp_*`) are
  refused by `update_event_settings` on purpose: an agent that can BCC every customer mail
  to an address of its choosing exfiltrates personal data that redaction never sees,
  because it never passes through the agent's context. Change those in the UI.
- **Question options** are not editable via PATCH on a question; recreate the question,
  or edit choices in the UI.
- **Amounts take at most two decimal places** and must be decimal strings — a float is
  refused rather than rounded, because a rounded price is a rounding bug charged to a
  customer. Currencies with three decimal places (KWD, BHD, TND) are therefore not
  supported; widen `PRICE_RE` in [validate.py](pretix_agent_mcp/validate.py) if you
  need one.
- **Checking someone in** is deliberately not a tool: scanning tickets is a
  physical-presence operation.
- Organizer-level team and token management is out of scope by design — agents must not
  manage their own permissions.

## Development

```bash
make setup
make ci      # ruff check + format check + pytest, the same thing CI runs
```

- [AGENTS.md](AGENTS.md) — the working rules (and what Claude, Codex and friends load)
- [ARCHITECTURE.md](ARCHITECTURE.md) — where the seams are and why
- [TESTING.md](TESTING.md) — the fake-pretix harness and what a change owes in tests
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to open a PR
- [PRD.md](PRD.md) — the product spec this was built from

MIT licensed. Contributions welcome — changes to validation, redaction, the approval gate
or auth need tests.
