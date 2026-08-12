"""Orders, attendees and sales figures.

Fixtures use invented people and amounts — the point is the shaping and the arithmetic,
not realistic data.
"""

from __future__ import annotations

import pytest

from pretix_agent_mcp.tools import orders  # noqa: F401  — importing registers the tools
from pretix_agent_mcp.validate import ValidationError

EVENT = {"slug": "conf27", "name": "Conf 27", "currency": "EUR", "live": False, "testmode": True}
ITEMS = [
    {"id": 1, "name": {"en": "Regular ticket"}, "variations": [{"id": 7, "value": {"en": "Student"}}]},
    {"id": 2, "name": {"en": "Workshop"}, "variations": []},
]


def position(**over):
    base = {
        "id": 101,
        "order": "ABC12",
        "positionid": 1,
        "canceled": False,
        "item": 1,
        "variation": 7,
        "price": "23.00",
        "attendee_name": "Ada Lovelace",
        "attendee_email": "ada@example.org",
        "street": "Test street 12",
        "answers": [{"question": 12, "question_identifier": "WY3TP9SL", "answer": "Vegan"}],
        "checkins": [],
        "subevent": None,
    }
    base.update(over)
    return base


def order(**over):
    base = {
        "code": "ABC12",
        "status": "p",
        "total": "23.00",
        "datetime": "2027-01-05T10:00:00Z",
        "expires": "2027-01-12T10:00:00Z",
        "payment_provider": "banktransfer",
        "email": "ada@example.org",
        "phone": "+491234567",
        "invoice_address": {"name": "Ada Lovelace", "street": "Test street 12", "city": "Testington"},
        "fees": [],
        "payments": [{"local_id": 1, "state": "confirmed", "amount": "23.00", "provider": "banktransfer"}],
        "refunds": [],
        "positions": [position()],
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _event_and_items(api):
    api.route("GET", "events/conf27", EVENT)
    api.page("GET", "events/conf27/items", ITEMS)


async def test_search_orders_shapes_and_filters(api, call):
    api.page("GET", "events/conf27/orders", [order(), order(code="DEF34", status="n", total="42.00")])
    result = await call("search_orders", event="conf27", status="p", email="ada@example.org")

    assert result["currency"] == "EUR"
    first = result["results"][0]
    assert first == {
        "code": "ABC12",
        "status": "p",
        "datetime": "2027-01-05T10:00:00Z",
        "expires": "2027-01-12T10:00:00Z",
        "total": "23.00",
        "payment_provider": "banktransfer",
        "item_count": 1,
    }
    # No PII in a list summary, whatever pretix sent.
    assert not {"email", "phone", "invoice_address", "positions"} & set(first)
    query = next(r.url.params for r in api.requests if r.url.path.endswith("/orders/"))
    assert query["status"] == "p" and query["email"] == "ada@example.org"
    assert query["ordering"] == "-datetime"


async def test_search_orders_rejects_unknown_status(call):
    with pytest.raises(ValidationError):
        await call("search_orders", event="conf27", status="r")


async def test_get_order_returns_positions_and_contact(api, call):
    checkin = {"list": 44, "type": "entry", "datetime": "2027-06-12T09:00:00Z"}
    api.route("GET", "events/conf27/orders/ABC12", order(positions=[position(checkins=[checkin])]))
    result = await call("get_order", event="conf27", code="abc12")

    assert result["code"] == "ABC12"
    assert result["email"] == "a***@example.org"  # redacted by the registry, not by us
    assert result["invoice_address"]["city"] == "***"
    pos = result["positions"][0]
    assert (pos["item_name"], pos["variation_name"]) == ("Regular ticket", "Student")
    assert pos["checked_in"] is True
    assert pos["answers"][0]["answer"] == "***"


async def test_search_attendees_filters_by_item_and_checkin(api, call):
    api.page("GET", "events/conf27/orderpositions", [position(), position(id=102, item=2, variation=None)])
    result = await call("search_attendees", event="conf27", item=2, has_checkin=False, search="ada")

    assert result["count"] == 2
    assert [p["item_name"] for p in result["results"]] == ["Regular ticket", "Workshop"]
    assert result["results"][0]["attendee_name"] == "A*** L***"
    query = next(r.url.params for r in api.requests if r.url.path.endswith("/orderpositions/"))
    assert (query["item"], query["has_checkin"], query["search"]) == ("2", "false", "ada")


async def test_sales_summary_aggregates_only(api, call):
    api.page(
        "GET",
        "events/conf27/orders",
        [
            order(),
            order(code="DEF34", status="n", total="42.00", positions=[position(id=1, price="42.00", item=2)]),
            order(code="GHI56", status="c", total="10.00", positions=[]),
            order(
                code="JKL78",
                status="p",
                total="0.10",
                refunds=[
                    {"local_id": 1, "state": "done", "amount": "5.00"},
                    {"local_id": 2, "state": "created", "amount": "99.00"},
                ],
                positions=[position(id=2, price="0.10", canceled=True)],
            ),
        ],
    )
    result = await call("sales_summary", event="conf27", by_item=True)

    assert result["revenue"] == {
        "paid": "23.10",  # Decimal, not float: 23.00 + 0.10
        "pending": "42.00",
        "expired": "0.00",
        "canceled": "10.00",
        "refunded": "5.00",  # only completed refunds
    }
    assert result["orders"] == {"pending": 1, "paid": 2, "expired": 0, "canceled": 1}
    assert result["tickets"] == 2  # canceled position not counted
    assert result["by_item"] == [
        {"item": 2, "tickets": 1, "revenue": "42.00", "name": "Workshop"},
        {"item": 1, "tickets": 1, "revenue": "23.00", "name": "Regular ticket"},
    ]
    assert result["scan"]["truncated"] is False
    # Aggregates only: no order codes, no attendees, no PII anywhere in the payload.
    assert "ABC12" not in repr(result)


async def test_sales_summary_reports_a_truncated_window(api, make_app):
    from pretix_agent_mcp.registry import REGISTRY, run_tool

    api.route(
        "GET",
        "events/conf27/orders",
        {"count": 9, "next": "https://example.org/next", "results": [order(), order()]},
    )
    app = make_app(SALES_SCAN_CAP="2")
    result = await run_tool(app, REGISTRY["sales_summary"], {"event": "conf27"})

    assert result["scan"] == {"orders_scanned": 2, "orders_matching": 9, "cap": 2, "truncated": True}
    assert "Partial" in result["note"]


async def test_write_paths_send_the_documented_bodies(api, call):
    api.route("POST", "events/conf27/orders/ABC12/mark_paid", order())
    api.route("POST", "events/conf27/orders/ABC12/extend", order(status="n", expires="2027-03-31"))
    api.route("PATCH", "events/conf27/orders/ABC12", order(comment="called the customer"))
    api.route("PATCH", "events/conf27/orderpositions/101", position(attendee_email="new@example.org"))
    api.route("POST", "events/conf27/orders/ABC12/resend_link", None)

    assert (await call("mark_order_paid", event="conf27", code="ABC12"))["marked_paid"]["status"] == "p"
    await call("extend_payment_deadline", event="conf27", code="ABC12", expires="2027-03-31")
    await call("add_order_comment", event="conf27", code="ABC12", comment="called the customer")
    await call("edit_attendee", event="conf27", position_id=101, attendee_email="new@example.org")
    await call("resend_order_email", event="conf27", code="ABC12")

    assert api.sent("POST", "events/conf27/orders/ABC12/mark_paid") == [{"send_email": True}]
    extend = api.sent("POST", "events/conf27/orders/ABC12/extend")
    assert extend == [{"expires": "2027-03-31", "force": False}]
    assert api.sent("PATCH", "events/conf27/orders/ABC12") == [{"comment": "called the customer"}]
    assert api.sent("PATCH", "events/conf27/orderpositions/101") == [{"attendee_email": "new@example.org"}]
    assert api.sent("POST", "events/conf27/orders/ABC12/resend_link") == [None]


async def test_edit_attendee_needs_a_field(call):
    with pytest.raises(ValidationError):
        await call("edit_attendee", event="conf27", position_id=101)


async def test_cancel_order_only_proposes(api, call):
    api.route("GET", "events/conf27/orders/ABC12", order())
    result = await call("cancel_order", event="conf27", code="ABC12", cancellation_fee="5.00")

    assert result["status"] == "awaiting_approval"
    assert "CANCEL order ABC12" in result["preview"]
    assert "keep 5.00 as a cancellation fee" in result["preview"]
    assert "a***@example.org" in result["preview"] and "ada@example.org" not in result["preview"]
    assert api.sent("POST", "events/conf27/orders/ABC12/mark_canceled") == []


async def test_refund_order_proposes_then_executes_after_approval(api, app):
    from pretix_agent_mcp.registry import REGISTRY, execute_approved, run_tool

    api.route("GET", "events/conf27/orders/ABC12", order())
    api.route(
        "POST",
        "events/conf27/orders/ABC12/refunds",
        {"local_id": 1, "state": "created", "amount": "23.00", "provider": "manual"},
        status=201,
    )
    args = {"event": "conf27", "code": "ABC12", "amount": "23.00", "payment": 1}
    proposed = await run_tool(app, REGISTRY["refund_order"], dict(args))

    assert "REFUND 23.00 on order ABC12 via provider 'manual'" in proposed["preview"]
    assert "banktransfer" in proposed["preview"]
    assert api.sent("POST", "events/conf27/orders/ABC12/refunds") == []

    app.pending.decide(proposed["pending_action_id"], "approved")
    executed = await execute_approved(app, proposed["pending_action_id"])

    assert executed["refund"]["amount"] == "23.00"
    assert api.sent("POST", "events/conf27/orders/ABC12/refunds") == [
        {
            "state": "created",
            "source": "admin",
            "amount": "23.00",
            "provider": "manual",
            "mark_canceled": False,
            "mark_pending": True,
            "payment": 1,
        }
    ]


async def test_refund_order_rejects_a_bogus_amount(api, call):
    api.route("GET", "events/conf27/orders/ABC12", order())
    for bad in ("-1.00", "twenty", "NaN"):
        with pytest.raises(ValidationError):
            await call("refund_order", event="conf27", code="ABC12", amount=bad)
    assert api.sent("POST", "events/conf27/orders/ABC12/refunds") == []
