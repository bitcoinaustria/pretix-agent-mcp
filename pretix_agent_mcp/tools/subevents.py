"""Sub-events: the individual dates of an event series (``has_subevents=true``).

The tool that earns its keep here is :func:`create_subevents`: one call turns a list of
dates the agent already computed into a list of series dates, optionally with one quota
each. It deliberately contains no recurrence logic and no date parsing beyond
:func:`datetime.datetime.fromisoformat` — working out "every first Thursday until
December" is the agent's job, not this server's.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..pretix import PretixError
from ..registry import App, tool
from ..validate import ValidationError, object_id, page_size
from ._shared import clean, listing, pick

SUBEVENT_SUMMARY = (
    "id",
    "name",
    "date_from",
    "date_to",
    "active",
    "is_public",
    "presale_start",
    "presale_end",
    "best_availability_state",  # only present with with_availability=True
)
SUBEVENT_DETAIL = SUBEVENT_SUMMARY + (
    "date_admission",
    "location",
    "item_price_overrides",
    "variation_price_overrides",
    "meta_data",
)
# One HTTP round trip per date (pretix has no bulk subevent endpoint), so cap the fan-out.
MAX_BATCH = 100


@tool("read")
async def list_subevents(
    app: App,
    event: str,
    active: bool | None = None,
    since: str | None = None,
    until: str | None = None,
    with_availability: bool = False,
    limit: int = 50,
) -> dict:
    """List the dates of an event series, optionally filtered by active state and a date window.

    ``since``/``until`` are ISO 8601 datetimes filtering on the date the series date starts.
    ``with_availability`` adds ``best_availability_state`` per date (100 = available, below
    that = sold out or reserved); pretix documents it as slow, so it is off by default —
    use list_quotas for real numbers.
    """
    params: dict[str, Any] = {}
    if active is not None:
        params["active"] = "true" if active else "false"
    if since is not None:
        params["date_from_after"] = _iso(since, "since")
    if until is not None:
        params["date_from_before"] = _iso(until, "until")
    if with_availability:
        params["with_availability_for"] = "web"
    subevents, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "subevents", params=params, cap=page_size(limit)
    )
    return listing(
        [pick(s, *SUBEVENT_SUMMARY) for s in subevents],
        total=total,
        truncated=truncated,
    )


@tool("read")
async def get_subevent(app: App, event: str, subevent_id: int) -> dict:
    """Full configuration of one series date, including its per-item price overrides."""
    subevent = await app.pretix.get(
        "events", app.check_event(event), "subevents", str(object_id(subevent_id, field="subevent_id"))
    )
    return pick(subevent, *SUBEVENT_DETAIL)


@tool("write", live_guard=True)
async def create_subevents(
    app: App,
    event: str,
    dates: list[str],
    name: str,
    duration_minutes: int | None = None,
    location: str | None = None,
    active: bool = True,
    is_public: bool = True,
    presale_start: str | None = None,
    presale_end: str | None = None,
    item_prices: dict[str, str] | None = None,
    quota_size: int | None = None,
    quota_items: list[int] | None = None,
    quota_name: str | None = None,
) -> dict:
    """Create many dates of an event series at once — one date per entry in ``dates``.

    ``dates`` are the start datetimes in ISO 8601 (``2027-02-04T19:00:00+01:00``); compute
    the recurrence yourself and pass the resulting list, this tool does not understand
    "every first Thursday". All other arguments describe the shape shared by every date:
    ``duration_minutes`` sets each ``date_to``, ``item_prices`` maps a product id to the
    price for these dates (omit it to sell every product at its normal price), and
    ``quota_size`` creates one quota of that size per date over ``quota_items``.

    Dates are validated up front, then created one by one. If pretix rejects one, the
    response lists the ids created before it and stops — nothing is rolled back.
    """
    if not dates:
        raise ValidationError("dates must not be empty")
    if len(dates) > MAX_BATCH:
        raise ValidationError(f"too many dates: {len(dates)} (max {MAX_BATCH} per call)")
    starts = [_iso(value, f"dates[{index}]") for index, value in enumerate(dates)]
    if quota_size is not None:
        quota_size = object_id(quota_size, field="quota_size")
        if not quota_items:
            raise ValidationError("quota_items is required when quota_size is given")
    items = [object_id(item, field="quota_items") for item in quota_items or []]
    overrides = [
        {"item": object_id(item, field="item_prices key"), "price": str(price)}
        for item, price in (item_prices or {}).items()
    ]

    slug = app.check_event(event)
    created: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        payload = clean(
            {
                "name": name,
                "date_from": start,
                "date_to": _shift(start, duration_minutes) if duration_minutes else None,
                "active": active,
                "is_public": is_public,
                "location": location,
                "presale_start": presale_start,
                "presale_end": presale_end,
                # Sent explicitly (even when empty) because pretix's create example includes them.
                "item_price_overrides": overrides,
                "variation_price_overrides": [],
                "meta_data": {},
            }
        )
        try:
            subevent = await app.pretix.post("events", slug, "subevents", json=payload)
            # Appended before the quota call so a failing quota still surfaces the new date's id.
            entry = pick(subevent, "id", "name", "date_from", "date_to", "active")
            created.append(entry)
            if quota_size is not None:
                quota = await app.pretix.post(
                    "events",
                    slug,
                    "quotas",
                    json={
                        "name": quota_name or name,
                        "size": quota_size,
                        "items": items,
                        "subevent": subevent.get("id"),
                    },
                )
                entry["quota_id"] = quota.get("id")
        except PretixError as exc:
            return {
                "status": "partial_failure",
                "created": created,
                "created_count": len(created),
                "failed_at": start,
                "error": str(exc),
                "remaining": starts[index + 1 :],
                "note": (
                    "Nothing was rolled back. An entry in 'created' without a quota_id got the date "
                    "but not its quota. Fix the error, then re-run for the remaining dates only."
                ),
            }
    return {"status": "ok", "created": created, "created_count": len(created)}


@tool("write", live_guard=True)
async def update_subevent(
    app: App,
    event: str,
    subevent_id: int,
    name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_admission: str | None = None,
    active: bool | None = None,
    is_public: bool | None = None,
    presale_start: str | None = None,
    presale_end: str | None = None,
    location: str | None = None,
    item_prices: dict[str, str] | None = None,
) -> dict:
    """Change one series date: name, dates, active/public state, presale window, item prices.

    Only the fields you pass are changed, except ``item_prices``, which replaces the whole
    override list for this date (read it first with get_subevent).
    """
    payload = clean(
        {
            "name": name,
            "date_from": _iso(date_from, "date_from") if date_from else None,
            "date_to": _iso(date_to, "date_to") if date_to else None,
            "date_admission": _iso(date_admission, "date_admission") if date_admission else None,
            "active": active,
            "is_public": is_public,
            "presale_start": presale_start,
            "presale_end": presale_end,
            "location": location,
        }
    )
    if item_prices is not None:
        payload["item_price_overrides"] = [
            {"item": object_id(item, field="item_prices key"), "price": str(price)}
            for item, price in item_prices.items()
        ]
    if not payload:
        raise ValidationError("nothing to update: pass at least one field")
    updated = await app.pretix.patch(
        "events",
        app.check_event(event),
        "subevents",
        str(object_id(subevent_id, field="subevent_id")),
        json=payload,
    )
    return {"updated": pick(updated, *SUBEVENT_DETAIL), "changed": sorted(payload)}


async def _delete_subevent_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    slug = app.check_event(kwargs["event"])
    subevent_id = object_id(kwargs.get("subevent_id"), field="subevent_id")
    subevent = await app.pretix.get("events", slug, "subevents", str(subevent_id))
    summary = pick(subevent, *SUBEVENT_SUMMARY)
    positions = await _count(app, slug, "orderpositions", subevent_id)
    orders = await _count(app, slug, "orders", subevent_id)
    return (
        f"DELETE series date {subevent_id} '{summary.get('name')}' on {summary.get('date_from')} "
        f"(active={summary.get('active')}).\n"
        f"  order positions referencing it: {positions}\n"
        f"  orders containing it:           {orders}\n"
        "Irreversible. pretix refuses the delete while tickets have been sold for this date.",
        summary | {"order_positions": positions, "orders": orders},
    )


@tool("write:high-risk", preview=_delete_subevent_preview)
async def delete_subevent(app: App, event: str, subevent_id: int) -> dict:
    """Delete one date of an event series. pretix refuses while tickets were sold for it.

    Irreversible. To take a date off sale without deleting it, use update_subevent with
    active=false instead.
    """
    await app.pretix.delete(
        "events", app.check_event(event), "subevents", str(object_id(subevent_id, field="subevent_id"))
    )
    return {"deleted": subevent_id}


async def _count(app: App, slug: str, resource: str, subevent_id: int) -> int | None:
    """How many objects of ``resource`` reference this date, via pretix's paginated count."""
    payload = await app.pretix.get("events", slug, resource, params={"subevent": subevent_id, "page_size": 1})
    return payload.get("count") if isinstance(payload, dict) else None


def _iso(value: object, field: str) -> str:
    """Accept an ISO 8601 datetime as a string. No natural-language dates, by design."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be an ISO 8601 datetime string")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{field} is not an ISO 8601 datetime: {value!r}") from None
    return value


def _shift(start: str, minutes: int) -> str:
    delta = timedelta(minutes=object_id(minutes, field="duration_minutes"))
    return (datetime.fromisoformat(start) + delta).isoformat()
