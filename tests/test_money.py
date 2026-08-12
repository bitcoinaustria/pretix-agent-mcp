"""Money: one validator, applied to every amount before anything can act on it.

Two bugs are locked down here. First, three modules each parsed money their own way — one
sent ``str(19.999)`` and another ``'nan'`` straight through to pretix, while a third had it
right. Every amount now goes through :func:`pretix_agent_mcp.validate.price`.

Second, and subtler: a high-risk or live-escalated call is queued for approval *without
running its body*, so validating inside the body refused a bad price only after a human had
approved it. The registry checks the parameters a tool declares in ``money=`` before that
branch, and the tests below assert nothing unparseable ever reaches the pending store.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from pretix_agent_mcp.registry import REGISTRY
from pretix_agent_mcp.tools import _shared
from pretix_agent_mcp.validate import ValidationError, price

SERIES = {"slug": "stammtisch", "name": {"en": "Stammtisch"}, "live": False, "testmode": True}
ORDER = {"code": "ABC12", "status": "p", "total": "23.00", "email": "buyer@example.org"}

# Every shape that must not become a number in a pretix payload. Floats are the dangerous
# ones: they arrive from an agent's own arithmetic and look harmless.
BAD_AMOUNTS = [
    0.1 + 0.2,  # 0.30000000000000004
    12.005,
    19.999,
    "nan",
    "inf",
    "-inf",
    "1e5",
    "23.456",  # a third decimal place pretix would re-round
    "0.001",
    "twenty",
    "",
    "12,00",  # a comma is not a decimal point
    True,
]


@pytest.mark.parametrize("value", BAD_AMOUNTS)
def test_the_money_validator_refuses(value):
    with pytest.raises(ValidationError):
        price(value, field="amount")


@pytest.mark.parametrize("value", ["0.00", "23.00", "23.0", "23", "-5.00", " 7.50 ", "9999999999.99"])
def test_the_money_validator_accepts_decimal_strings(value):
    assert price(value, field="amount") == value.strip()


def test_none_passes_through_so_optional_prices_stay_optional():
    assert price(None) is None


async def test_a_float_price_never_reaches_a_subevent(api, call):
    """update_subevent used to send str(19.999). It is a price on a selling date."""
    api.route("GET", "events/stammtisch", SERIES)
    api.route("PATCH", "events/stammtisch/subevents/7", {"id": 7})

    with pytest.raises(ValidationError, match="item_prices"):
        await call("update_subevent", event="stammtisch", subevent_id=7, item_prices={"3": 19.999})

    assert api.sent("PATCH", "events/stammtisch/subevents/7") == []


async def test_a_float_price_never_reaches_a_new_series_date(api, call):
    api.route("GET", "events/stammtisch", SERIES)
    api.route("POST", "events/stammtisch/subevents", {"id": 11})

    with pytest.raises(ValidationError, match="item_prices"):
        await call(
            "create_subevents",
            event="stammtisch",
            dates=["2027-02-04T19:00:00+01:00"],
            name="Stammtisch",
            item_prices={"3": 0.1 + 0.2},
        )

    assert api.sent("POST", "events/stammtisch/subevents") == []


async def test_a_null_price_override_is_refused(api, call):
    api.route("GET", "events/stammtisch", SERIES)
    with pytest.raises(ValidationError, match="not null"):
        await call("update_subevent", event="stammtisch", subevent_id=7, item_prices={"3": None})


@pytest.mark.parametrize("value", ["nan", "inf", 12.005])
async def test_a_voucher_value_is_a_decimal_string_or_nothing(api, call, value):
    """A voucher decides what somebody pays; 'nan' used to be sent verbatim."""
    api.route("GET", "events/conf27", {"slug": "conf27", "live": False, "testmode": True})
    api.route("POST", "events/conf27/vouchers", {"id": 5, "code": "ABCDE"})

    with pytest.raises(ValidationError):
        await call("create_voucher", event="conf27", price_mode="set", value=value)

    assert api.sent("POST", "events/conf27/vouchers") == []


async def test_a_negative_voucher_value_is_still_refused(api, call):
    api.route("GET", "events/conf27", {"slug": "conf27", "live": False, "testmode": True})
    with pytest.raises(ValidationError, match="negative"):
        await call("create_voucher", event="conf27", price_mode="set", value="-5.00")


@pytest.mark.parametrize("amount", ["0.001", "23.456", 12.005, "nan"])
async def test_a_refund_amount_is_refused_rather_than_rounded(api, call, amount):
    """'0.001' silently became '0.00' — a refund of nothing, recorded as a refund."""
    api.route("GET", "events/conf27/orders/ABC12", ORDER)
    api.route("POST", "events/conf27/orders/ABC12/refunds", {"local_id": 1})

    with pytest.raises(ValidationError):
        await call("refund_order", event="conf27", code="ABC12", amount=amount)

    assert api.sent("POST", "events/conf27/orders/ABC12/refunds") == []


async def test_a_cancellation_fee_goes_through_the_same_validator(api, call):
    api.route("GET", "events/conf27/orders/ABC12", ORDER)
    with pytest.raises(ValidationError):
        await call("cancel_order", event="conf27", code="ABC12", cancellation_fee="1.005")


LIVE = {"slug": "stammtisch", "name": {"en": "Stammtisch"}, "live": True, "testmode": False}


@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        ("update_subevent", {"subevent_id": 7, "item_prices": {"3": 19.999}}),
        (
            "create_subevents",
            {"dates": ["2027-02-04T19:00:00+01:00"], "name": "x", "item_prices": {"3": "1e5"}},
        ),
        ("create_vouchers_batch", {"count": 100, "value": "nan"}),
        ("create_product", {"name": "Ticket", "default_price": 23.5}),
        ("update_product", {"item_id": 3, "default_price": "23.456"}),
        ("create_tax_rule", {"name": "VAT", "rate": 20.0}),
    ],
)
async def test_a_bad_amount_never_reaches_the_approval_queue(app, api, call, tool_name, kwargs):
    """On a live event these escalate to high-risk, which queues the call *without running
    its body*. Before the registry validated declared amounts, the bad price sat in the
    pending store and was only refused after a human had approved it."""
    api.route("GET", "events/stammtisch", LIVE)

    with pytest.raises(ValidationError):
        await call(tool_name, event="stammtisch", **kwargs)

    assert app.pending.list("pending") == [], "an unparseable amount must not be queued"
    assert [r for r in api.requests if r.method != "GET"] == []


def test_no_tool_module_parses_money_itself():
    """The regression this file exists for: a module reaching for float() or str() on an
    amount instead of the one validator. If a new money shape needs handling, widen
    validate.price rather than parsing it locally."""
    offenders = []
    for path in Path(_shared.__file__).parent.glob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#")[0]
            if "float(" in code or '"price": str(' in code or '"amount": str(' in code:
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], f"validate money with validate.price, not locally: {offenders}"


# Every parameter that holds an amount, and the tool that takes it. Declared here as well as
# in the decorator so that adding a priced parameter without declaring it fails the build.
MONEY_SURFACE = {
    "create_product": ("default_price",),
    "update_product": ("default_price",),
    "create_product_variation": ("default_price",),
    "update_product_variation": ("default_price",),
    "create_subevents": ("item_prices",),
    "update_subevent": ("item_prices",),
    "create_voucher": ("value",),
    "create_vouchers_batch": ("value",),
    "refund_order": ("amount",),
    "cancel_order": ("cancellation_fee",),
    "create_tax_rule": ("rate",),
    "update_tax_rule": ("rate",),
}
# Parameters whose name looks like money but is not: a variation's `value` is its label.
NOT_MONEY = {("create_product_variation", "value"), ("update_product_variation", "value")}
MONEY_NAMES = {"amount", "price", "default_price", "cancellation_fee", "rate", "item_prices", "value"}


def test_every_tool_declares_the_amounts_it_takes():
    """The registry validates what a tool declares in money=. A priced parameter that is not
    declared is validated only inside the body — which a high-risk call never reaches until
    after a human approved it."""
    for name, spec in REGISTRY.items():
        priced = {
            param
            for param in inspect.signature(spec.fn).parameters
            if param in MONEY_NAMES and (name, param) not in NOT_MONEY
        }
        assert priced == set(MONEY_SURFACE.get(name, ())), f"{name}: undeclared amount {priced}"
        assert set(spec.money) == priced, f"{name}: money={spec.money} does not match {priced}"


@pytest.mark.parametrize("tool_name,fields", sorted(MONEY_SURFACE.items()))
def test_a_declared_amount_is_checked_before_the_approval_ceremony(tool_name, fields):
    """A live-guarded or high-risk call is queued for approval without running its body, so
    an amount validated only in the body would be refused *after* a human approved it. The
    registry checks declared amounts before the propose/execute branch instead."""
    spec = REGISTRY[tool_name]
    assert set(spec.money) == set(fields)
    for field in fields:
        assert field in inspect.signature(spec.fn).parameters
