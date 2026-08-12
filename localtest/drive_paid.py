"""The whole paid-event lifecycle, agent-driven, against a real pretix.

This is the north star as a test: an operator sets payment up once at organizer level
(`bootstrap.py` does it), and from then on an agent creates a paid event, prices it, opens
it for sale and changes a price on the selling event — with the only human step being two
`approve` commands on the server, and no visit to the pretix web UI.

It also asserts the negative: the agent cannot rewrite where the money goes. pretix's own
permission for that (`event.settings.payment:write`) is coarse — it covers payment deadlines
and IBANs with one flag — so update_event_settings refuses the money-routing subset itself,
which is what makes granting that permission survivable.

Usage: uv run python localtest/drive_paid.py <bearer-token>
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import drive  # noqa: E402
from drive import EVENT, call, check, cli, results, section  # noqa: E402

# Money-routing keys an injected agent would love: a destination account and two provider
# credentials. Named as three different plugins invent them, because the guard is on the
# shape of the name rather than a list of providers we happen to know about.
MONEY_ROUTING = {
    "payment_banktransfer_bank_details_sepa_iban": "AT999999999999999999",
    "payment_stripe_secret_key": "sk_live_attacker",
    "payment_btcpay_api_key": "attacker-controlled-key",
}


def main() -> int:
    drive.ENV = dict(os.environ)
    print(f"test event slug: {EVENT} (paid)")

    section("the agent builds a paid event")
    check(
        not call("create_event", event=EVENT, name="Paid Conference", date_from="2027-06-12T09:00:00+02:00")[
            1
        ],
        "create_event (draft, test mode)",
    )
    product, err = call(
        "create_product", event=EVENT, name="Standard Ticket", default_price="42.00", admission=True
    )
    check(not err, "create_product at 42.00")
    item = product["created"]["id"]
    check(not call("create_quota", event=EVENT, name="Seats", size=100, items=[item])[1], "create_quota")
    plugins = call("get_event", event=EVENT)[0].get("plugins") or []
    wanted = sorted(set(plugins) | {"pretix.plugins.banktransfer"})
    check(not call("set_event_plugins", event=EVENT, plugins=wanted)[1], "set_event_plugins")

    section("but money routing is out of reach")
    for key, value in MONEY_ROUTING.items():
        blocked, err = call("update_event_settings", event=EVENT, settings={key: value})
        check(err and "payment configuration" in json.dumps(blocked), f"refused: {key}")

    section("publish: propose, approve on the server, execute")
    proposal, err = call("publish_event", event=EVENT)
    if not check(not err and proposal.get("status") == "awaiting_approval", "publish_event proposes only"):
        print("   ", json.dumps(proposal)[:400])
        return 1
    print("      preview:\n        " + proposal["preview"].replace("\n", "\n        "))
    check(call("get_event", event=EVENT)[0].get("live") is False, "nothing mutated while pending")
    action_id = proposal["pending_action_id"]
    check("approved" in cli("approve", action_id), "operator approves")
    done, err = call("execute_pending_action", pending_action_id=action_id)
    check(not err, "the approved publish executes", json.dumps(done)[:120])
    live = call("get_event", event=EVENT)[0]
    check(live.get("live") is True, "PAID event is LIVE")
    check(live.get("testmode") is False, "and out of test mode")

    section("the live guard on an event that is genuinely selling")
    escalated, err = call("update_product", event=EVENT, item_id=item, default_price="99.00")
    check(not err and escalated.get("status") == "awaiting_approval", "a price change escalates to high-risk")
    price = str(call("get_product", event=EVENT, item_id=item)[0]["default_price"])
    check(price == "42.00", "the old price is held while pending", price)
    check("approved" in cli("approve", escalated["pending_action_id"]), "operator approves the price change")
    check(
        not call("execute_pending_action", pending_action_id=escalated["pending_action_id"])[1], "it applies"
    )
    price = str(call("get_product", event=EVENT, item_id=item)[0]["default_price"])
    check(price == "99.00", "the new price is live", price)

    failed = [label for ok, label in results if not ok]
    print(f"\n\033[1m{len(results) - len(failed)}/{len(results)} checks passed\033[0m")
    for label in failed:
        print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
