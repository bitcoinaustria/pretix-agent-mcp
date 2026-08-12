# Security Policy

## Supported versions

pretix-agent-mcp is pre-release. Only `main` receives fixes; there are no maintenance
branches, and there are no tagged releases yet.

| Version | Supported |
| --- | --- |
| `main` | yes |
| anything older | no — update first |

## Reporting a vulnerability

**Do not open a public issue, pull request, or discussion for a security-impacting bug.**

Use any of these:

- Email `security@bitcoin-austria.at`.
- Signal: `BitcoinAT.21`.
- GitHub → the repository's **Security** tab → **Report a vulnerability**
  ([private advisory](https://github.com/bitcoinaustria/pretix-agent-mcp/security/advisories/new)
  — keeps the report, the fix, and the credit in one place).

Please include:

- the affected commit, and your pretix version (self-hosted or pretix Hosted),
- the relevant configuration: which capability classes are enabled, `PII_MODE`, and any
  `MCP_AUTO_APPROVE` entries — the same bug is often harmless under the defaults and severe
  under a relaxed config,
- which of the mitigations below you got past, and what an attacker gains,
- a reproduction: the tool call or HTTP request, or a minimal command sequence.

**Never send real credentials or customer data.** Not your pretix API token, not the MCP
bearer token, not an unredacted order or attendee record, not an audit log excerpt with
personal data in it. Describe them, or use the fixture conventions from `tests/`
(`tickets.example.org`, invented names).

## What to expect

- Acknowledgement within 7 days.
- An assessment (accepted / not-a-vulnerability / duplicate) and, if accepted, a rough fix
  timeline in the same thread.
- Coordinated disclosure: we publish an advisory and credit you, unless you prefer
  otherwise. Please hold public details until the fix ships.
- There is no bug bounty. This is a volunteer project.

## What this project defends against

In priority order:

1. **A prompt-injected agent** misusing legitimate tool access — exfiltrating personal data,
   or issuing destructive writes. Mitigations: PII masked by default, a capability allowlist
   that decides which tools exist at all, and out-of-band human approval for irreversible
   writes (a `confirm: true` parameter would be set by the attacker).
2. **An unauthorized network client** reaching the MCP endpoint. Mitigations: bearer token
   required on the HTTP transport, `127.0.0.1` bind by default, TLS terminated by a reverse
   proxy as the documented deployment.
3. **Credential theft.** The pretix API token exists only in the server's environment. It is
   never returned through MCP, never written to logs or audit records, never accepted as a
   tool parameter, and never needed on the machine running the agent.

## Scope

In scope — anything that:

- leaks the pretix API token or the MCP bearer token,
- reaches a pretix endpoint the tool surface does not intend (a path escape, a way to
  address another organizer, an event outside `PRETIX_EVENT_ALLOWLIST`),
- returns personal data that `PII_MODE=redacted` should have masked,
- mutates anything through a `write:high-risk` tool without an out-of-band approval, or
  executes an approved action more than once,
- lets an unauthenticated or wrongly-authenticated HTTP client get any data or tool list,
- lets an agent widen its own permissions, or exfiltrate data through a channel redaction
  cannot see (the `mail_bcc` class of bug — mail routing settings are refused for exactly
  this reason),
- lets an agent change where customer money goes — payment provider enablement, bank
  details, a provider's API key. pretix's own permission for payment settings covers
  deadlines and IBANs with one flag, so `update_event_settings` refuses the money-routing
  subset itself; a way around that refusal is a finding.

Out of scope — documented behaviour, not bugs:

- **The agent being manipulated at all.** The agent is assumed untrusted and possibly
  prompt-injected; that is the premise of the design, not a finding. What matters is what it
  can *reach* — that part is in scope.
- A compromised pretix-agent-mcp host, or any process already running as the same user
  reading the token from its environment.
- The pretix instance itself, and pretix's own vulnerabilities — report those to
  `security@pretix.eu` under pretix's
  [responsible disclosure policy](https://docs.pretix.eu/trust/security/disclosure/).
- Personal data in agent context when the operator set `PII_MODE=full`, or an unapproved
  write the operator enabled with `MCP_AUTO_APPROVE`. Both are explicit, logged decisions.
- Shell or Docker-socket access on the server being equivalent to approval rights — the
  out-of-band approval assumes the server is trusted.
- **Volume abuse through a legitimately granted `write` capability** — an agent calling
  `create_voucher` (or any ungated write) in a loop. Per-call classification cannot express
  a rate limit, so granting `write` on a live event is a decision to trust the agent with
  repetition; the audit log is the control that makes it reviewable. A *single* call that
  reaches further than its tool intends is in scope.
- Anything in a third-party dependency: report it upstream.

## Security-relevant code

These carry tests, and changes to them need tests in the same PR — see
[TESTING.md](TESTING.md):

| Area | Code | Tests |
|---|---|---|
| Input validation (no arbitrary REST access) | `pretix_agent_mcp/validate.py` | `tests/test_validate.py`, `tests/test_no_arbitrary_access.py` |
| Money validation (one validator, applied by the registry before approval) | `pretix_agent_mcp/validate.py`, `registry.py` | `tests/test_money.py` |
| PII redaction | `pretix_agent_mcp/redact.py` | `tests/test_redact.py` |
| Capability gate, live-event guard | `pretix_agent_mcp/registry.py` | `tests/test_capabilities.py` |
| Approval ceremony | `pretix_agent_mcp/pending.py`, `cli.py` | `tests/test_approval.py` |
| Bearer auth, transport | `pretix_agent_mcp/server.py` | `tests/test_transport.py` |

[ARCHITECTURE.md](ARCHITECTURE.md) explains where the enforcement points are and why.

## Deployment expectations

- Terminate TLS in front of the server; do not expose plain HTTP off-host.
- Use a **restricted pretix team token** with the least permissions your enabled
  capabilities need (see the table in the [README](README.md#the-pretix-token)). Never a
  token from a team that can change teams and permissions.
- Run the server on a different machine from the agent when you can, so the agent cannot
  reach the credential through the filesystem or the environment. If you run it beside a
  self-hosted pretix, keep it in its own container — not baked into the pretix image.
- Keep `PII_MODE=redacted` unless you have decided, deliberately, that this deployment's
  agent context may hold customer data.
