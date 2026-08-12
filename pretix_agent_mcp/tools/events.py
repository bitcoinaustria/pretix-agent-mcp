"""Events: reading them, creating them, cloning them, taking them live.

The interesting part is the live boundary. Reconfiguring a draft event is friction-free;
``publish_event`` and ``delete_event`` are high-risk, and ``update_event`` /
``update_event_settings`` escalate themselves when the event is already selling
(the live-event guard in :mod:`pretix_agent_mcp.registry`).
"""

from __future__ import annotations

from typing import Any

from ..registry import App, tool
from ..validate import ValidationError, object_id, page_size, slug
from ._shared import clean, i18n, listing, pick

EVENT_SUMMARY = (
    "slug",
    "name",
    "live",
    "testmode",
    "currency",
    "date_from",
    "date_to",
    "presale_start",
    "presale_end",
    "has_subevents",
    "is_public",
)
EVENT_DETAIL = EVENT_SUMMARY + ("date_admission", "location", "timezone", "plugins", "meta_data")


@tool("read")
async def list_events(app: App, live: bool | None = None, limit: int = 50) -> dict:
    """List the events of the configured organizer.

    Optionally filter by whether an event is live (selling) or not.
    """
    params: dict[str, Any] = {}
    if live is not None:
        params["live"] = "true" if live else "false"
    cap = page_size(limit)
    events, total, truncated = await app.pretix.paginate("events", params=params, cap=cap)
    allowed = [e for e in events if app.cfg.event_allowed(e.get("slug", ""))]
    return listing([pick(e, *EVENT_SUMMARY) for e in allowed], total=total, truncated=truncated)


@tool("read")
async def get_event(app: App, event: str) -> dict:
    """Full configuration of one event: dates, presale window, live/test state, plugins."""
    return pick(await app.event(event), *EVENT_DETAIL)


@tool("read")
async def get_event_settings(app: App, event: str, keys: list[str] | None = None) -> dict:
    """Read event settings (mail texts, presale behaviour, waiting list, ...).

    The settings object is large; pass ``keys`` to fetch only the settings you need.
    """
    settings = await app.pretix.get("events", app.check_event(event), "settings")
    if not isinstance(settings, dict):
        return {"settings": settings}
    if keys:
        return {"settings": {key: i18n(settings.get(key)) for key in keys}}
    return {"settings": {key: i18n(value) for key, value in settings.items()}}


@tool("write")
async def create_event(
    app: App,
    event: str,
    name: str,
    date_from: str,
    currency: str = "EUR",
    date_to: str | None = None,
    location: str | None = None,
    timezone: str | None = None,
    presale_start: str | None = None,
    presale_end: str | None = None,
    has_subevents: bool = False,
) -> dict:
    """Create a new event from scratch, in test mode and not live.

    Prefer clone_event when a previous edition of the same event exists: cloning carries
    over products, quotas, settings and mail texts, which this tool does not.
    ``event`` is the URL slug. Dates are ISO 8601 (``2027-06-12T09:00:00+02:00``).
    """
    payload = clean(
        {
            "slug": app.check_event(event),
            "name": name,
            "live": False,
            "testmode": True,
            "currency": currency,
            "date_from": date_from,
            "date_to": date_to,
            "location": location,
            "timezone": timezone,
            "presale_start": presale_start,
            "presale_end": presale_end,
            "has_subevents": has_subevents,
        }
    )
    created = await app.pretix.post("events", json=payload)
    return {"created": pick(created, *EVENT_DETAIL), "url": _frontend_url(app, created.get("slug", event))}


@tool("write")
async def clone_event(
    app: App,
    source_event: str,
    event: str,
    name: str,
    date_from: str,
    date_to: str | None = None,
    presale_start: str | None = None,
    presale_end: str | None = None,
    location: str | None = None,
) -> dict:
    """Create a new edition of an existing event by cloning it.

    Copies products, quotas, categories, questions and settings from ``source_event``.
    The clone is created not live and in test mode; adjust it, then publish_event.
    """
    payload = clean(
        {
            "slug": app.check_event(event),
            "name": name,
            "live": False,
            "testmode": True,
            "date_from": date_from,
            "date_to": date_to,
            "presale_start": presale_start,
            "presale_end": presale_end,
            "location": location,
        }
    )
    created = await app.pretix.post("events", app.check_event(source_event), "clone", json=payload)
    return {
        "created": pick(created, *EVENT_DETAIL),
        "cloned_from": source_event,
        "url": _frontend_url(app, created.get("slug", event)),
    }


@tool("write", live_guard=True)
async def update_event(
    app: App,
    event: str,
    name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_admission: str | None = None,
    presale_start: str | None = None,
    presale_end: str | None = None,
    location: str | None = None,
    is_public: bool | None = None,
    testmode: bool | None = None,
) -> dict:
    """Change an event's name, dates, presale window, location or test mode.

    Only the fields you pass are changed. This tool cannot take an event live — that is
    publish_event, which is always approved out of band.
    """
    payload = clean(
        {
            "name": name,
            "date_from": date_from,
            "date_to": date_to,
            "date_admission": date_admission,
            "presale_start": presale_start,
            "presale_end": presale_end,
            "location": location,
            "is_public": is_public,
            "testmode": testmode,
        }
    )
    if not payload:
        raise ValidationError("nothing to update: pass at least one field")
    updated = await app.pretix.patch("events", app.check_event(event), json=payload)
    return {"updated": pick(updated, *EVENT_DETAIL), "changed": sorted(payload)}


@tool("write", live_guard=True)
async def update_event_settings(app: App, event: str, settings: dict[str, Any]) -> dict:
    """Change event settings: mail texts, confirmation texts, waiting-list behaviour, ...

    Pass a mapping of pretix setting names to values; read them first with
    get_event_settings. Settings the pretix API does not expose stay UI-only.
    """
    if not settings:
        raise ValidationError("settings must not be empty")
    updated = await app.pretix.patch("events", app.check_event(event), "settings", json=settings)
    keys = sorted(settings)
    current = updated if isinstance(updated, dict) else {}
    return {"changed": keys, "settings": {key: i18n(current.get(key)) for key in keys}}


@tool("write", live_guard=True)
async def set_event_plugins(app: App, event: str, plugins: list[str]) -> dict:
    """Set the enabled plugins of an event to exactly this list.

    Read the current list with get_event first — this replaces it rather than adding to it.
    """
    for name in plugins:
        if not isinstance(name, str) or not name:
            raise ValidationError(f"invalid plugin name: {name!r}")
    updated = await app.pretix.patch("events", app.check_event(event), json={"plugins": plugins})
    return {"plugins": updated.get("plugins", [])}


@tool("write:high-risk", preview=lambda app, kwargs: _publish_preview(app, kwargs))
async def publish_event(app: App, event: str) -> dict:
    """Take an event live: its shop opens and it can sell tickets to the public.

    Always requires out-of-band approval — this is the boundary crossing itself.
    """
    updated = await app.pretix.patch("events", app.check_event(event), json={"live": True})
    return {"published": pick(updated, *EVENT_SUMMARY), "url": _frontend_url(app, event)}


@tool("write", live_guard=True)
async def unpublish_event(app: App, event: str) -> dict:
    """Take an event off sale again (live=false). The shop closes; existing orders stay."""
    updated = await app.pretix.patch("events", app.check_event(event), json={"live": False})
    return {"unpublished": pick(updated, *EVENT_SUMMARY)}


@tool("write:high-risk", preview=lambda app, kwargs: _delete_event_preview(app, kwargs))
async def delete_event(app: App, event: str) -> dict:
    """Delete an event. pretix only allows this while the event has no orders.

    Irreversible; always requires out-of-band approval.
    """
    await app.pretix.delete("events", app.check_event(event))
    return {"deleted": event}


@tool("read")
async def list_tax_rules(app: App, event: str) -> dict:
    """Tax rules of an event, with their rates."""
    rules, total, truncated = await app.pretix.paginate("events", app.check_event(event), "taxrules", cap=100)
    return listing(
        [pick(r, "id", "name", "rate", "price_includes_tax", "internal_name") for r in rules],
        total=total,
        truncated=truncated,
    )


@tool("write", live_guard=True)
async def create_tax_rule(
    app: App,
    event: str,
    name: str,
    rate: str,
    price_includes_tax: bool = True,
) -> dict:
    """Create a tax rule, e.g. name '20% VAT' with rate '20.00'."""
    created = await app.pretix.post(
        "events",
        app.check_event(event),
        "taxrules",
        json={"name": name, "rate": rate, "price_includes_tax": price_includes_tax},
    )
    return {"created": pick(created, "id", "name", "rate", "price_includes_tax")}


@tool("write", live_guard=True)
async def update_tax_rule(
    app: App,
    event: str,
    tax_rule_id: int,
    name: str | None = None,
    rate: str | None = None,
    price_includes_tax: bool | None = None,
) -> dict:
    """Change a tax rule's name, rate or tax-inclusive flag."""
    payload = clean({"name": name, "rate": rate, "price_includes_tax": price_includes_tax})
    if not payload:
        raise ValidationError("nothing to update: pass at least one field")
    updated = await app.pretix.patch(
        "events",
        app.check_event(event),
        "taxrules",
        str(object_id(tax_rule_id, field="tax_rule_id")),
        json=payload,
    )
    return {"updated": pick(updated, "id", "name", "rate", "price_includes_tax")}


@tool("write:high-risk")
async def delete_tax_rule(app: App, event: str, tax_rule_id: int) -> dict:
    """Delete a tax rule. Fails while products still reference it."""
    await app.pretix.delete(
        "events", app.check_event(event), "taxrules", str(object_id(tax_rule_id, field="tax_rule_id"))
    )
    return {"deleted": tax_rule_id}


def _frontend_url(app: App, event_slug: str) -> str:
    return f"{app.cfg.pretix_base_url}/{app.cfg.organizer}/{slug(event_slug, field='event slug')}/"


async def _publish_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    event = await app.event(kwargs["event"])
    summary = pick(event, *EVENT_SUMMARY)
    return (
        f"Take event '{summary.get('slug')}' ({summary.get('name')}) LIVE — the public shop opens.\n"
        f"  starts:  {summary.get('date_from')}\n"
        f"  presale: {summary.get('presale_start')} → {summary.get('presale_end')}\n"
        f"  test mode: {summary.get('testmode')}",
        summary,
    )


async def _delete_event_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    event = await app.event(kwargs["event"])
    summary = pick(event, *EVENT_SUMMARY)
    orders = await app.pretix.get("events", kwargs["event"], "orders", params={"page_size": 1})
    count = orders.get("count") if isinstance(orders, dict) else None
    return (
        f"DELETE event '{summary.get('slug')}' ({summary.get('name')}), live={summary.get('live')}, "
        f"orders={count}. Irreversible.",
        summary,
    )
