# Security policy

## Reporting a vulnerability

Email **security@bitcoin-austria.at** with a description, affected version or commit,
and a reproduction if you have one. Please do not open a public issue for anything
exploitable. We aim to acknowledge within 72 hours.

If you prefer GitHub, use a
[private security advisory](https://github.com/bitcoinaustria/pretix-agent-mcp/security/advisories/new).

## What this project defends against

In priority order:

1. **A prompt-injected agent** misusing legitimate tool access — exfiltrating personal
   data, or issuing destructive writes. Mitigations: PII masked by default, a capability
   allowlist that decides which tools exist at all, and out-of-band human approval for
   irreversible writes (a `confirm: true` parameter would be set by the attacker).
2. **An unauthorized network client** reaching the MCP endpoint. Mitigations: bearer
   token required on the HTTP transport, `127.0.0.1` bind by default, TLS terminated by
   a reverse proxy as the documented deployment.
3. **Credential theft.** The pretix API token exists only in the server's environment.
   It is never returned through MCP, never written to logs or audit records, never
   accepted as a tool parameter, and never needed on the machine running the agent.

Explicit non-goals: protecting against a compromised pretix-agent-mcp host, and
protecting against the pretix instance itself.

## Security-relevant code

These carry tests, and changes to them need tests:

| Area | Code | Tests |
|---|---|---|
| Input validation (no arbitrary REST access) | `pretix_agent_mcp/validate.py` | `tests/test_validate.py` |
| PII redaction | `pretix_agent_mcp/redact.py` | `tests/test_redact.py` |
| Capability gate, live-event guard | `pretix_agent_mcp/registry.py` | `tests/test_capabilities.py` |
| Approval ceremony | `pretix_agent_mcp/pending.py`, `cli.py` | `tests/test_approval.py` |
| Bearer auth, transport | `pretix_agent_mcp/server.py` | `tests/test_transport.py` |

## Deployment expectations

- Terminate TLS in front of the server; do not expose plain HTTP off-host.
- Use a **restricted pretix team token** with the least permissions your enabled
  capabilities need. Never a token from a team with "can change teams and permissions".
- Run the server on a different machine from the agent when you can, so the agent cannot
  reach the credential through the filesystem or the environment.
- Keep `PII_MODE=redacted` unless you have decided, deliberately, that this deployment's
  agent context may hold customer data.
