"""The product catalog of one event: products, variations, categories, quotas, questions.

Everything here decides *what* can be bought, *at what price* and *how many are left*, so
every write carries ``live_guard=True``: changing a price or a quota size on a live event
is escalated to out-of-band approval by the registry.

Prices are decimal strings in pretix (``"23.00"``). Floats are rejected rather than
rounded — a float price is a rounding bug waiting to be charged to a customer.

``get_availability`` is the tool that answers "is it sold out": pretix tracks availability
on quotas, not on products, so a product is sold out when every quota covering it is.
"""

from __future__ import annotations

from typing import Any

from ..registry import App, tool
from ..validate import ValidationError, object_id, page_size
from ._shared import clean, i18n, listing, pick

# https://docs.pretix.eu/dev/api/resources/questions.html — answer type codes.
QUESTION_TYPES = ("N", "S", "T", "B", "C", "M", "F", "D", "H", "W", "CC", "TEL")
CHOICE_TYPES = ("C", "M")

ITEM_SUMMARY = (
    "id",
    "name",
    "internal_name",
    "default_price",
    "active",
    "category",
    "tax_rule",
    "admission",
    "position",
)
ITEM_DETAIL = ITEM_SUMMARY + (
    "description",
    "free_price",
    "available_from",
    "available_until",
    "hide_without_voucher",
    "require_voucher",
    "min_per_order",
    "max_per_order",
    "personalized",
    "generate_tickets",
    "show_quota_left",
)
VARIATION_SUMMARY = ("id", "value", "default_price", "price", "active")
VARIATION_DETAIL = VARIATION_SUMMARY + (
    "description",
    "position",
    "available_from",
    "available_until",
    "hide_without_voucher",
)
ADDON_FIELDS = ("addon_category", "min_count", "max_count", "position", "multi_allowed", "price_included")

QUOTA_FIELDS = ("id", "name", "size", "items", "variations", "subevent", "closed", "close_when_sold_out")
# Present only when the quota was fetched with_availability=true.
AVAILABILITY_FIELDS = (
    "available",
    "available_number",
    "total_size",
    "paid_orders",
    "pending_orders",
    "exited_orders",
    "cart_positions",
    "blocking_vouchers",
    "waiting_list",
)

CATEGORY_FIELDS = ("id", "name", "internal_name", "description", "position", "is_addon")
QUESTION_FIELDS = (
    "id",
    "question",
    "type",
    "required",
    "position",
    "identifier",
    "items",
    "help_text",
    "ask_during_checkin",
    "hidden",
    "dependency_question",
    "dependency_values",
)


# --------------------------------------------------------------------------- read


@tool("read")
async def list_products(app: App, event: str, active: bool | None = None, limit: int = 50) -> dict:
    """List the products (pretix "items") of an event with prices, variations and category.

    Prices are decimal strings in the event currency. Availability is *not* here — pretix
    counts stock on quotas, so use get_availability to find out whether something is sold out.
    """
    params: dict[str, Any] = {}
    if active is not None:
        params["active"] = "true" if active else "false"
    items, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "items", params=params, cap=page_size(limit)
    )
    return listing([_item(i, VARIATION_SUMMARY) for i in items], total=total, truncated=truncated)


@tool("read")
async def get_product(app: App, event: str, item_id: int) -> dict:
    """One product in full: pricing, sale window, its variations and its add-on slots.

    ``addons`` lists which categories may be added on to this product, not the individual
    add-on products — list those with list_products filtered by that category.
    """
    item = await app.pretix.get(
        "events", app.check_event(event), "items", str(object_id(item_id, field="item_id"))
    )
    detail = _item(item, VARIATION_DETAIL, *ITEM_DETAIL)
    detail["addons"] = [pick(a, *ADDON_FIELDS) for a in item.get("addons") or []]
    return detail


@tool("read")
async def list_quotas(app: App, event: str, subevent: int | None = None, limit: int = 50) -> dict:
    """List the quotas of an event: name, size and which products/variations they cover.

    Sizes only, no live numbers — get_availability returns the same quotas with how many
    tickets are actually still available.
    """
    quotas, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "quotas", params=_subevent_filter(subevent), cap=page_size(limit)
    )
    return listing([pick(q, *QUOTA_FIELDS) for q in quotas], total=total, truncated=truncated)


@tool("read")
async def get_availability(
    app: App,
    event: str,
    quota_id: int | None = None,
    subevent: int | None = None,
    limit: int = 50,
) -> dict:
    """How many tickets are still available — use this to answer "is it sold out".

    Per quota: ``available_number`` left of ``total_size``, and where the rest went
    (``paid_orders``, ``pending_orders``, ``cart_positions`` = reserved in carts,
    ``blocking_vouchers``, ``waiting_list``). ``available_number`` is null for an unlimited
    quota. Without ``quota_id`` every quota of the event is returned plus a ``sold_out``
    list of the quota names that have nothing left.
    """
    event_slug = app.check_event(event)
    params: dict[str, Any] = {"with_availability": "true"}
    if quota_id is not None:
        quota = await app.pretix.get(
            "events", event_slug, "quotas", str(object_id(quota_id, field="quota_id")), params=params
        )
        return {"quota": pick(quota, *QUOTA_FIELDS, *AVAILABILITY_FIELDS)}
    params.update(_subevent_filter(subevent))
    quotas, total, truncated = await app.pretix.paginate(
        "events", event_slug, "quotas", params=params, cap=page_size(limit)
    )
    rows = [pick(q, *QUOTA_FIELDS, *AVAILABILITY_FIELDS) for q in quotas]
    return listing(
        rows,
        total=total,
        truncated=truncated,
        sold_out=[r.get("name") for r in rows if r.get("available") is False],
    )


@tool("read")
async def list_categories(app: App, event: str, limit: int = 50) -> dict:
    """List the product categories of an event. ``is_addon`` marks add-on-only categories."""
    cats, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "categories", cap=page_size(limit)
    )
    return listing([pick(c, *CATEGORY_FIELDS) for c in cats], total=total, truncated=truncated)


@tool("read")
async def list_questions(app: App, event: str, limit: int = 50) -> dict:
    """List the attendee questions (order form fields) of an event.

    ``type`` is the pretix answer-type code (N number, S line, T text, B boolean, C single
    choice, M multiple choice, F file, D date, H time, W datetime, CC country, TEL phone).
    ``options`` holds the choices of C/M questions. ``items`` is which products it is asked for.
    """
    questions, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "questions", cap=page_size(limit)
    )
    return listing([_question(q) for q in questions], total=total, truncated=truncated)


# -------------------------------------------------------------------------- write


@tool("write", live_guard=True, money=("default_price",))
async def create_product(
    app: App,
    event: str,
    name: str,
    default_price: str = "0.00",
    category: int | None = None,
    tax_rule: int | None = None,
    active: bool = True,
    admission: bool = False,
    description: str | None = None,
    position: int | None = None,
    available_from: str | None = None,
    available_until: str | None = None,
    hide_without_voucher: bool = False,
) -> dict:
    """Create a product. ``default_price`` is a decimal string like "23.00", never a number.

    A new product sells nothing until a quota covers it — follow up with create_quota.
    Variations are added afterwards with create_product_variation, one call each.
    ``category`` and ``tax_rule`` are numeric ids from list_categories / list_tax_rules.
    ``available_from``/``available_until`` are ISO 8601 timestamps.
    """
    payload = clean(
        {
            "name": name,
            "default_price": default_price,
            "category": _opt_id(category, "category"),
            "tax_rule": _opt_id(tax_rule, "tax_rule"),
            "active": active,
            "admission": admission,
            "description": description,
            "position": position,
            "available_from": available_from,
            "available_until": available_until,
            "hide_without_voucher": hide_without_voucher,
        }
    )
    created = await app.pretix.post("events", app.check_event(event), "items", json=payload)
    return {"created": _item(created, VARIATION_SUMMARY, *ITEM_DETAIL)}


@tool("write", live_guard=True, money=("default_price",))
async def update_product(
    app: App,
    event: str,
    item_id: int,
    name: str | None = None,
    default_price: str | None = None,
    category: int | None = None,
    tax_rule: int | None = None,
    active: bool | None = None,
    admission: bool | None = None,
    description: str | None = None,
    position: int | None = None,
    available_from: str | None = None,
    available_until: str | None = None,
    hide_without_voucher: bool | None = None,
) -> dict:
    """Change a product: price, name, category, tax rule, sale window, active flag, order.

    Only the fields you pass are changed. Variations are not editable here — use
    update_product_variation, which also carries the per-variation price.
    """
    payload = clean(
        {
            "name": name,
            "default_price": default_price,
            "category": _opt_id(category, "category"),
            "tax_rule": _opt_id(tax_rule, "tax_rule"),
            "active": active,
            "admission": admission,
            "description": description,
            "position": position,
            "available_from": available_from,
            "available_until": available_until,
            "hide_without_voucher": hide_without_voucher,
        }
    )
    _require(payload)
    updated = await app.pretix.patch(
        "events", app.check_event(event), "items", str(object_id(item_id, field="item_id")), json=payload
    )
    return {"updated": _item(updated, VARIATION_SUMMARY, *ITEM_DETAIL), "changed": sorted(payload)}


@tool("write", live_guard=True, money=("default_price",))
async def create_product_variation(
    app: App,
    event: str,
    item_id: int,
    value: str,
    default_price: str | None = None,
    active: bool = True,
    description: str | None = None,
    position: int | None = None,
    available_from: str | None = None,
    available_until: str | None = None,
    hide_without_voucher: bool = False,
) -> dict:
    """Add a variation (e.g. "Student", "Size M") to an existing product.

    ``value`` is the variation's label. Omit ``default_price`` to inherit the product price;
    otherwise a decimal string like "18.00". A variation needs its own quota coverage to sell.
    """
    payload = clean(
        {
            "value": value,
            "default_price": default_price,
            "active": active,
            "description": description,
            "position": position,
            "available_from": available_from,
            "available_until": available_until,
            "hide_without_voucher": hide_without_voucher,
        }
    )
    created = await app.pretix.post(*_variations_path(app, event, item_id), json=payload)
    return {"created": pick(created, *VARIATION_DETAIL)}


@tool("write", live_guard=True, money=("default_price",))
async def update_product_variation(
    app: App,
    event: str,
    item_id: int,
    variation_id: int,
    value: str | None = None,
    default_price: str | None = None,
    active: bool | None = None,
    description: str | None = None,
    position: int | None = None,
    available_from: str | None = None,
    available_until: str | None = None,
    hide_without_voucher: bool | None = None,
) -> dict:
    """Change one variation of a product: its label, its own price, active flag, sale window.

    Only the fields you pass are changed.
    """
    payload = clean(
        {
            "value": value,
            "default_price": default_price,
            "active": active,
            "description": description,
            "position": position,
            "available_from": available_from,
            "available_until": available_until,
            "hide_without_voucher": hide_without_voucher,
        }
    )
    _require(payload)
    updated = await app.pretix.patch(
        *_variations_path(app, event, item_id),
        str(object_id(variation_id, field="variation_id")),
        json=payload,
    )
    return {"updated": pick(updated, *VARIATION_DETAIL), "changed": sorted(payload)}


@tool("write", live_guard=True)
async def create_category(
    app: App,
    event: str,
    name: str,
    internal_name: str | None = None,
    description: str | None = None,
    position: int | None = None,
    is_addon: bool = False,
) -> dict:
    """Create a product category. ``is_addon=true`` makes it an add-on-only category."""
    payload = clean(
        {
            "name": name,
            "internal_name": internal_name,
            "description": description,
            "position": position,
            "is_addon": is_addon,
        }
    )
    created = await app.pretix.post("events", app.check_event(event), "categories", json=payload)
    return {"created": pick(created, *CATEGORY_FIELDS)}


@tool("write", live_guard=True)
async def update_category(
    app: App,
    event: str,
    category_id: int,
    name: str | None = None,
    internal_name: str | None = None,
    description: str | None = None,
    position: int | None = None,
    is_addon: bool | None = None,
) -> dict:
    """Rename or reorder a category. Only the fields you pass are changed.

    Which products are in the category is a property of the product — use update_product.
    """
    payload = clean(
        {
            "name": name,
            "internal_name": internal_name,
            "description": description,
            "position": position,
            "is_addon": is_addon,
        }
    )
    _require(payload)
    updated = await app.pretix.patch(
        "events",
        app.check_event(event),
        "categories",
        str(object_id(category_id, field="category_id")),
        json=payload,
    )
    return {"updated": pick(updated, *CATEGORY_FIELDS), "changed": sorted(payload)}


@tool("write", live_guard=True)
async def create_quota(
    app: App,
    event: str,
    name: str,
    size: int | None = None,
    items: list[int] | None = None,
    variations: list[int] | None = None,
    subevent: int | None = None,
) -> dict:
    """Create a quota: how many of the listed products/variations may be sold in total.

    ``size`` omitted or null means unlimited; 0 blocks sales entirely. A quota that lists a
    product with variations must list the ``variations`` ids instead of relying on the item.
    ``subevent`` is required for an event series and must be omitted otherwise.
    """
    payload = clean(
        {
            "name": name,
            "items": _ids(items, "items"),
            "variations": _ids(variations, "variations"),
            "subevent": _opt_id(subevent, "subevent"),
        }
    )
    payload["size"] = _size(size)  # explicit: null is meaningful (unlimited), clean() would drop it
    created = await app.pretix.post("events", app.check_event(event), "quotas", json=payload)
    return {"created": pick(created, *QUOTA_FIELDS)}


@tool("write", live_guard=True)
async def update_quota(
    app: App,
    event: str,
    quota_id: int,
    name: str | None = None,
    size: int | None = None,
    unlimited: bool = False,
    items: list[int] | None = None,
    variations: list[int] | None = None,
    subevent: int | None = None,
) -> dict:
    """Resize a quota or change which products it covers. Only the fields you pass change.

    Pass ``unlimited=true`` to remove the cap (``size=null``); passing ``size`` alone sets a
    number. ``items``/``variations`` replace the current lists rather than adding to them —
    read the quota first with list_quotas. Shrinking a quota never cancels existing orders.
    """
    payload = clean(
        {
            "name": name,
            "size": _size(size),
            "items": _ids(items, "items"),
            "variations": _ids(variations, "variations"),
            "subevent": _opt_id(subevent, "subevent"),
        }
    )
    if unlimited:
        if "size" in payload:
            raise ValidationError("pass either size or unlimited=true, not both")
        payload["size"] = None
    _require(payload)
    updated = await app.pretix.patch(
        "events", app.check_event(event), "quotas", str(object_id(quota_id, field="quota_id")), json=payload
    )
    return {"updated": pick(updated, *QUOTA_FIELDS), "changed": sorted(payload)}


@tool("write", live_guard=True)
async def create_question(
    app: App,
    event: str,
    question: str,
    type: str,
    required: bool = False,
    options: list[str] | None = None,
    items: list[int] | None = None,
    position: int | None = None,
    identifier: str | None = None,
    help_text: str | None = None,
    ask_during_checkin: bool = False,
) -> dict:
    """Add an attendee question / order form field.

    ``type``: N number, S single line, T multi-line, B boolean, C single choice, M multiple
    choice, F file upload, D date, H time, W datetime, CC country, TEL phone. ``options`` is
    the list of choice labels and is required for C and M, rejected for every other type.
    ``items`` limits the question to those products; empty means it is asked for none.
    """
    kind = _question_type(type)
    if kind in CHOICE_TYPES and not options:
        raise ValidationError(f"question type {kind!r} needs a non-empty options list")
    if options and kind not in CHOICE_TYPES:
        raise ValidationError(f"options are only allowed for question types {CHOICE_TYPES}, not {kind!r}")
    payload = clean(
        {
            "question": question,
            "type": kind,
            "required": required,
            "items": _ids(items, "items"),
            "position": position,
            "identifier": identifier,
            "help_text": help_text,
            "ask_during_checkin": ask_during_checkin,
            "options": [{"answer": label} for label in options] if options else None,
        }
    )
    created = await app.pretix.post("events", app.check_event(event), "questions", json=payload)
    return {"created": _question(created)}


@tool("write", live_guard=True)
async def update_question(
    app: App,
    event: str,
    question_id: int,
    question: str | None = None,
    type: str | None = None,
    required: bool | None = None,
    items: list[int] | None = None,
    position: int | None = None,
    help_text: str | None = None,
    hidden: bool | None = None,
    ask_during_checkin: bool | None = None,
) -> dict:
    """Change a question's wording, type, required flag, products or sort position.

    The choice ``options`` of a C/M question cannot be edited: the pretix API refuses them on
    an update and only exposes them through nested option endpoints this server does not
    expose. To change the choices, delete the question and create it again.
    """
    payload = clean(
        {
            "question": question,
            "type": _question_type(type) if type is not None else None,
            "required": required,
            "items": _ids(items, "items"),
            "position": position,
            "help_text": help_text,
            "hidden": hidden,
            "ask_during_checkin": ask_during_checkin,
        }
    )
    _require(payload)
    updated = await app.pretix.patch(
        "events",
        app.check_event(event),
        "questions",
        str(object_id(question_id, field="question_id")),
        json=payload,
    )
    return {"updated": _question(updated), "changed": sorted(payload)}


# ---------------------------------------------------------------------- high risk


async def _delete_product_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    item = await app.pretix.get(
        "events",
        app.check_event(kwargs["event"]),
        "items",
        str(object_id(kwargs.get("item_id"), field="item_id")),
    )
    summary = _item(item, VARIATION_SUMMARY)
    variations = summary.get("variations") or []
    return (
        f"DELETE product #{summary.get('id')} '{summary.get('name')}' from event "
        f"'{kwargs['event']}' — price {summary.get('default_price')}, active="
        f"{summary.get('active')}, {len(variations)} variation(s). pretix refuses this while "
        "the product appears in any order; it is irreversible otherwise.",
        summary,
    )


async def _delete_quota_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    quota = await app.pretix.get(
        "events",
        app.check_event(kwargs["event"]),
        "quotas",
        str(object_id(kwargs.get("quota_id"), field="quota_id")),
        params={"with_availability": "true"},
    )
    summary = pick(quota, *QUOTA_FIELDS, *AVAILABILITY_FIELDS)
    return (
        f"DELETE quota #{summary.get('id')} '{summary.get('name')}' from event "
        f"'{kwargs['event']}' — size {summary.get('size')}, {summary.get('available_number')} "
        f"still available, {summary.get('paid_orders')} paid / {summary.get('pending_orders')} "
        f"pending / {summary.get('cart_positions')} in carts. Covers items "
        f"{summary.get('items')} variations {summary.get('variations')}, which stop selling "
        "unless another quota covers them. Irreversible.",
        summary,
    )


@tool("write:high-risk", preview=_delete_product_preview)
async def delete_product(app: App, event: str, item_id: int) -> dict:
    """Delete a product. pretix refuses while it is part of any order — deactivate instead.

    To take a product off sale without losing it, call update_product with active=false.
    """
    await app.pretix.delete(
        "events", app.check_event(event), "items", str(object_id(item_id, field="item_id"))
    )
    return {"deleted": {"item_id": item_id, "event": event}}


@tool("write:high-risk", preview=_delete_quota_preview)
async def delete_quota(app: App, event: str, quota_id: int) -> dict:
    """Delete a quota. Every product covered only by this quota stops being sellable.

    To stop sales while keeping the quota, use update_quota with size=0.
    """
    await app.pretix.delete(
        "events", app.check_event(event), "quotas", str(object_id(quota_id, field="quota_id"))
    )
    return {"deleted": {"quota_id": quota_id, "event": event}}


@tool("write:high-risk")
async def delete_category(app: App, event: str, category_id: int) -> dict:
    """Delete a product category. Products in it become uncategorised rather than deleted."""
    await app.pretix.delete(
        "events", app.check_event(event), "categories", str(object_id(category_id, field="category_id"))
    )
    return {"deleted": {"category_id": category_id, "event": event}}


@tool("write:high-risk")
async def delete_question(app: App, event: str, question_id: int) -> dict:
    """Delete an attendee question. Answers already given to it are deleted with it.

    To stop asking a question without losing the answers, use update_question hidden=true.
    """
    await app.pretix.delete(
        "events", app.check_event(event), "questions", str(object_id(question_id, field="question_id"))
    )
    return {"deleted": {"question_id": question_id, "event": event}}


# ------------------------------------------------------------------------ helpers


def _item(item: dict[str, Any], variation_fields: tuple[str, ...], *fields: str) -> dict[str, Any]:
    shaped = pick(item, *(fields or ITEM_SUMMARY))
    if "variations" in item:
        shaped["variations"] = [pick(v, *variation_fields) for v in item["variations"] or []]
    return shaped


def _question(question: dict[str, Any]) -> dict[str, Any]:
    shaped = pick(question, *QUESTION_FIELDS)
    if "options" in question:
        shaped["options"] = [
            # pretix calls the choice label `answer`; renamed here because the redactor
            # treats `answer` as customer free text and would mask every option.
            {"id": o.get("id"), "identifier": o.get("identifier"), "label": i18n(o.get("answer"))}
            for o in question["options"] or []
        ]
    return shaped


def _variations_path(app: App, event: str, item_id: int) -> tuple[str, ...]:
    return ("events", app.check_event(event), "items", str(object_id(item_id, field="item_id")), "variations")


def _subevent_filter(subevent: int | None) -> dict[str, Any]:
    return {} if subevent is None else {"subevent": object_id(subevent, field="subevent")}


def _require(payload: dict[str, Any]) -> None:
    if not payload:
        raise ValidationError("nothing to update: pass at least one field")


def _size(value: object) -> int | None:
    """Quota size: null is unlimited, 0 is a valid (blocking) size, so object_id won't do."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"invalid size: {value!r} — a non-negative integer, or null for unlimited")
    return value


def _question_type(value: object) -> str:
    if not isinstance(value, str) or value.strip().upper() not in QUESTION_TYPES:
        raise ValidationError(f"invalid question type: {value!r} — one of {QUESTION_TYPES}")
    return value.strip().upper()


def _opt_id(value: object, field: str) -> int | None:
    return None if value is None else object_id(value, field=field)


def _ids(values: list[int] | None, field: str) -> list[int] | None:
    if values is None:
        return None
    if not isinstance(values, list):
        raise ValidationError(f"{field} must be a list of numeric ids")
    return [object_id(v, field=field) for v in values]
