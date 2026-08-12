"""The north-star scenario: run a season without opening the pretix UI.

Clone last year's edition, adjust it, add series dates, publish it (one approval),
answer a sales question, refund an order (one approval). Everything else is friction-free.
"""

from __future__ import annotations

from pretix_agent_mcp.cli import main
from pretix_agent_mcp.registry import REGISTRY, run_tool

CONF26 = {"slug": "conf26", "name": {"en": "Conf 26"}, "live": True, "testmode": False}
CONF27 = {"slug": "conf27", "name": {"en": "Conf 27"}, "live": False, "testmode": True, "currency": "EUR"}


async def call(app, tool_name, /, **kwargs):
    return await run_tool(app, REGISTRY[tool_name], kwargs)


def approve(app, monkeypatch, action_id: str) -> None:
    """What the operator does on the server — the whole ceremony."""
    monkeypatch.setattr("pretix_agent_mcp.cli.load", lambda config_file=None: app.cfg)
    monkeypatch.setattr("pretix_agent_mcp.cli.build_app", lambda cfg: app)
    assert main(["approve", action_id]) == 0


async def test_a_season_without_the_web_ui(app, api, monkeypatch):
    # 1. "Clone this year's conference for 2027, June 12-14, early-bird until March."
    api.route("POST", "events/conf26/clone", CONF27)
    clone = await call(
        app,
        "clone_event",
        source_event="conf26",
        event="conf27",
        name="Conf 27",
        date_from="2027-06-12T09:00:00+02:00",
        date_to="2027-06-14T18:00:00+02:00",
        presale_end="2027-03-01T00:00:00+01:00",
    )
    assert clone["created"]["live"] is False, "a new edition starts as a draft"

    # 2. Reconfigure the draft — no approval, no friction.
    api.route("GET", "events/conf27", CONF27)
    api.route("PATCH", "events/conf27", {**CONF27, "location": "Wien"})
    assert (await call(app, "update_event", event="conf27", location="Wien"))["changed"] == ["location"]

    # 3. "Add stammtisch dates for every first Thursday until year end, 40 seats each."
    api.route("POST", "events/conf27/subevents", {"id": 11, "name": {"en": "Stammtisch"}})
    api.route("POST", "events/conf27/quotas", {"id": 21, "name": "Stammtisch", "size": 40})
    series = await call(
        app,
        "create_subevents",
        event="conf27",
        dates=["2027-09-02T19:00:00+02:00", "2027-10-07T19:00:00+02:00"],
        name="Stammtisch",
        quota_size=40,
        quota_items=[3],
    )
    assert series["status"] == "ok" and len(series["created"]) == 2

    # 4. "Take the 2027 conference live." — the one moment a human is in the loop.
    api.route("PATCH", "events/conf27", {**CONF27, "live": True})
    proposal = await call(app, "publish_event", event="conf27")
    assert proposal["status"] == "awaiting_approval"
    assert api.sent("PATCH", "events/conf27")[-1] == {"location": "Wien"}, "not published yet"
    approve(app, monkeypatch, proposal["pending_action_id"])
    published = await call(app, "execute_pending_action", pending_action_id=proposal["pending_action_id"])
    assert published["published"]["slug"] == "conf27"

    # 5. "Sales for the workshop?" — numbers, no PII, no dashboard login.
    api.page(
        "GET",
        "events/conf27/orders",
        [
            {"code": "ABC12", "status": "p", "total": "42.00", "positions": [{"item": 3, "price": "42.00"}]},
            {"code": "DEF34", "status": "n", "total": "42.00", "positions": [{"item": 3, "price": "42.00"}]},
        ],
    )
    summary = await call(app, "sales_summary", event="conf27")
    assert summary["scan"]["orders_scanned"] == 2
    assert summary["orders"]["paid"] == 1 and summary["orders"]["pending"] == 1
    assert "@" not in repr(summary), "a sales answer must carry no personal data"

    # 6. "Refund order ABC12." — the other moment a human is in the loop.
    api.route("GET", "events/conf27/orders/ABC12", {"code": "ABC12", "status": "p", "total": "42.00"})
    api.route(
        "POST", "events/conf27/orders/ABC12/refunds", {"local_id": 1, "amount": "42.00", "state": "done"}
    )
    refund = await call(app, "refund_order", event="conf27", code="ABC12", amount="42.00")
    assert refund["status"] == "awaiting_approval"
    assert api.sent("POST", "events/conf27/orders/ABC12/refunds") == [], "no money moves before approval"
    approve(app, monkeypatch, refund["pending_action_id"])
    await call(app, "execute_pending_action", pending_action_id=refund["pending_action_id"])
    assert len(api.sent("POST", "events/conf27/orders/ABC12/refunds")) == 1

    # Exactly two approvals for a whole season.
    audit = app.cfg.audit_log.read_text().splitlines()
    assert len([line for line in audit if '"approved"' in line]) == 2
