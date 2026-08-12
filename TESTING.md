# Testing

```bash
make test     # pytest -q
make ci       # what CI runs, on Python 3.11 and 3.13
```

No test talks to a real pretix instance, and no fixture contains a real URL, token or
person. `asyncio_mode=auto`, so async tests need no decorator.

## The harness

[tests/conftest.py](tests/conftest.py) provides a fake pretix API and an `App` wired to it:

```python
async def test_list_quotas(api, call):
    api.page("GET", "events/conf27/quotas", [{"id": 1, "name": "Tickets", "size": 40}])
    result = await call("list_quotas", event="conf27")
    assert result["results"][0]["name"] == "Tickets"
```

- `api.route(method, path, payload)` — one response. Paths are organizer-relative, so
  `"events/conf27"` answers `/api/v1/organizers/demo/events/conf27/`.
- `api.page(...)` — a paginated listing. `api.route_fn(...)` — a handler, for per-attempt
  behaviour like a 429 then a 200.
- `api.sent(method, path)` — the JSON bodies your code sent, in order. **Assert on this for
  every write**, and assert `== []` to prove something was *not* sent.
- `call(tool_name, **kwargs)` runs a tool the way the server does — through the registry
  gate, so capability checks, the live guard and redaction all apply. It is positional-only
  in its first argument, so a tool's own `name=` parameter can be passed as a keyword.
- `make_app(**env)` builds an `App` with config overrides (`MCP_CAPABILITIES="read"`,
  `PRETIX_EVENT_ALLOWLIST="conf27"`, …). The default `app` fixture has all capabilities on.
- The fake transport asserts every request stays under the configured organizer's path, so
  a path-escape bug fails the test that provoked it, wherever it is.

## What a change must bring with it

| If you touch | Add or extend |
|---|---|
| `validate.py` | [test_validate.py](tests/test_validate.py) — including what is now *rejected* |
| `redact.py` | [test_redact.py](tests/test_redact.py) — a leak test asserting the raw value is absent from `repr()` of the whole result |
| anything that carries an amount | [test_money.py](tests/test_money.py) — add the new call site there, not a local price test |
| `registry.py`, capability classes | [test_capabilities.py](tests/test_capabilities.py) |
| the approval flow, `pending.py`, `cli.py` | [test_approval.py](tests/test_approval.py) |
| `server.py`, auth, transport | [test_transport.py](tests/test_transport.py) — speaks the real wire protocol against the real ASGI app |
| any new tool | `tests/test_<domain>.py` — one happy path, one rejected input, and for high-risk or live-guarded tools: `status == "awaiting_approval"` **and** `api.sent(...) == []` |

Two suites run across the whole registry and will catch a tool that skipped a rule, so run
the full suite rather than just your file:

- [test_no_arbitrary_access.py](tests/test_no_arbitrary_access.py) feeds path-escape
  payloads into every string parameter of all 64 tools.
- [test_scenario.py](tests/test_scenario.py) plays the north-star season end to end and
  asserts it needs exactly two approvals.

Keep tests proportionate: the smallest thing that fails if the logic breaks. No fixtures
factories, no per-function suites.
