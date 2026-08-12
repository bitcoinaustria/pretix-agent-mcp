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


async def test_publish_preview_is_a_go_live_checklist(api, call):
    """A real pretix refuses to take an event live when it has a paid product and no payment
    provider — and that used to surface *after* a human approved the publish, spending the
    one manual step in the system on a doomed action. The preview reports it instead."""
    api.route("GET", "events/conf27", DRAFT)
    api.page("GET", "events/conf27/items", [{"id": 3, "name": {"en": "Ticket"}, "default_price": "42.00"}])
    api.page("GET", "events/conf27/quotas", [{"id": 1, "name": "Seats", "size": 100}])

    result = await call("publish_event", event="conf27")

    assert result["status"] == "awaiting_approval"
    preview = result["preview"]
    assert "products: 1 (1 priced above zero)" in preview
    assert "quotas:   1" in preview
    assert "pretix refuses to go live unless a payment provider is enabled" in preview
    # Never asserted as fact: pretix exposes core settings only, so a correctly configured
    # bank transfer or Stripe is invisible here. Claiming "none" from that silence was a lie.
    assert "providers enabled: none" not in preview
    assert api.sent("PATCH", "events/conf27") == []


async def test_publish_preview_warns_only_about_what_it_can_see(api, call):
    """Missing products or quotas are facts the API does state, so those are WARNINGs. The
    payment precondition is a NOTE, because this server cannot verify it either way."""
    api.route("GET", "events/conf27", DRAFT)
    api.page("GET", "events/conf27/items", [])
    api.page("GET", "events/conf27/quotas", [])

    preview = (await call("publish_event", event="conf27"))["preview"]

    assert "WARNING: no products" in preview
    assert "WARNING: no quotas" in preview
    assert "NOTE" not in preview, "no priced products, so the payment note is not due"


async def test_a_free_event_needs_no_payment_provider(api, call):
    """A meetup at 0.00 goes live with no provider configured, so no warning is due."""
    api.route("GET", "events/conf27", DRAFT)
    api.page("GET", "events/conf27/items", [{"id": 3, "name": {"en": "Free"}, "default_price": "0.00"}])
    api.page("GET", "events/conf27/quotas", [{"id": 1, "name": "Seats", "size": 40}])

    preview = (await call("publish_event", event="conf27"))["preview"]

    assert "products: 1 (0 priced above zero)" in preview
    assert "WARNING" not in preview
    assert "NOTE" not in preview, "a free event needs no payment provider"


async def test_publish_preview_survives_what_it_cannot_read(api, call):
    """The preview informs the operator; it must never be the reason a proposal fails. A
    token that cannot read settings gets no payment line rather than a wrong 'none'."""
    api.route("GET", "events/conf27", DRAFT)
    api.route("GET", "events/conf27/items", {"detail": "forbidden"}, status=403)
    api.route("GET", "events/conf27/quotas", {"detail": "forbidden"}, status=403)

    result = await call("publish_event", event="conf27")

    assert result["status"] == "awaiting_approval"
    assert "LIVE" in result["preview"]
    assert "products:" not in result["preview"], "unknown is reported as absent, not as zero"


async def test_payment_configuration_is_refused(api, call):
    """The mail_bcc argument, for money: an agent that can rewrite an IBAN or a provider's
    API key redirects every customer payment into an account it chose. Ordinary `write` on a
    draft event, so no approval covers it — it has to be refused outright."""
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27/settings", {})
    for key in (
        "payment_banktransfer__enabled",
        "payment_banktransfer_bank_details_sepa_iban",
        "payment_banktransfer_bank_details",
        "payment_stripe_secret_key",
        "payment_stripe_publishable_key",
        "payment_paypal_merchant_id",
        "payment_btcpay_api_key",
        "payment_btcpay_url",
    ):
        with pytest.raises(ValidationError, match="payment configuration"):
            await call("update_event_settings", event="conf27", settings={key: "attacker-controlled"})
    assert api.sent("PATCH", "events/conf27/settings") == []


async def test_a_credential_shaped_setting_is_refused_whatever_it_is_called(api, call):
    """Third-party payment plugins invent their own setting names, so the credential check
    is on the shape of the name rather than a list of known providers."""
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27/settings", {})
    for key in ("some_plugin_api_key", "acme_webhook_secret", "gateway_private_key", "x_access_token"):
        with pytest.raises(ValidationError, match="payment configuration"):
            await call("update_event_settings", event="conf27", settings={key: "nope"})
    assert api.sent("PATCH", "events/conf27/settings") == []


async def test_payment_terms_are_still_agent_writable(api, call):
    """A deadline moves nobody's money, and setting one is ordinary event administration."""
    api.route("GET", "events/conf27", DRAFT)
    api.route("PATCH", "events/conf27/settings", {"payment_term_days": 14, "payment_explanation": "Bank"})

    result = await call(
        "update_event_settings",
        event="conf27",
        settings={"payment_term_days": 14, "payment_explanation": "Bank"},
    )

    assert result["changed"] == ["payment_explanation", "payment_term_days"]
    assert api.sent("PATCH", "events/conf27/settings") == [
        {"payment_term_days": 14, "payment_explanation": "Bank"}
    ]
