"""Orders, attendees and the money that moves through them.

Two things shape this module. First, orders are where pretix keeps its PII, so list
results carry codes, states and numbers only — contact details, invoice addresses and
attendee address blocks appear in ``get_order`` and ``search_attendees``, where the
caller asked for them, and nowhere else. Second, pretix has no aggregate-stats endpoint,
so ``sales_summary`` computes its numbers by paginating orders and is honest about the
window it managed to scan.

Money is summed with :class:`decimal.Decimal` and returned as strings; pretix speaks
decimal strings and a float round-trip would quietly change a total.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from ..redact import mask_email
from ..registry import App, tool
from ..validate import ValidationError, object_id, order_code, page_size
from ._shared import clean, i18n, listing, pick

# pretix order states. There is no "refunded" state — refunds are separate objects on
# the order, so refunded money is reported as its own bucket in sales_summary.
STATUS_NAMES = {"n": "pending", "p": "paid", "e": "expired", "c": "canceled"}

ORDER_SUMMARY = ("code", "status", "datetime", "expires", "total", "payment_provider")
ORDER_DETAIL = ORDER_SUMMARY + (
    "testmode",
    "email",
    "phone",
    "locale",
    "sales_channel",
    "payment_date",
    "comment",
    "checkin_attention",
    "require_approval",
    "valid_if_pending",
    "cancellation_date",
)
INVOICE_ADDRESS = (
    "company",
    "is_business",
    "name",
    "street",
    "zipcode",
    "city",
    "country",
    "state",
    "vat_id",
)
POSITION = (
    "id",
    "positionid",
    "order",
    "item",
    "variation",
    "price",
    "attendee_name",
    "attendee_email",
    "subevent",
    "canceled",
)
# Attendee address block: only ever returned by get_order / search_attendees.
POSITION_ADDRESS = ("company", "street", "zipcode", "city", "country", "state")

# Trim the fat off order listings: ticket download links are useless in an LLM context.
NO_DOWNLOADS = {"exclude": ["positions.downloads"]}


@tool("read")
async def search_orders(
    app: App,
    event: str,
    status: str | None = None,
    email: str | None = None,
    search: str | None = None,
    created_since: str | None = None,
    subevent: int | None = None,
    limit: int = 50,
) -> dict:
    """Find orders of one event, newest first, as a compact summary per order.

    Filters: ``status`` (``n`` pending, ``p`` paid, ``e`` expired, ``c`` canceled),
    ``email`` (exact buyer address), ``search`` (fuzzy over names, emails, companies),
    ``created_since`` (ISO 8601), ``subevent`` (series date id). Without filters you get
    the most recent ``limit`` orders — the page size is capped server-side, so this never
    dumps the whole event.

    Returns no contact details: use get_order for one order's buyer and attendee data, or
    search_attendees to look across attendees. For totals and counts use sales_summary.
    """
    slug = app.check_event(event)
    params: dict[str, Any] = dict(NO_DOWNLOADS, ordering="-datetime")
    if status is not None:
        params["status"] = _status(status)
    if subevent is not None:
        params["subevent"] = object_id(subevent, field="subevent")
    params.update(clean({"email": email, "search": search, "created_since": created_since}))
    cap = page_size(limit)
    orders, total, truncated = await app.pretix.paginate("events", slug, "orders", params=params, cap=cap)
    currency = (await app.event(slug)).get("currency")
    return listing([_order_summary(o) for o in orders], total=total, truncated=truncated, currency=currency)


@tool("read")
async def get_order(app: App, event: str, code: str) -> dict:
    """One order in full: buyer contact, invoice address, payments, refunds and positions.

    Each position carries product, variation, price, the attendee's name/email/answers and
    whether the ticket has been checked in. This is the only order tool that returns
    contact and address data, so prefer search_orders when you only need states or totals.
    """
    slug = app.check_event(event)
    order = await app.pretix.get("events", slug, "orders", order_code(code), params=NO_DOWNLOADS)
    items, variations = await _catalog(app, slug)
    positions = order.get("positions") or []
    return {
        **pick(order, *ORDER_DETAIL),
        "invoice_address": pick(order.get("invoice_address"), *INVOICE_ADDRESS),
        "fees": [pick(f, "fee_type", "value", "description", "canceled") for f in order.get("fees") or []],
        "payments": [
            pick(p, "local_id", "state", "amount", "provider", "payment_date")
            for p in order.get("payments") or []
        ],
        "refunds": [
            pick(r, "local_id", "state", "amount", "provider", "execution_date")
            for r in order.get("refunds") or []
        ],
        "positions": [_position(p, items, variations) for p in positions],
    }


@tool("read")
async def search_attendees(
    app: App,
    event: str,
    item: int | None = None,
    subevent: int | None = None,
    search: str | None = None,
    has_checkin: bool | None = None,
    order_status: str | None = None,
    limit: int = 50,
) -> dict:
    """Find individual attendees (order positions) of one event — one row per ticket.

    Filters: ``item`` (product id), ``subevent`` (series date id), ``search`` (fuzzy over
    attendee name, order code and invoice name), ``has_checkin`` (checked in yet or not),
    ``order_status`` (``n``/``p``/``e``/``c``). Results are capped server-side.

    pretix cannot filter this endpoint by a *specific* check-in list — ``has_checkin`` is
    "checked in on any list". Use the check-in tools for per-list questions.
    """
    slug = app.check_event(event)
    params: dict[str, Any] = {}
    if item is not None:
        params["item"] = object_id(item, field="item")
    if subevent is not None:
        params["subevent"] = object_id(subevent, field="subevent")
    if has_checkin is not None:
        params["has_checkin"] = "true" if has_checkin else "false"
    if order_status is not None:
        params["order__status"] = _status(order_status)
    if search is not None:
        params["search"] = search
    cap = page_size(limit)
    found, total, truncated = await app.pretix.paginate(
        "events", slug, "orderpositions", params=params, cap=cap
    )
    items, variations = await _catalog(app, slug)
    return listing([_position(p, items, variations) for p in found], total=total, truncated=truncated)


@tool("read")
async def sales_summary(
    app: App,
    event: str,
    subevent: int | None = None,
    by_item: bool = False,
    by_subevent: bool = False,
) -> dict:
    """Ticket-sales figures for one event: revenue and counts per order state, no PII.

    pretix has no aggregate-stats endpoint, so this paginates the event's orders and adds
    them up here. **On a large event it is slow** — one HTTP round trip per 100 orders —
    and it stops after ``SALES_SCAN_CAP`` orders (default 5000). The ``scan`` block in the
    result reports how many orders were read and whether the cap truncated the window; a
    truncated result is a partial answer, so narrow it with ``subevent`` if that happens.

    ``tickets`` counts non-canceled positions of paid and pending orders. ``revenue`` is
    order totals grouped by state, plus a ``refunded`` bucket summed from completed
    refunds. Set ``by_item`` / ``by_subevent`` for per-product / per-date breakdowns
    (paid and pending only). Amounts are decimal strings in the event's currency.
    """
    slug = app.check_event(event)
    params: dict[str, Any] = dict(NO_DOWNLOADS)
    if subevent is not None:
        params["subevent"] = object_id(subevent, field="subevent")
    cap = max(app.cfg.scan_cap, 1)
    orders, total, truncated = await app.pretix.paginate("events", slug, "orders", params=params, cap=cap)

    counts = dict.fromkeys(STATUS_NAMES.values(), 0)
    revenue = {name: Decimal(0) for name in STATUS_NAMES.values()}
    refunded = Decimal(0)
    tickets = 0
    per_item: dict[Any, list[Any]] = defaultdict(lambda: [0, Decimal(0)])
    per_subevent: dict[Any, list[Any]] = defaultdict(lambda: [0, Decimal(0)])

    for order in orders:
        name = STATUS_NAMES.get(order.get("status"))
        if name is None:
            continue
        counts[name] += 1
        revenue[name] += _money(order.get("total"))
        for refund in order.get("refunds") or []:
            if refund.get("state") == "done":
                refunded += _money(refund.get("amount"))
        if name not in ("paid", "pending"):
            continue
        for position in order.get("positions") or []:
            if position.get("canceled"):
                continue
            tickets += 1
            price = _money(position.get("price"))
            for bucket, key in ((per_item, position.get("item")), (per_subevent, position.get("subevent"))):
                bucket[key][0] += 1
                bucket[key][1] += price

    result: dict[str, Any] = {
        "event": slug,
        "currency": (await app.event(slug)).get("currency"),
        "scan": {
            "orders_scanned": len(orders),
            "orders_matching": total,
            "cap": cap,
            "truncated": truncated,
        },
        "orders": counts,
        "revenue": {**{name: _str(value) for name, value in revenue.items()}, "refunded": _str(refunded)},
        "tickets": tickets,
    }
    if truncated:
        result["note"] = (
            f"Partial: only the {cap} most recently created orders were scanned. "
            "Narrow the scan (subevent) or raise SALES_SCAN_CAP for a complete answer."
        )
    if by_item or by_subevent:
        items, _ = await _catalog(app, slug)
        if by_item:
            result["by_item"] = _breakdown(per_item, "item", items)
        if by_subevent:
            result["by_subevent"] = _breakdown(per_subevent, "subevent", None)
    return result


@tool("write", live_guard=True)
async def mark_order_paid(app: App, event: str, code: str, send_email: bool = True) -> dict:
    """Record a pending or expired order as paid (e.g. after a bank transfer arrived).

    By default this emails the customer their payment confirmation. It does not move any
    money — pretix only records the payment.
    """
    order = await app.pretix.post(
        "events",
        app.check_event(event),
        "orders",
        order_code(code),
        "mark_paid",
        json={"send_email": send_email},
    )
    return {"marked_paid": pick(order, *ORDER_SUMMARY)}


@tool("write", live_guard=True)
async def extend_payment_deadline(app: App, event: str, code: str, expires: str, force: bool = False) -> dict:
    """Give a pending or expired order a new payment deadline.

    ``expires`` is a future date (``2027-03-31``). pretix refuses the change if no quota is
    left to hold the tickets; ``force=true`` pushes it through and may overbook the quota.
    """
    order = await app.pretix.post(
        "events",
        app.check_event(event),
        "orders",
        order_code(code),
        "extend",
        json={"expires": expires, "force": force},
    )
    return {"extended": pick(order, *ORDER_SUMMARY)}


@tool("write")
async def add_order_comment(app: App, event: str, code: str, comment: str) -> dict:
    """Set the internal comment on an order — staff-only, never shown to the customer.

    pretix stores a single comment field, so this replaces any existing comment rather
    than appending to it. Read the current one with get_order first.
    """
    if not comment.strip():
        raise ValidationError("comment must not be empty")
    order = await app.pretix.patch(
        "events", app.check_event(event), "orders", order_code(code), json={"comment": comment}
    )
    return {"code": order.get("code"), "comment": order.get("comment")}


@tool("write", live_guard=True)
async def edit_attendee(
    app: App,
    event: str,
    position_id: int,
    attendee_name: str | None = None,
    attendee_email: str | None = None,
    answers: list[dict[str, Any]] | None = None,
) -> dict:
    """Correct the attendee name, email or question answers on one order position.

    ``position_id`` is the position ``id`` from get_order or search_attendees (not the
    per-order ``positionid``). pretix replaces *all* answers of the position, so pass the
    complete list — each entry ``{"question": <id>, "answer": "..."}``, with
    ``"file:keep"`` to keep an existing file upload. Does not notify the attendee.
    """
    payload = clean({"attendee_name": attendee_name, "attendee_email": attendee_email, "answers": answers})
    if not payload:
        raise ValidationError("nothing to update: pass attendee_name, attendee_email or answers")
    position = await app.pretix.patch(
        "events",
        app.check_event(event),
        "orderpositions",
        str(object_id(position_id, field="position_id")),
        json=payload,
    )
    return {
        "updated": pick(position, "id", "positionid", "order", "attendee_name", "attendee_email"),
        "answers": [pick(a, "question", "answer") for a in position.get("answers") or []],
        "changed": sorted(payload),
    }


@tool("write")
async def resend_order_email(app: App, event: str, code: str) -> dict:
    """Send the customer another email with the link to their order and tickets.

    This really does email the buyer's address — use it when someone lost their
    confirmation, not to check whether an address is valid.
    """
    code = order_code(code)
    await app.pretix.post("events", app.check_event(event), "orders", code, "resend_link")
    return {"resent_to_buyer_of": code}


async def _cancel_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    # Previews validate too: the tool body only runs after approval, and an operator
    # should never approve an action that is going to be rejected on the way out.
    order = await _order(app, kwargs)
    raw_fee = kwargs.get("cancellation_fee")
    fee = _amount(raw_fee, field="cancellation_fee") if raw_fee is not None else None
    keep = f"keep {fee} as a cancellation fee" if fee else "cancel the order in full, fees included"
    return (
        f"CANCEL order {order.get('code')} — {keep}.\n"
        f"  status: {STATUS_NAMES.get(order.get('status'), order.get('status'))}\n"
        f"  total:  {order.get('total')}\n"
        f"  customer: {mask_email(order.get('email') or '')}\n"
        f"  email the customer: {kwargs.get('send_email', True)}",
        pick(order, *ORDER_SUMMARY),
    )


@tool("write:high-risk", preview=lambda app, kwargs: _cancel_preview(app, kwargs))
async def cancel_order(
    app: App,
    event: str,
    code: str,
    cancellation_fee: str | None = None,
    send_email: bool = True,
    comment: str | None = None,
) -> dict:
    """Cancel an order and release its tickets back into the quota.

    Without ``cancellation_fee`` the whole order is canceled, fees included, and goes to
    state ``c``. With a ``cancellation_fee`` (a decimal string like ``"5.00"``) a paid
    order instead *stays paid*: every position is removed and replaced by that retained
    fee. pretix offers no other way to keep fees — there is no keep-fees flag.

    Cancelling does not refund money; create the refund separately with refund_order.
    ``comment`` may be shown to the customer in the cancellation email.
    """
    payload: dict[str, Any] = {"send_email": send_email}
    if cancellation_fee is not None:
        payload["cancellation_fee"] = _amount(cancellation_fee, field="cancellation_fee")
    if comment is not None:
        payload["comment"] = comment
    order = await app.pretix.post(
        "events", app.check_event(event), "orders", order_code(code), "mark_canceled", json=payload
    )
    return {"canceled": pick(order, *ORDER_SUMMARY)}


async def _refund_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    amount = _amount(kwargs.get("amount"), field="amount")  # reject bogus money before queueing
    order = await _order(app, kwargs)
    payments = [pick(p, "local_id", "state", "amount", "provider") for p in order.get("payments") or []]
    provider = kwargs.get("provider", "manual")
    state = STATUS_NAMES.get(order.get("status"), order.get("status"))
    return (
        f"REFUND {amount} on order {order.get('code')} via provider '{provider}'.\n"
        f"  order total: {order.get('total')} ({state})\n"
        f"  customer: {mask_email(order.get('email') or '')}\n"
        f"  payments on file: {payments or 'none'}\n"
        f"  against payment: {kwargs.get('payment') or 'unspecified'}",
        {"order": pick(order, *ORDER_SUMMARY), "payments": payments},
    )


@tool("write:high-risk", preview=lambda app, kwargs: _refund_preview(app, kwargs))
async def refund_order(
    app: App,
    event: str,
    code: str,
    amount: str,
    provider: str = "manual",
    payment: int | None = None,
    comment: str | None = None,
    state: str = "created",
    mark_canceled: bool = False,
    mark_pending: bool = True,
) -> dict:
    """Record a refund against an order — money going back to the customer.

    ``amount`` is a decimal string (``"23.00"``). pretix only reliably automates this for
    provider ``manual``: it records the refund without asking the payment provider to move
    anything, so with ``manual`` an operator still has to transfer the money. ``payment``
    is the ``local_id`` of the payment being refunded (see get_order). ``state`` is
    ``created`` (still to be executed) or ``done`` (money already sent).

    pretix does not check that the amount matches the payment — read the order first.
    """
    if state not in {"created", "done"}:
        raise ValidationError("state must be 'created' or 'done'")
    payload: dict[str, Any] = {
        "state": state,
        "source": "admin",
        "amount": _amount(amount, field="amount"),
        "provider": provider,
        "mark_canceled": mark_canceled,
        "mark_pending": mark_pending,
    }
    if payment is not None:
        payload["payment"] = object_id(payment, field="payment")
    if comment is not None:
        payload["comment"] = comment
    refund = await app.pretix.post(
        "events", app.check_event(event), "orders", order_code(code), "refunds", json=payload
    )
    return {"refund": pick(refund, "local_id", "state", "amount", "provider", "execution_date")}


async def _order(app: App, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Fetch the order a high-risk preview is about."""
    return await app.pretix.get(
        "events",
        app.check_event(kwargs["event"]),
        "orders",
        order_code(kwargs["code"]),
        params={"exclude": ["positions"]},
    )


def _order_summary(order: dict[str, Any]) -> dict[str, Any]:
    summary = pick(order, *ORDER_SUMMARY)
    positions = order.get("positions")
    if isinstance(positions, list):
        summary["item_count"] = sum(1 for p in positions if not p.get("canceled"))
    return summary


def _position(position: dict[str, Any], items: dict[int, Any], variations: dict[int, Any]) -> dict[str, Any]:
    out = pick(position, *POSITION)
    out["item_name"] = items.get(position.get("item"))
    out["variation_name"] = variations.get(position.get("variation"))
    out.update(pick(position, *POSITION_ADDRESS))
    out["answers"] = [
        pick(a, "question", "question_identifier", "answer") for a in position.get("answers") or []
    ]
    checkins = position.get("checkins")
    if isinstance(checkins, list):
        out["checked_in"] = bool(checkins)
        out["checkins"] = [pick(c, "list", "type", "datetime") for c in checkins]
    return out


async def _catalog(app: App, event_slug: str) -> tuple[dict[int, Any], dict[int, Any]]:
    """Product and variation names, so positions read as products rather than ids."""
    items, _, _ = await app.pretix.paginate("events", event_slug, "items", cap=1000)
    names = {i["id"]: i18n(i.get("name")) for i in items if isinstance(i.get("id"), int)}
    variations = {
        v["id"]: i18n(v.get("value"))
        for i in items
        for v in i.get("variations") or []
        if isinstance(v.get("id"), int)
    }
    return names, variations


def _breakdown(bucket: dict[Any, list[Any]], key: str, names: dict[int, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for k, (count, total) in sorted(bucket.items(), key=lambda kv: kv[1][1], reverse=True):
        row: dict[str, Any] = {key: k, "tickets": count, "revenue": _str(total)}
        if names is not None:
            row["name"] = names.get(k)
        rows.append(row)
    return rows


def _status(value: object) -> str:
    if value in STATUS_NAMES:
        return str(value)
    raise ValidationError(
        f"invalid order status {value!r}: pretix uses 'n' (pending), 'p' (paid), 'e' (expired), "
        "'c' (canceled). There is no refunded status — see sales_summary for refunded amounts."
    )


def _money(value: object) -> Decimal:
    """pretix money is a decimal string. Anything unparseable counts as zero rather than
    breaking a whole summary over one odd order."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _amount(value: object, *, field: str) -> str:
    """Validate an agent-supplied money value. This one must not be forgiving."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"invalid {field}: {value!r} is not a decimal amount") from None
    if not amount.is_finite() or amount < 0:
        raise ValidationError(f"{field} must be a non-negative amount, got {value!r}")
    return _str(amount)


def _str(amount: Decimal) -> str:
    return f"{amount:.2f}"
