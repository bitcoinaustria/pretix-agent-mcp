"""Vouchers: listing, single create, the batch endpoint, validation, and the delete guard."""

from __future__ import annotations

import pytest

from pretix_agent_mcp import tools  # noqa: F401  — importing the package registers the tools
from pretix_agent_mcp.validate import ValidationError


async def test_list_vouchers(api, call):
    api.page(
        "GET",
        "events/conf27/vouchers",
        [
            {
                "id": 1,
                "code": "SPONSOR1",
                "item": 3,
                "price_mode": "set",
                "value": "0.00",
                "redeemed": 1,
                "max_usages": 2,
                "tag": "sponsors",
                "secret": "should-not-appear",
            }
        ],
    )
    result = await call("list_vouchers", event="conf27", tag="sponsors")
    assert result["results"][0]["code"] == "SPONSOR1"
    assert "secret" not in result["results"][0]  # output is shaped, not dumped
    assert api.requests[-1].url.params["tag"] == "sponsors"


async def test_create_voucher(api, call):
    api.route("POST", "events/conf27/vouchers", {"id": 5, "code": "FREEBIE01", "price_mode": "set"})
    result = await call(
        "create_voucher",
        event="conf27",
        code="FREEBIE01",
        item=3,
        price_mode="set",
        value="0.00",
        valid_until="2027-12-31T23:59:59+01:00",
        tag="press",
    )
    assert result["created"]["code"] == "FREEBIE01"
    assert api.sent("POST", "events/conf27/vouchers")[0] == {
        "code": "FREEBIE01",
        "price_mode": "set",
        "value": "0.00",
        "item": 3,
        "max_usages": 1,
        "valid_until": "2027-12-31T23:59:59+01:00",
        "tag": "press",
        "block_quota": False,
    }


async def test_create_vouchers_batch_lets_pretix_generate_codes(api, call):
    api.route(
        "POST",
        "events/conf27/vouchers/batch_create",
        [{"id": 1, "code": "AAAAA1"}, {"id": 2, "code": "BBBBB2"}, {"id": 3, "code": "CCCCC3"}],
        status=201,
    )
    result = await call(
        "create_vouchers_batch",
        event="conf27",
        count=3,
        item=3,
        price_mode="percent",
        value="50.00",
        tag="early",
    )
    assert result["created_count"] == 3
    assert result["codes"] == ["AAAAA1", "BBBBB2", "CCCCC3"]

    body = api.sent("POST", "events/conf27/vouchers/batch_create")[0]
    assert len(body) == 3
    assert all("code" not in voucher for voucher in body)  # omitted => pretix generates
    assert body[0]["price_mode"] == "percent" and body[0]["tag"] == "early"


async def test_create_vouchers_batch_with_explicit_codes(api, call):
    api.route("POST", "events/conf27/vouchers/batch_create", [{"id": 1, "code": "SPONSOR-ACME"}], status=201)
    await call("create_vouchers_batch", event="conf27", codes=["SPONSOR-ACME"], quota=8)
    assert api.sent("POST", "events/conf27/vouchers/batch_create")[0][0]["code"] == "SPONSOR-ACME"


async def test_voucher_input_is_validated_before_anything_is_sent(api, call):
    with pytest.raises(ValidationError):  # not a pretix price mode
        await call("create_voucher", event="conf27", item=3, price_mode="half_off", value="5.00")
    with pytest.raises(ValidationError):  # valid_until must be an ISO 8601 string
        await call("create_voucher", event="conf27", item=3, valid_until="end of the year")
    with pytest.raises(ValidationError):  # item and quota are mutually exclusive
        await call("create_voucher", event="conf27", item=3, quota=8)
    with pytest.raises(ValidationError):  # exactly one of count/codes
        await call("create_vouchers_batch", event="conf27", count=3, codes=["ABCDEF"])
    with pytest.raises(ValidationError):  # pretix rejects a batch with duplicate codes
        await call("create_vouchers_batch", event="conf27", codes=["ABCDEF", "ABCDEF"])
    assert api.sent("POST", "events/conf27/vouchers") == []
    assert api.sent("POST", "events/conf27/vouchers/batch_create") == []


async def test_delete_voucher_needs_approval_and_shows_redemptions(api, call):
    api.route(
        "GET",
        "events/conf27/vouchers/5",
        {"id": 5, "code": "FREEBIE01", "price_mode": "set", "value": "0.00", "redeemed": 1, "max_usages": 1},
    )
    result = await call("delete_voucher", event="conf27", voucher_id=5)
    assert result["status"] == "awaiting_approval"
    assert "redeemed 1 of 1 usages" in result["preview"]
    assert api.sent("DELETE", "events/conf27/vouchers/5") == []
