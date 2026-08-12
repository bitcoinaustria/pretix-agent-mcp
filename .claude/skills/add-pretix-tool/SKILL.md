---
name: add-pretix-tool
description: Add a new pretix capability to pretix-agent-mcp as an MCP tool — the registry contract, capability classification, output shaping, and the tests it needs. Use when adding, changing or reviewing a tool in pretix_agent_mcp/tools/, or when a pretix feature is not yet exposed to agents.
---

# Adding a pretix tool

Read [tools/events.py](../../../pretix_agent_mcp/tools/events.py) first — it is the
reference implementation, and matching it is faster than reading this twice. The rules that
are not obvious from it are below.

## 1. Check what pretix actually offers

Fetch `https://docs.pretix.eu/dev/api/resources/<resource>.html` and confirm the endpoint
path, the required fields on create, and the exact field names. A wrong path is the most
common failure here, and memory is not good enough — the docs also contain a few
singular/plural typos where the DRF router registers the plural form (`quotas/{id}/`, not
`quota/{id}/`).

If pretix cannot do the thing: implement the subset that exists and add the gap to "Known
limits" in the README. Never scrape the UI, use an admin session, or call an undocumented
endpoint.

## 2. Write the function

```python
@tool("read")
async def list_quotas(app: App, event: str, limit: int = 50) -> dict:
    """First line is what the model sees when deciding whether to call this.

    Say what it does not do, and name the better tool when there is one. This docstring is
    the tool description — it is the only documentation the agent gets.
    """
    quotas, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "quotas", cap=page_size(limit)
    )
    return listing([pick(q, "id", "name", "size") for q in quotas], total=total, truncated=truncated)
```

- **First parameter is always `app: App`.** Everything after it is agent-supplied and
  becomes JSON Schema from its annotation, so annotate concretely (`str`, `int`, `bool`,
  `list[str]`, `dict[str, Any]`, `str | None = None`). A default makes it optional. No
  `*args`/`**kwargs`. Return `-> dict`.
- **Name the event-slug parameter `event`.** The registry keys the event allowlist and the
  live-event guard on that exact name; any other name silently loses both.
- **Validate everything that becomes a path segment**: `app.check_event(event)` (validates
  *and* enforces the allowlist), `object_id(value, field="quota_id")`,
  `order_code(code)`, `page_size(limit)`. Raise `ValidationError` for anything else
  malformed, before the request is built.
- **Never redact, audit, or check capabilities in a tool** — the registry does all three.
- Reuse [tools/_shared.py](../../../pretix_agent_mcp/tools/_shared.py): `i18n()` flattens
  pretix's `{"en": ...}` fields, `pick()` subsets an object, `clean()` drops `None` so a
  PATCH only touches what the agent named, `listing()` gives lists a uniform shape.
- Reject an empty update (`if not payload: raise ValidationError(...)`) rather than sending
  a no-op PATCH.
- Prices are decimal strings (`"23.00"`); accept `str`, reject floats, sum with `Decimal`.

## 3. Shape the output

Every result lands in an LLM context, and often in a third-party model log. Return the
handful of fields that answer the question — never a raw pretix object, never more than was
asked for. List tools paginate, cap server-side, and report `truncated` rather than implying
a complete answer. Personal data belongs only in the tools whose job is a person
(`get_order`, `search_attendees`, check-in lists), not in summaries.

## 4. Classify it

| Class | Use for |
|---|---|
| `read` | anything that only reads |
| `write` | ordinary changes, including on a draft event |
| `write:high-risk` | irreversible: delete, cancel, refund, publish |

Add `live_guard=True` to any `write` that can change price, availability, product structure
or existing orders — the registry then escalates it to high-risk at call time when the event
is live and not in test mode. Draft work stays friction-free; a selling event does not.

`write:high-risk` tools may take `preview=`:

```python
async def _delete_quota_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    quota = await app.pretix.get("events", app.check_event(kwargs["event"]), "quotas", ...)
    return f"DELETE quota '{quota['name']}' — {quota['available_number']} still available", quota
```

The preview runs **before** approval, so it must only read, it must validate the slug
itself, and it must mask any personal data it puts in the text (preview strings do not pass
through `redact()`). Write it for the human who will read it on a terminal, and include
whatever number makes the consequence obvious — orders sold, seats left, amount refunded.

Do not add a `testmode` parameter: `publish_event` is the only tool allowed to leave test
mode, and a test enforces that.

## 5. Test it

See [TESTING.md](../../../TESTING.md) for the harness. Per tool group: one happy path, one
rejected input, and for high-risk or live-guarded tools, that the call returns
`status == "awaiting_approval"` **and** that nothing was sent (`api.sent(...) == []`). A
live-guarded tool needs `api.route("GET", "events/conf27", DRAFT)` so the guard can look the
event up.

Then run the whole suite, not just your file — two registry-wide suites will catch a rule
you skipped:

```bash
make ci
```

## 6. Finish the paperwork

- Add the tool to the table in [README.md](../../../README.md), with `¹` if it is
  live-guarded.
- If it exposes a pretix limitation, add it to "Known limits" there.
- `make ci` clean, then commit with a subject saying what the agent can now do.
