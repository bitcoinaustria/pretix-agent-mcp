# Local end-to-end test against a real pretix

`make ci` runs 559 tests against a fake pretix. It cannot tell you whether an endpoint path
or a field name matches a real instance — that is what this directory is for. Everything
here is throwaway: a SQLite pretix in Docker, an organizer called `demo`, invented people.

The `pretix/standalone` image is amd64. On Apple silicon it runs emulated: expect the first
boot to take a few minutes and give Docker ~8 GB, or gunicorn workers get OOM-killed.

## 1. Start pretix

```bash
docker network create pretix-test
docker run -d --name pretix-redis --network pretix-test redis:7-alpine
docker run -d --name pretix-local --network pretix-test -p 127.0.0.1:8345:80 --memory 10g \
  -v pretix-local-data:/data -v "$PWD/localtest/pretix.cfg:/etc/pretix/pretix.cfg:ro" \
  pretix/standalone:stable web
```

Ready when this answers `401` (it wants a token — that is the point):

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8345/api/v1/organizers/
```

`web` only, not `all`: without a Celery worker nothing in the tool surface breaks, and the
worker doubles the memory an emulated instance needs.

## 2. Bootstrap an organizer and a restricted token

The one step that cannot go through this server. The team gets least privilege for the tool
surface, so a permission the server actually needs surfaces as a pretix 403 instead of being
masked by an all-permissions team.

```bash
docker exec -i pretix-local pretix shell -c "$(cat localtest/bootstrap.py)"
```

Copy the printed `PRETIX_TOKEN=…`.

## 3. Run the server against it

```bash
export PRETIX_BASE_URL=http://localhost:8345 PRETIX_ORGANIZER=demo
export PRETIX_API_TOKEN=<the token from step 2>
export MCP_BEARER_TOKEN=$(openssl rand -hex 24)
export MCP_CAPABILITIES=read,write,write:high-risk
export AUDIT_LOG=/tmp/pretix-agent-audit.jsonl STATE_DB=/tmp/pretix-agent-pending.sqlite3
uv run pretix-agent-mcp serve --transport http
```

## 4. Drive it over the real wire

In another shell, with the same environment exported:

```bash
uv run python localtest/drive.py "$MCP_BEARER_TOKEN"       # 35 checks: boundary → catalog → ceremony
uv run python localtest/drive_live.py "$MCP_BEARER_TOKEN"  # 20 checks: publish + live-guard, free event
uv run python localtest/drive_paid.py "$MCP_BEARER_TOKEN"  # 18 checks: the whole paid lifecycle
uv run python localtest/drive_pii.py "$MCP_BEARER_TOKEN"   # 21 checks: redaction on a real order
```

Each speaks JSON-RPC over streamable HTTP directly, so every header including the bearer
token is explicit. Each run creates a fresh event slug; nothing is cleaned up, because
looking at the leftovers in the pretix UI is often the point.

`drive_paid.py` is the north star as a test: the agent creates a paid event, prices it,
opens it for sale and changes a price on the selling event, with two `approve` commands as
the only human steps and no visit to the web UI. It works because `bootstrap.py` sets the
payment provider up once at *organizer* level, which every event inherits — that is the real
deployment shape, and pretix will not take a paid event live without it.

All three expect `bootstrap.py` to have run: without the organizer-level payment provider,
every publish of a paid event fails with pretix's own "no payment provider enabled" — real
behaviour, not a bug, and now flagged in `publish_event`'s preview before an approval is
spent on it.

## 5. Tear down

```bash
docker rm -f pretix-local pretix-redis
docker volume rm pretix-local-data
docker network rm pretix-test
```

## What this caught that the unit tests could not

- `update_event_settings` accepting **payment** configuration. An agent could rewrite a
  destination IBAN or a provider's API key and send every customer payment somewhere else —
  the `mail_bcc` bug with money. Refused outright now, and the guard is keyed on the shape of
  the setting name, so a third-party provider (BTCPay, Stripe) needs no special casing.
- `publish_event` failing *after* a human approved it, because pretix requires a payment
  provider for paid products. The preview reports the go-live facts now.
- The first version of that preview then claimed "payment providers enabled: none" on a
  correctly configured event, because pretix's settings API exposes core settings only and
  every real provider is invisible there. A confident false negative on the one line meant
  to stop the operator is worse than silence: the claim is gone.
- pretix 2026.x replacing `can_*` team booleans with `group:action` permissions, which made
  the README's token instructions stale.
- pretix classifying `payment_term_days` under `event.settings.payment:write` — one coarse
  flag covering deadlines and IBANs, which is precisely why the finer refusal above matters.
- Payment providers being inherited from organizer level, while the *plugin* has to be
  active per event — so a from-scratch paid event needs one `set_event_plugins` call the
  agent can make itself.
