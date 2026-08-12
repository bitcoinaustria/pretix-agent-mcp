"""The publish ceremony and the live-event guard, against a real pretix.

Uses a free product: pretix refuses to take an event live when it has a paid product and no
payment provider configured, and a free event (a stammtisch, a meetup) needs none. That
constraint is itself a finding — see the report — but it is not what this script tests.

Usage: uv run python drive_live.py <bearer-token>
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from drive import EVENT, call, check, cli, results, section  # noqa: E402


def main() -> int:
    import os

    import drive

    drive.ENV = dict(os.environ)
    print(f"test event slug: {EVENT} (free product)")

    section("setup: a free event, the way a meetup is organised")
    created, err = call(
        "create_event", event=EVENT, name="Free Stammtisch", date_from="2027-02-04T19:00:00+01:00"
    )
    check(not err, "create_event", json.dumps(created)[:120])
    product, err = call(
        "create_product", event=EVENT, name="Free Entry", default_price="0.00", admission=True
    )
    check(not err, "create_product at 0.00", json.dumps(product)[:120])
    item_id = (product or {}).get("created", {}).get("id")
    quota, err = call("create_quota", event=EVENT, name="Seats", size=40, items=[item_id])
    check(not err, "create_quota", json.dumps(quota)[:120])

    section("the publish ceremony, end to end")
    proposal, err = call("publish_event", event=EVENT)
    ok = check(not err and proposal.get("status") == "awaiting_approval", "publish_event only proposes")
    if not ok:
        print("   ", json.dumps(proposal)[:300])
        return 1
    action_id = proposal["pending_action_id"]
    check(call("get_event", event=EVENT)[0].get("live") is False, "nothing mutated while pending")
    check(action_id in cli("pending"), "listed by `pretix-agent-mcp pending`")
    check("approved" in cli("approve", action_id), "operator approves on the server")

    done, err = call("execute_pending_action", pending_action_id=action_id)
    check(not err, "the approved action executes", json.dumps(done)[:160])
    live = call("get_event", event=EVENT)[0]
    check(live.get("live") is True, "event is LIVE", f"live={live.get('live')}")
    check(live.get("testmode") is False, "publish also left test mode", f"testmode={live.get('testmode')}")
    again, err = call("execute_pending_action", pending_action_id=action_id)
    check(err, "an approved action runs at most once")

    section("the live-event guard, against real event state")
    escalated, err = call("update_product", event=EVENT, item_id=item_id, default_price="5.00")
    if check(
        not err and escalated.get("status") == "awaiting_approval",
        "a price change on a LIVE event escalates to high-risk",
        json.dumps(escalated)[:120],
    ):
        still = call("get_product", event=EVENT, item_id=item_id)[0]
        check(
            str(still.get("default_price")) == "0.00",
            "the price did not change",
            str(still.get("default_price")),
        )
        check("rejected" in cli("reject", escalated["pending_action_id"]), "operator rejects it")
        after, err = call("execute_pending_action", pending_action_id=escalated["pending_action_id"])
        check(err, "a rejected action never executes")
        final = call("get_product", event=EVENT, item_id=item_id)[0]
        check(str(final.get("default_price")) == "0.00", "price still unchanged after rejection")

    section("an unguarded write still works on a live event")
    voucher, err = call("create_voucher", event=EVENT, value="0.00", price_mode="set", item=item_id)
    check(not err, "create_voucher on a live event executes directly", json.dumps(voucher)[:120])

    section("audit trail")
    log = os.environ["AUDIT_LOG"]
    lines = [json.loads(line) for line in open(log) if line.strip()]
    events = [entry["event"] for entry in lines]
    check(
        "proposed" in events and "approved" in events and "executed" in events,
        f"lifecycle audited: {set(events)}",
    )
    check(
        all(os.environ["PRETIX_API_TOKEN"] not in json.dumps(entry) for entry in lines),
        "no pretix token in the audit log",
    )
    check(
        all(os.environ["MCP_BEARER_TOKEN"] not in json.dumps(entry) for entry in lines),
        "no bearer token in the audit log",
    )

    failed = [label for ok, label in results if not ok]
    print(f"\n\033[1m{len(results) - len(failed)}/{len(results)} checks passed\033[0m")
    for label in failed:
        print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
