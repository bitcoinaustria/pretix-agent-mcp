"""Check-in list and waiting-list tools."""

from __future__ import annotations

import pytest

from pretix_agent_mcp.registry import REGISTRY, run_tool
from pretix_agent_mcp.tools import checkin  # noqa: F401  - registers the tools
from pretix_agent_mcp.validate import ValidationError


@pytest.fixture
def call_args(app):
    """Like the ``call`` fixture, but takes the arguments as a dict — needed for tools
    with a ``name`` parameter, which collides with ``call``'s own first argument."""

    async def _call(tool: str, args: dict) -> dict:
        return await run_tool(app, REGISTRY[tool], dict(args))

    return _call


LIST = {
    "id": 1,
    "name": "Main entrance",
    "all_products": True,
    "limit_products": [],
    "subevent": None,
    "include_pending": False,
    "position_count": 42,
    "checkin_count": 17,
}


async def test_list_checkin_lists(api, call):
    api.page("GET", "events/conf27/checkinlists", [LIST])
    result = await call("list_checkin_lists", event="conf27")
    entry = result["results"][0]
    assert (entry["id"], entry["checkin_count"], entry["position_count"]) == (1, 17, 42)
    assert entry["all_products"] is True
    assert entry["name"] == "Main entrance"  # an object label, not a person: not masked


async def test_create_and_update_checkin_list(api, call_args, call):
    api.route("POST", "events/conf27/checkinlists", LIST | {"id": 2, "name": "VIP", "all_products": False})
    created = await call_args(
        "create_checkin_list",
        {"event": "conf27", "name": "VIP", "all_products": False, "limit_products": [3, 4]},
    )
    assert created["created"]["id"] == 2
    assert api.sent("POST", "events/conf27/checkinlists") == [
        {"name": "VIP", "all_products": False, "limit_products": [3, 4], "include_pending": False}
    ]

    api.route("PATCH", "events/conf27/checkinlists/2", LIST | {"id": 2, "include_pending": True})
    updated = await call("update_checkin_list", event="conf27", checkin_list_id=2, include_pending=True)
    assert updated["changed"] == ["include_pending"]
    assert api.sent("PATCH", "events/conf27/checkinlists/2") == [{"include_pending": True}]


async def test_create_checkin_list_needs_products_when_not_all(call_args):
    with pytest.raises(ValidationError):
        await call_args("create_checkin_list", {"event": "conf27", "name": "VIP", "all_products": False})


async def test_update_checkin_list_rejects_bad_id(call_args):
    with pytest.raises(ValidationError):
        await call_args("update_checkin_list", {"event": "conf27", "checkin_list_id": "1/../2", "name": "x"})


async def test_delete_checkin_list_awaits_approval(api, call):
    api.route("GET", "events/conf27/checkinlists/1", LIST)
    result = await call("delete_checkin_list", event="conf27", checkin_list_id=1)
    assert result["status"] == "awaiting_approval"
    assert "17 of 42" in result["preview"]
    assert api.sent("DELETE", "events/conf27/checkinlists/1") == []


async def test_list_checkins(api, call):
    api.page(
        "GET",
        "events/conf27/checkinlists/1/positions",
        [
            {
                "id": 23442,
                "order": "ABC12",
                "positionid": 1,
                "item": 1345,
                "variation": None,
                "attendee_name": "Peter Panne",
                "secret": "z3fsn8jyufm5kpk768q69gkbyr5f4h6w",
                "checkins": [
                    {"list": 1, "type": "entry", "datetime": "2027-06-12T09:12:00Z"},
                    {"list": 1, "type": "exit", "datetime": "2027-06-12T11:30:00Z"},
                ],
            },
            {"id": 23443, "order": "ABC13", "positionid": 2, "item": 1345, "checkins": []},
        ],
    )
    result = await call("list_checkins", event="conf27", checkin_list_id=1, checked_in=True, search="Panne")
    first, second = result["results"]
    assert first["order"] == "ABC12"
    assert (first["checked_in"], first["checkin_time"], first["checkin_type"]) == (
        True,
        "2027-06-12T11:30:00Z",
        "exit",
    )
    assert second["checked_in"] is False and second["checkin_time"] is None
    assert "secret" not in first
    assert first["attendee_name"] == "P*** P***"  # registry redaction, not the tool's job

    query = api.requests[-1].url.params
    assert query["has_checkin"] == "true" and query["search"] == "Panne"


async def test_list_waiting_list(api, call):
    api.page(
        "GET",
        "events/conf27/waitinglistentries",
        [
            {
                "id": 7,
                "created": "2027-01-02T10:00:00Z",
                "email": "someone@example.org",
                "item": 1345,
                "variation": None,
                "subevent": None,
                "voucher": 12,
            }
        ],
    )
    result = await call("list_waiting_list", event="conf27", has_voucher=True)
    entry = result["results"][0]
    assert entry["id"] == 7 and entry["voucher_sent"] is True
    assert entry["email"] == "s***@example.org"
    assert api.requests[-1].url.params["has_voucher"] == "true"


async def test_send_waiting_list_voucher(api, call):
    api.route("POST", "events/conf27/waitinglistentries/7/send_voucher", status=204)
    result = await call("send_waiting_list_voucher", event="conf27", entry_id=7)
    assert result == {"voucher_sent": True, "entry_id": 7}
    assert api.sent("POST", "events/conf27/waitinglistentries/7/send_voucher") == [None]


async def test_send_waiting_list_voucher_rejects_bad_entry_id(call):
    with pytest.raises(ValidationError):
        await call("send_waiting_list_voucher", event="conf27", entry_id=0)
