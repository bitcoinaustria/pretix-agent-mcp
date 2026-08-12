"""Event lifecycle tools: the "never open the UI" path for a new edition."""

from __future__ import annotations

import pytest

from pretix_agent_mcp.validate import ValidationError

DRAFT = {"slug": "conf27", "name": {"en": "Conf 27"}, "live": False, "testmode": True, "currency": "EUR"}


async def test_list_events_is_shaped_not_dumped(api, call):
    api.page("GET", "events", [{**DRAFT, "seat_category_mapping": {"a": 1}, "internal_field": "x"}])
    result = await call("list_events")
    assert result["results"] == [
        {"slug": "conf27", "name": "Conf 27", "live": False, "testmode": True, "currency": "EUR"}
    ]


async def test_object_names_are_not_masked(api, call):
    """Event names are labels, not personal data — masking them would be useless output."""
    api.page("GET", "events", [DRAFT])
    result = await call("list_events")
    assert result["results"][0]["name"] == "Conf 27"


async def test_get_event_rejects_a_path_escape_before_any_request(api, call):
    with pytest.raises(ValidationError):
        await call("get_event", event="../other-org/events")
    assert api.requests == []


async def test_create_event_is_always_draft_and_test_mode(api, call):
    api.route("POST", "events", DRAFT)
    result = await call("create_event", event="conf27", name="Conf 27", date_from="2027-06-12T09:00:00+02:00")
    sent = api.sent("POST", "events")[0]
    assert sent["live"] is False and sent["testmode"] is True
    assert result["url"].endswith("/demo/conf27/")


async def test_clone_event_uses_the_clone_endpoint(api, call):
    api.route("POST", "events/conf26/clone", {**DRAFT, "slug": "conf27"})
    result = await call(
        "clone_event",
        source_event="conf26",
        event="conf27",
        name="Conf 27",
        date_from="2027-06-12T09:00:00+02:00",
        presale_end="2027-03-01T00:00:00+01:00",
    )
    assert result["cloned_from"] == "conf26"
    assert api.sent("POST", "events/conf26/clone")[0]["presale_end"] == "2027-03-01T00:00:00+01:00"


async def test_update_event_only_sends_what_was_passed(api, call):
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", DRAFT)
    await call("update_event", event="conf27", location="Wien")
    assert api.sent("PATCH", "events/conf27") == [{"location": "Wien"}]


async def test_empty_update_is_rejected(api, call):
    api.route("GET", "events/conf27", DRAFT)
    with pytest.raises(ValidationError, match="nothing to update"):
        await call("update_event", event="conf27")


async def test_update_event_cannot_take_an_event_live(api, call):
    """live=true is publish_event's job, and that is always approved out of band."""
    import inspect

    from pretix_agent_mcp.registry import REGISTRY

    assert "live" not in inspect.signature(REGISTRY["update_event"].fn).parameters


async def test_settings_can_be_fetched_selectively(api, call):
    api.route(
        "GET", "events/conf27/settings", {"waiting_list_enabled": True, "mail_text_order_free": {"en": "hi"}}
    )
    result = await call("get_event_settings", event="conf27", keys=["waiting_list_enabled"])
    assert result["settings"] == {"waiting_list_enabled": True}


async def test_tax_rule_crud(api, call):
    api.route("GET", "events/conf27", DRAFT)
    api.route("POST", "events/conf27/taxrules", {"id": 1, "name": "20% VAT", "rate": "20.00"})
    result = await call("create_tax_rule", event="conf27", name="20% VAT", rate="20.00")
    assert result["created"]["rate"] == "20.00"


async def test_publish_leaves_test_mode(api, call):
    """live=true alone still makes test orders; leaving test mode is what makes them real,
    so publish_event does both — and it is the only tool that can."""
    import inspect

    from pretix_agent_mcp.registry import REGISTRY

    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27", {**DRAFT, "live": True, "testmode": False})
    proposal = await call("publish_event", event="conf27")
    assert proposal["status"] == "awaiting_approval"

    for name, spec in REGISTRY.items():
        if name == "publish_event":
            continue
        parameters = inspect.signature(spec.fn, eval_str=True).parameters
        assert "testmode" not in parameters, f"{name} could leave test mode without approval"


async def test_mail_routing_settings_are_refused(api, call):
    """An agent that can set mail_bcc copies every customer mail off-box, and redaction
    would never see it — the data never enters the agent's context."""
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27/settings", {})
    for key in ("mail_bcc", "mail_from", "mail_reply_to", "smtp_host", "mail_bcc_extra"):
        with pytest.raises(ValidationError, match="mail routing"):
            await call("update_event_settings", event="conf27", settings={key: "attacker@example.net"})
    assert api.sent("PATCH", "events/conf27/settings") == []


async def test_settings_without_keys_returns_names_only(api, call):
    """The full settings object is tens of kilobytes of mail templates per language."""
    api.route(
        "GET", "events/conf27/settings", {"waiting_list_enabled": True, "mail_text_order_free": "x" * 5000}
    )
    result = await call("get_event_settings", event="conf27")
    assert result["available_keys"] == ["mail_text_order_free", "waiting_list_enabled"]
    assert "settings" not in result
