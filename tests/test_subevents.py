"""Series dates: filters, the batch create (the north-star call), partial failure, delete guard."""

from __future__ import annotations

import httpx
import pytest

from pretix_agent_mcp import tools  # noqa: F401  — importing the package registers the tools
from pretix_agent_mcp.validate import ValidationError

SERIES = {
    "slug": "stammtisch",
    "name": {"en": "Stammtisch"},
    "live": False,
    "testmode": True,
    "has_subevents": True,
}


async def test_list_subevents_filters_and_shapes(api, call):
    api.page(
        "GET",
        "events/stammtisch/subevents",
        [{"id": 7, "name": {"en": "Stammtisch"}, "date_from": "2027-02-04T19:00:00+01:00", "active": True}],
    )
    result = await call("list_subevents", event="stammtisch", active=True, since="2027-01-01T00:00:00+01:00")
    assert result["results"][0] == {
        "id": 7,
        "name": "Stammtisch",
        "date_from": "2027-02-04T19:00:00+01:00",
        "active": True,
    }
    params = api.requests[-1].url.params
    assert params["active"] == "true"
    assert params["date_from_after"] == "2027-01-01T00:00:00+01:00"
    assert "with_availability_for" not in params


async def test_get_subevent(api, call):
    api.route("GET", "events/stammtisch/subevents/7", {"id": 7, "name": {"de": "Stammtisch"}})
    assert (await call("get_subevent", event="stammtisch", subevent_id=7))["name"] == "Stammtisch"


async def test_create_subevents_batch_with_quotas(api, call):
    api.route("GET", "events/stammtisch", SERIES)
    ids = iter([11, 12, 13])
    api.route_fn("POST", "events/stammtisch/subevents", lambda r: httpx.Response(201, json={"id": next(ids)}))
    api.route("POST", "events/stammtisch/quotas", {"id": 99, "size": 40})

    result = await call(
        "create_subevents",
        event="stammtisch",
        dates=["2027-02-04T19:00:00+01:00", "2027-03-04T19:00:00+01:00", "2027-04-01T19:00:00+02:00"],
        name="Stammtisch",
        duration_minutes=180,
        item_prices={"3": "0.00"},
        quota_size=40,
        quota_items=[3],
    )

    assert result["status"] == "ok"
    assert [entry["id"] for entry in result["created"]] == [11, 12, 13]
    assert all(entry["quota_id"] == 99 for entry in result["created"])

    posted = api.sent("POST", "events/stammtisch/subevents")
    assert len(posted) == 3
    assert posted[0]["date_from"] == "2027-02-04T19:00:00+01:00"
    assert posted[0]["date_to"] == "2027-02-04T22:00:00+01:00"
    assert posted[0]["item_price_overrides"] == [{"item": 3, "price": "0.00"}]
    assert posted[0]["variation_price_overrides"] == [] and posted[0]["meta_data"] == {}
    assert api.sent("POST", "events/stammtisch/quotas")[0] == {
        "name": "Stammtisch",
        "size": 40,
        "items": [3],
        "subevent": 11,
    }


async def test_create_subevents_reports_partial_success(api, call):
    api.route("GET", "events/stammtisch", SERIES)
    calls = iter([httpx.Response(201, json={"id": 11}), httpx.Response(400, json={"date_from": ["bad"]})])
    api.route_fn("POST", "events/stammtisch/subevents", lambda r: next(calls))

    result = await call(
        "create_subevents",
        event="stammtisch",
        dates=["2027-02-04T19:00:00+01:00", "2027-03-04T19:00:00+01:00", "2027-04-01T19:00:00+02:00"],
        name="Stammtisch",
    )

    assert result["status"] == "partial_failure"
    assert [entry["id"] for entry in result["created"]] == [11]
    assert result["failed_at"] == "2027-03-04T19:00:00+01:00"
    assert result["remaining"] == ["2027-04-01T19:00:00+02:00"]
    assert "400" in result["error"]


async def test_create_subevents_rejects_bad_input(api, call):
    api.route("GET", "events/stammtisch", SERIES)
    with pytest.raises(ValidationError):
        await call("create_subevents", event="stammtisch", dates=["next thursday"], name="Stammtisch")
    with pytest.raises(ValidationError):  # quota_size without the products it covers
        await call(
            "create_subevents",
            event="stammtisch",
            dates=["2027-02-04T19:00:00+01:00"],
            name="Stammtisch",
            quota_size=40,
        )
    assert api.sent("POST", "events/stammtisch/subevents") == []


async def test_update_subevent(api, call):
    api.route("GET", "events/stammtisch", SERIES)
    api.route("PATCH", "events/stammtisch/subevents/7", {"id": 7, "active": False})
    result = await call(
        "update_subevent", event="stammtisch", subevent_id=7, active=False, item_prices={"3": "5.00"}
    )
    assert result["changed"] == ["active", "item_price_overrides"]
    assert api.sent("PATCH", "events/stammtisch/subevents/7")[0] == {
        "active": False,
        "item_price_overrides": [{"item": 3, "price": "5.00"}],
    }


async def test_create_subevents_on_live_event_awaits_approval(api, call):
    api.route("GET", "events/stammtisch", dict(SERIES, live=True, testmode=False))
    result = await call(
        "create_subevents", event="stammtisch", dates=["2027-02-04T19:00:00+01:00"], name="Stammtisch"
    )
    assert result["status"] == "awaiting_approval"
    assert api.sent("POST", "events/stammtisch/subevents") == []


async def test_delete_subevent_previews_sold_tickets(api, call):
    api.route("GET", "events/stammtisch/subevents/7", {"id": 7, "name": {"en": "Stammtisch"}, "active": True})
    api.route("GET", "events/stammtisch/orderpositions", {"count": 12, "next": None, "results": []})
    api.route("GET", "events/stammtisch/orders", {"count": 9, "next": None, "results": []})

    result = await call("delete_subevent", event="stammtisch", subevent_id=7)

    assert result["status"] == "awaiting_approval"
    assert "order positions referencing it: 12" in result["preview"]
    assert "orders containing it:           9" in result["preview"]
    assert api.sent("DELETE", "events/stammtisch/subevents/7") == []
