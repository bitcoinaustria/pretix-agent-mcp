"""Catalog tools: shaping, decimal-string prices, the live guard and the delete previews."""

from __future__ import annotations

import pytest

from pretix_agent_mcp.tools import catalog  # noqa: F401  (registers the tools)
from pretix_agent_mcp.validate import ValidationError

EVENT = "events/conf27"
DRAFT = {"slug": "conf27", "name": "Conf 27", "live": False, "testmode": True}
LIVE = {"slug": "conf27", "name": "Conf 27", "live": True, "testmode": False}


def draft(api):
    api.route("GET", EVENT, DRAFT)


@pytest.fixture
def run(make_app):
    """Call a tool through the registry gate.

    Not conftest's ``call``: its first parameter is named ``name``, which collides with the
    ``name=`` argument of every create_* tool here. PII_MODE=full because the redactor masks
    any key called ``name`` — including product and quota names, which are not PII.
    """
    from pretix_agent_mcp.registry import REGISTRY, run_tool

    app = make_app(PII_MODE="full")

    async def _run(tool_name, /, **kwargs):
        return await run_tool(app, REGISTRY[tool_name], kwargs)

    return _run


async def test_list_products_shapes_variations(api, run):
    api.page(
        "GET",
        f"{EVENT}/items",
        [
            {
                "id": 1,
                "name": {"en": "Regular"},
                "default_price": "23.00",
                "active": True,
                "category": 4,
                "tax_rule": 2,
                "admission": True,
                "picture": "ignored",
                "variations": [
                    {"id": 9, "value": {"en": "Student"}, "default_price": "12.00", "active": True}
                ],
            }
        ],
    )
    result = await run("list_products", event="conf27")
    product = result["results"][0]
    assert product == {
        "id": 1,
        "name": "Regular",
        "default_price": "23.00",
        "active": True,
        "category": 4,
        "tax_rule": 2,
        "admission": True,
        "variations": [{"id": 9, "value": "Student", "default_price": "12.00", "active": True}],
    }


async def test_get_product_includes_addons(api, run):
    api.route(
        "GET",
        f"{EVENT}/items/1",
        {
            "id": 1,
            "name": {"en": "Regular"},
            "default_price": "23.00",
            "hide_without_voucher": False,
            "variations": [{"id": 9, "value": {"en": "Student"}, "position": 1}],
            "addons": [{"addon_category": 7, "min_count": 0, "max_count": 2, "price_included": False}],
        },
    )
    result = await run("get_product", event="conf27", item_id=1)
    assert result["variations"] == [{"id": 9, "value": "Student", "position": 1}]
    assert result["addons"] == [
        {"addon_category": 7, "min_count": 0, "max_count": 2, "price_included": False}
    ]


async def test_get_availability_flags_sold_out(api, run):
    api.page(
        "GET",
        f"{EVENT}/quotas",
        [
            {
                "id": 1,
                "name": "Regular",
                "size": 100,
                "items": [1],
                "available": False,
                "available_number": 0,
                "total_size": 100,
                "paid_orders": 95,
                "pending_orders": 3,
                "cart_positions": 2,
                "blocking_vouchers": 0,
                "waiting_list": 11,
            },
            {
                "id": 2,
                "name": "Workshop",
                "size": None,
                "items": [2],
                "available": True,
                "available_number": None,
            },
        ],
    )
    result = await run("get_availability", event="conf27")
    assert result["sold_out"] == ["Regular"]
    assert result["results"][0]["paid_orders"] == 95
    assert result["results"][0]["waiting_list"] == 11
    assert api.requests[0].url.params["with_availability"] == "true"


async def test_get_availability_single_quota(api, run):
    api.route("GET", f"{EVENT}/quotas/7", {"id": 7, "name": "VIP", "size": 10, "available_number": 4})
    result = await run("get_availability", event="conf27", quota_id=7)
    assert result["quota"]["available_number"] == 4


async def test_list_questions_flattens_options(api, run):
    api.page(
        "GET",
        f"{EVENT}/questions",
        [
            {
                "id": 3,
                "question": {"en": "T-shirt size"},
                "type": "C",
                "required": True,
                "items": [1],
                "options": [{"id": 5, "identifier": "s", "answer": {"en": "S"}}],
            }
        ],
    )
    result = await run("list_questions", event="conf27")
    assert result["results"][0]["question"] == "T-shirt size"
    # `label`, not pretix's `answer` — the redactor treats `answer` as customer free text.
    assert result["results"][0]["options"] == [{"id": 5, "identifier": "s", "label": "S"}]


async def test_list_categories(api, run):
    api.page("GET", f"{EVENT}/categories", [{"id": 4, "name": {"en": "Tickets"}, "is_addon": False}])
    result = await run("list_categories", event="conf27")
    assert result["results"] == [{"id": 4, "name": "Tickets", "is_addon": False}]


async def test_create_product_sends_decimal_price(api, run):
    draft(api)
    api.route(
        "POST", f"{EVENT}/items", {"id": 1, "name": {"en": "Regular"}, "default_price": "23.00"}, status=201
    )
    await run(
        "create_product", event="conf27", name="Regular", default_price="23.00", category=4, admission=True
    )
    assert api.sent("POST", f"{EVENT}/items") == [
        {
            "name": "Regular",
            "default_price": "23.00",
            "category": 4,
            "active": True,
            "admission": True,
            "hide_without_voucher": False,
        }
    ]


async def test_update_product_sends_only_passed_fields(api, run):
    draft(api)
    api.route("PATCH", f"{EVENT}/items/1", {"id": 1, "default_price": "25.00"})
    result = await run("update_product", event="conf27", item_id=1, default_price="25.00")
    assert api.sent("PATCH", f"{EVENT}/items/1") == [{"default_price": "25.00"}]
    assert result["changed"] == ["default_price"]


async def test_create_variation_uses_nested_path(api, run):
    draft(api)
    api.route("POST", f"{EVENT}/items/1/variations", {"id": 9, "value": {"en": "Student"}}, status=201)
    await run("create_product_variation", event="conf27", item_id=1, value="Student", default_price="12.00")
    assert api.sent("POST", f"{EVENT}/items/1/variations") == [
        {"value": "Student", "default_price": "12.00", "active": True, "hide_without_voucher": False}
    ]


async def test_update_variation_uses_nested_path(api, run):
    draft(api)
    api.route("PATCH", f"{EVENT}/items/1/variations/9", {"id": 9, "default_price": "14.00"})
    await run("update_product_variation", event="conf27", item_id=1, variation_id=9, default_price="14.00")
    assert api.sent("PATCH", f"{EVENT}/items/1/variations/9") == [{"default_price": "14.00"}]


async def test_create_quota_keeps_null_size_for_unlimited(api, run):
    draft(api)
    api.route("POST", f"{EVENT}/quotas", {"id": 1, "name": "Workshop", "size": None}, status=201)
    await run("create_quota", event="conf27", name="Workshop", items=[1, 2])
    assert api.sent("POST", f"{EVENT}/quotas") == [{"name": "Workshop", "items": [1, 2], "size": None}]


async def test_create_quota_keeps_zero_size(api, run):
    draft(api)
    api.route("POST", f"{EVENT}/quotas", {"id": 1, "name": "Blocked", "size": 0}, status=201)
    await run("create_quota", event="conf27", name="Blocked", size=0, items=[1])
    assert api.sent("POST", f"{EVENT}/quotas")[0]["size"] == 0


async def test_update_quota_unlimited(api, run):
    draft(api)
    api.route("PATCH", f"{EVENT}/quotas/1", {"id": 1, "size": None})
    await run("update_quota", event="conf27", quota_id=1, unlimited=True)
    assert api.sent("PATCH", f"{EVENT}/quotas/1") == [{"size": None}]


async def test_create_question_with_options(api, run):
    draft(api)
    api.route("POST", f"{EVENT}/questions", {"id": 3, "question": {"en": "Size"}, "type": "C"}, status=201)
    await run("create_question", event="conf27", question="Size", type="C", required=True, options=["S", "M"])
    assert api.sent("POST", f"{EVENT}/questions") == [
        {
            "question": "Size",
            "type": "C",
            "required": True,
            "ask_during_checkin": False,
            "options": [{"answer": "S"}, {"answer": "M"}],
        }
    ]


async def test_create_category(api, run):
    draft(api)
    api.route(
        "POST", f"{EVENT}/categories", {"id": 4, "name": {"en": "Add-ons"}, "is_addon": True}, status=201
    )
    result = await run("create_category", event="conf27", name="Add-ons", is_addon=True)
    assert result["created"] == {"id": 4, "name": "Add-ons", "is_addon": True}


async def test_update_category_rejects_empty(api, run):
    draft(api)
    with pytest.raises(ValidationError):
        await run("update_category", event="conf27", category_id=4)
    assert api.sent("PATCH", f"{EVENT}/categories/4") == []


async def test_float_price_is_rejected(api, run):
    draft(api)
    with pytest.raises(ValidationError, match="decimal string"):
        await run("create_product", event="conf27", name="Regular", default_price=23.0)
    assert api.sent("POST", f"{EVENT}/items") == []


async def test_bad_question_type_is_rejected(api, run):
    draft(api)
    with pytest.raises(ValidationError, match="invalid question type"):
        await run("create_question", event="conf27", question="Size", type="Z")


async def test_choice_question_needs_options(api, run):
    draft(api)
    with pytest.raises(ValidationError, match="options"):
        await run("create_question", event="conf27", question="Size", type="C")


async def test_update_product_on_live_event_awaits_approval(api, run):
    api.route("GET", EVENT, LIVE)
    result = await run("update_product", event="conf27", item_id=1, default_price="99.00")
    assert result["status"] == "awaiting_approval"
    assert api.sent("PATCH", f"{EVENT}/items/1") == []


async def test_delete_quota_previews_availability(api, run):
    api.route("GET", EVENT, DRAFT)
    api.route(
        "GET",
        f"{EVENT}/quotas/1",
        {
            "id": 1,
            "name": "Regular",
            "size": 100,
            "items": [1],
            "available_number": 5,
            "paid_orders": 92,
            "pending_orders": 3,
            "cart_positions": 0,
        },
    )
    result = await run("delete_quota", event="conf27", quota_id=1)
    assert result["status"] == "awaiting_approval"
    assert "92 paid" in result["preview"]
    assert "5 still available" in result["preview"]
    assert api.sent("DELETE", f"{EVENT}/quotas/1") == []


async def test_delete_product_previews_and_sends_nothing(api, run):
    api.route(
        "GET",
        f"{EVENT}/items/1",
        {"id": 1, "name": {"en": "Regular"}, "default_price": "23.00", "active": True, "variations": []},
    )
    result = await run("delete_product", event="conf27", item_id=1)
    assert result["status"] == "awaiting_approval"
    assert "Regular" in result["preview"]
    assert api.sent("DELETE", f"{EVENT}/items/1") == []


async def test_event_allowlist_applies(api, make_app):
    from pretix_agent_mcp.registry import REGISTRY, run_tool

    app = make_app(PRETIX_EVENT_ALLOWLIST="other")
    with pytest.raises(ValidationError):
        await run_tool(app, REGISTRY["list_products"], {"event": "conf27"})
