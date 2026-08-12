"""Vouchers: discount and free-ticket codes.

Vouchers are ordinary ``write`` work — they never change what an existing customer
already bought, so they do not carry the live-event guard. What they *can* do is give
away money, so price modes and values are validated here before anything is sent, and
deleting a voucher is high-risk (it is the one voucher operation that destroys history).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..registry import App, tool
from ..validate import ValidationError, object_id, page_size, price
from ._shared import clean, listing, pick

PRICE_MODES = ("none", "set", "subtract", "percent")
VOUCHER_FIELDS = (
    "id",
    "code",
    "item",
    "variation",
    "quota",
    "subevent",
    "price_mode",
    "value",
    "valid_until",
    "redeemed",
    "max_usages",
    "min_usages",
    "block_quota",
    "tag",
    "comment",
)
# vouchers/batch_create/ is atomic on pretix's side, but the payload still goes over one
# request — keep it to a size that stays inside a normal request timeout.
MAX_BATCH = 500


@tool("read")
async def list_vouchers(
    app: App,
    event: str,
    code: str | None = None,
    tag: str | None = None,
    item: int | None = None,
    subevent: int | None = None,
    price_mode: str | None = None,
    limit: int = 50,
) -> dict:
    """List an event's vouchers with their price mode, value, usage count and validity.

    ``redeemed`` versus ``max_usages`` tells you whether a code can still be used; pretix
    has no "still usable" filter, so filter client-side after reading.
    """
    params: dict[str, Any] = clean(
        {
            "code": code,
            "tag": tag,
            "item": object_id(item, field="item") if item is not None else None,
            "subevent": object_id(subevent, field="subevent") if subevent is not None else None,
            "price_mode": _price_mode(price_mode) if price_mode is not None else None,
        }
    )
    vouchers, total, truncated = await app.pretix.paginate(
        "events", app.check_event(event), "vouchers", params=params, cap=page_size(limit)
    )
    return listing([pick(v, *VOUCHER_FIELDS) for v in vouchers], total=total, truncated=truncated)


@tool("write", money=("value",))
async def create_voucher(
    app: App,
    event: str,
    price_mode: str = "set",
    value: str = "0.00",
    code: str | None = None,
    item: int | None = None,
    variation: int | None = None,
    quota: int | None = None,
    subevent: int | None = None,
    max_usages: int = 1,
    valid_until: str | None = None,
    block_quota: bool = False,
    tag: str | None = None,
    comment: str | None = None,
) -> dict:
    """Create one voucher. Leave ``code`` empty and pretix generates a random code.

    ``price_mode`` is 'set' (the price becomes ``value``), 'subtract' (``value`` off),
    'percent' (``value`` percent off) or 'none' (no discount — just access). A free ticket
    is price_mode='set', value='0.00'. Point the voucher at exactly one of ``item`` (plus
    ``variation`` for a product with variations) or ``quota``. ``valid_until`` is an
    ISO 8601 datetime. For many codes at once use create_vouchers_batch instead.
    """
    payload = _voucher_payload(
        code=code,
        price_mode=price_mode,
        value=value,
        item=item,
        variation=variation,
        quota=quota,
        subevent=subevent,
        max_usages=max_usages,
        valid_until=valid_until,
        block_quota=block_quota,
        tag=tag,
        comment=comment,
    )
    created = await app.pretix.post("events", app.check_event(event), "vouchers", json=payload)
    return {"created": pick(created, *VOUCHER_FIELDS)}


# A single voucher is an ordinary write; a bulk batch on a live event can give away or
# freeze hundreds of seats at once, which is exactly what the live-event guard is for.
@tool("write", live_guard=True, money=("value",))
async def create_vouchers_batch(
    app: App,
    event: str,
    price_mode: str = "set",
    value: str = "0.00",
    count: int | None = None,
    codes: list[str] | None = None,
    item: int | None = None,
    variation: int | None = None,
    quota: int | None = None,
    subevent: int | None = None,
    max_usages: int = 1,
    valid_until: str | None = None,
    block_quota: bool = False,
    tag: str | None = None,
    comment: str | None = None,
) -> dict:
    """Create many identical vouchers in one atomic call, and report the codes.

    Pass either ``count`` (pretix generates that many random codes) or ``codes`` (your own
    list, e.g. sponsor names) — not both. Every other argument is the template shared by
    all of them and means the same as in create_voucher. Give them a ``tag`` so you can
    find and count this batch again with list_vouchers.
    """
    if (count is None) == (codes is None):
        raise ValidationError("pass exactly one of count or codes")
    if codes is not None:
        wanted: list[str | None] = [_code(c) for c in codes]
        if not wanted:
            raise ValidationError("codes must not be empty")
        if len(set(wanted)) != len(wanted):
            raise ValidationError("codes contains duplicates; pretix rejects the whole batch")
    else:
        # Check the cap *before* building the list: a huge count must be refused, not allocated.
        requested = object_id(count, field="count")
        if requested > MAX_BATCH:
            raise ValidationError(f"too many vouchers: {requested} (max {MAX_BATCH} per call)")
        wanted = [None] * requested
    if len(wanted) > MAX_BATCH:
        raise ValidationError(f"too many vouchers: {len(wanted)} (max {MAX_BATCH} per call)")

    template = dict(
        price_mode=price_mode,
        value=value,
        item=item,
        variation=variation,
        quota=quota,
        subevent=subevent,
        max_usages=max_usages,
        valid_until=valid_until,
        block_quota=block_quota,
        tag=tag,
        comment=comment,
    )
    payload = [_voucher_payload(code=code, **template) for code in wanted]
    created = await app.pretix.post(
        "events", app.check_event(event), "vouchers", "batch_create", json=payload
    )
    vouchers = created if isinstance(created, list) else []
    return {
        "created_count": len(vouchers),
        "codes": [v.get("code") for v in vouchers if isinstance(v, dict)],
        "created": [pick(v, *VOUCHER_FIELDS) for v in vouchers if isinstance(v, dict)],
    }


async def _delete_voucher_preview(app: App, kwargs: dict[str, Any]) -> tuple[str, Any]:
    voucher_id = object_id(kwargs.get("voucher_id"), field="voucher_id")
    voucher = await app.pretix.get("events", app.check_event(kwargs["event"]), "vouchers", str(voucher_id))
    summary = pick(voucher, *VOUCHER_FIELDS)
    return (
        f"DELETE voucher {summary.get('code')} (id {voucher_id}): {summary.get('price_mode')} "
        f"{summary.get('value')} on item={summary.get('item')} quota={summary.get('quota')}, "
        f"tag={summary.get('tag')!r}.\n"
        f"  redeemed {summary.get('redeemed')} of {summary.get('max_usages')} usages.\n"
        "pretix refuses to delete a voucher that has been redeemed. Irreversible.",
        summary,
    )


@tool("write:high-risk", preview=_delete_voucher_preview)
async def delete_voucher(app: App, event: str, voucher_id: int) -> dict:
    """Delete a voucher. pretix only allows this while it has never been redeemed.

    Irreversible, and it loses the record that the code existed. To stop an already
    redeemed code from being used again, set its valid_until to the past instead.
    """
    await app.pretix.delete(
        "events", app.check_event(event), "vouchers", str(object_id(voucher_id, field="voucher_id"))
    )
    return {"deleted": voucher_id}


def _voucher_payload(
    *,
    code: str | None,
    price_mode: str,
    value: str,
    item: int | None,
    variation: int | None,
    quota: int | None,
    subevent: int | None,
    max_usages: int,
    valid_until: str | None,
    block_quota: bool,
    tag: str | None,
    comment: str | None,
) -> dict[str, Any]:
    """One voucher object, validated. Shared by the single and the batch endpoint."""
    if item is not None and quota is not None:
        raise ValidationError("a voucher points at either item or quota, not both")
    if variation is not None and item is None:
        raise ValidationError("variation requires the item it belongs to")
    mode = _price_mode(price_mode)
    payload = clean(
        {
            "code": _code(code) if code is not None else None,
            "price_mode": mode,
            "value": _value(value, mode),
            "item": object_id(item, field="item") if item is not None else None,
            "variation": object_id(variation, field="variation") if variation is not None else None,
            "quota": object_id(quota, field="quota") if quota is not None else None,
            "subevent": object_id(subevent, field="subevent") if subevent is not None else None,
            "max_usages": object_id(max_usages, field="max_usages"),
            "valid_until": _valid_until(valid_until) if valid_until is not None else None,
            "tag": tag,
            "comment": comment,
        }
    )
    payload["block_quota"] = bool(block_quota)
    return payload


def _price_mode(value: object) -> str:
    if value not in PRICE_MODES:
        raise ValidationError(f"price_mode must be one of {', '.join(PRICE_MODES)}, not {value!r}")
    return str(value)


def _value(value: object, price_mode: str) -> str:
    """The sign rule for a voucher value. The amount itself arrived validated — the registry
    checks it before the body runs, so that a batch against a live event is refused *before*
    it is queued for approval rather than after a human granted it."""
    amount = price(value, field="value")
    if amount is None:
        raise ValidationError("value must be a decimal string like '12.00'")
    if price_mode != "none" and Decimal(amount) < 0:
        raise ValidationError(f"value must not be negative: {value!r}")
    return amount


def _valid_until(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            "valid_until must be an ISO 8601 datetime string, e.g. '2027-12-31T23:59:59+01:00'"
        )
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"valid_until is not an ISO 8601 datetime: {value!r}") from None
    return value


def _code(value: object) -> str:
    """pretix requires at least 5 characters and treats codes case-insensitively."""
    if not isinstance(value, str):
        raise ValidationError("voucher code must be a string")
    code = value.strip()
    if len(code) < 5 or len(code) > 255 or any(c.isspace() for c in code):
        raise ValidationError(f"invalid voucher code: {value!r} (5-255 characters, no whitespace)")
    return code
