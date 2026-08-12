"""Check-in lists and the waiting list.

Two read-heavy corners of pretix that an agent is genuinely useful for: "who has
arrived?" and "who is waiting for a ticket?". Deliberately absent is a redeem tool —
scanning a ticket asserts that a person is physically standing in front of a scanner,
which an agent cannot know. Use the pretix check-in app for that.

The listing tools return attendee data unmasked; the registry redacts on the way out.
"""

from __future__ import annotations

from typing import Any

from ..registry import App, tool
from ..validate import ValidationError, object_id, page_size
from ._shared import clean, listing, pick

CHECKIN_LIST = (
    "id",
    "name",
    "all_products",
    "limit_products",
    "subevent",
    "include_pending",
    "position_count",
    "checkin_count",
)


@tool("read")
async def list_checkin_lists(app: App, event: str, limit: int = 50) -> dict:
    """The check-in lists of an event: which products they cover and how many have arrived.

    ``position_count`` / ``checkin_count`` are the totals pretix reports per list. Use
    list_checkins for the individual attendees on one list.
    """
    lists, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "checkinlists", cap=page_size(limit)
    )
    return listing([pick(c, *CHECKIN_LIST) for c in lists], total=total, truncated=truncated)


@tool("write")
async def create_checkin_list(
    app: App,
    event: str,
    name: str,
    all_products: bool = True,
    limit_products: list[int] | None = None,
    subevent: int | None = None,
    include_pending: bool = False,
) -> dict:
    """Create a check-in list, e.g. one door per product group or per sub-event.

    Either leave ``all_products`` true, or set it false and pass ``limit_products`` with
    the item ids the list admits (list_products gives you those). ``include_pending``
    lets unpaid-but-pending orders through the door.
    """
    payload = clean(
        {
            "name": name,
            "all_products": all_products,
            "limit_products": _item_ids(limit_products),
            "subevent": object_id(subevent, field="subevent") if subevent is not None else None,
            "include_pending": include_pending,
        }
    )
    if not all_products and not payload.get("limit_products"):
        raise ValidationError("with all_products=false you must pass limit_products")
    created = await app.pretix.post("events", app.check_event(event), "checkinlists", json=payload)
    return {"created": pick(created, *CHECKIN_LIST)}


@tool("write")
async def update_checkin_list(
    app: App,
    event: str,
    checkin_list_id: int,
    name: str | None = None,
    all_products: bool | None = None,
    limit_products: list[int] | None = None,
    subevent: int | None = None,
    include_pending: bool | None = None,
) -> dict:
    """Change a check-in list's name, product scope, sub-event or pending-order handling.

    Only the fields you pass are changed. ``limit_products`` replaces the whole list of
    admitted items rather than adding to it.
    """
    payload = clean(
        {
            "name": name,
            "all_products": all_products,
            "limit_products": _item_ids(limit_products),
            "subevent": object_id(subevent, field="subevent") if subevent is not None else None,
            "include_pending": include_pending,
        }
    )
    if not payload:
        raise ValidationError("nothing to update: pass at least one field")
    updated = await app.pretix.patch(
        "events", app.check_event(event), "checkinlists", _list_id(checkin_list_id), json=payload
    )
    return {"updated": pick(updated, *CHECKIN_LIST), "changed": sorted(payload)}


@tool("write:high-risk", preview=lambda app, kwargs: _delete_list_preview(app, kwargs))
async def delete_checkin_list(app: App, event: str, checkin_list_id: int) -> dict:
    """Delete a check-in list. Its recorded check-ins are deleted with it.

    Irreversible — the arrival record for this door is gone. Always needs approval.
    """
    await app.pretix.delete("events", app.check_event(event), "checkinlists", _list_id(checkin_list_id))
    return {"deleted": checkin_list_id}


@tool("read")
async def list_checkins(
    app: App,
    event: str,
    checkin_list_id: int,
    checked_in: bool | None = None,
    search: str | None = None,
    item: int | None = None,
    limit: int = 50,
) -> dict:
    """Attendees on one check-in list, with whether and when they were checked in.

    ``checked_in=True`` gives the people who have arrived, ``False`` the no-shows so far.
    ``search`` is pretix's fuzzy match on attendee name and order code. For the totals
    only, list_checkin_lists is one request instead of a scan.
    """
    params: dict[str, Any] = {}
    if checked_in is not None:
        params["has_checkin"] = "true" if checked_in else "false"
    if search:
        params["search"] = search
    if item is not None:
        params["item"] = object_id(item, field="item")
    positions, total, truncated = await app.pretix.paginate(
        "events",
        app.check_event(event),
        "checkinlists",
        _list_id(checkin_list_id),
        "positions",
        params=params,
        cap=page_size(limit),
    )
    return listing([_attendee(p) for p in positions], total=total, truncated=truncated)


@tool("read")
async def list_waiting_list(
    app: App,
    event: str,
    has_voucher: bool | None = None,
    item: int | None = None,
    subevent: int | None = None,
    limit: int = 50,
) -> dict:
    """People waiting for a sold-out product, oldest first by ``created``.

    ``has_voucher=False`` is the queue that still needs serving; ``voucher_sent`` marks
    the entries that already got one. Serve one with send_waiting_list_voucher.
    """
    params: dict[str, Any] = {"ordering": "created"}
    if has_voucher is not None:
        params["has_voucher"] = "true" if has_voucher else "false"
    if item is not None:
        params["item"] = object_id(item, field="item")
    if subevent is not None:
        params["subevent"] = object_id(subevent, field="subevent")
    entries, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "waitinglistentries", params=params, cap=page_size(limit)
    )
    return listing([_waiting_entry(e) for e in entries], total=total, truncated=truncated)


@tool("write")
async def send_waiting_list_voucher(app: App, event: str, entry_id: int) -> dict:
    """Assign a voucher to one waiting-list entry AND EMAIL IT TO THAT PERSON immediately.

    This sends mail to a customer and cannot be recalled once sent. It fails if the entry
    already has a voucher or the product is still unavailable. Get ``entry_id`` from
    list_waiting_list, and check you have the right person before calling.
    """
    await app.pretix.post(
        "events",
        app.check_event(event),
        "waitinglistentries",
        str(object_id(entry_id, field="entry_id")),
        "send_voucher",
    )
    return {"voucher_sent": True, "entry_id": entry_id}


def _list_id(value: object) -> str:
    return str(object_id(value, field="checkin_list_id"))


def _item_ids(values: list[int] | None) -> list[int] | None:
    if values is None:
        return None
    return [object_id(v, field="limit_products item id") for v in values]


def _waiting_entry(entry: dict[str, Any]) -> dict[str, Any]:
    fields = pick(entry, "id", "created", "email", "item", "variation", "subevent")
    return fields | {"voucher_sent": bool(entry.get("voucher"))}


def _attendee(position: dict[str, Any]) -> dict[str, Any]:
    checkins = position.get("checkins") or []
    last = max(checkins, key=lambda c: c.get("datetime") or "") if checkins else None
    return pick(position, "id", "order", "positionid", "item", "variation", "attendee_name") | {
        "checked_in": bool(checkins),
        "checkin_time": last.get("datetime") if last else None,
        "checkin_type": last.get("type") if last else None,
    }


async def _delete_list_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    list_id = _list_id(kwargs["checkin_list_id"])
    summary = pick(await app.pretix.get("events", kwargs["event"], "checkinlists", list_id), *CHECKIN_LIST)
    return (
        f"DELETE check-in list {summary.get('id')} ('{summary.get('name')}') of event "
        f"{kwargs['event']} — {summary.get('checkin_count')} of {summary.get('position_count')} "
        f"recorded check-ins are deleted with it. Irreversible.",
        summary,
    )
