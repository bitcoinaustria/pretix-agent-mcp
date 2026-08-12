"""PII redaction against a real order, with real pretix field shapes.

`tests/test_redact.py` proves the masking logic on fixtures. This proves it on what pretix
actually sends: `name_parts` with a `_scheme`, an `invoice_address` block, answers, the lot.
A field pretix renames or nests differently is exactly the kind of thing fixtures cannot
catch, and the cost of missing it is a customer's address in an LLM's context.

The order is created straight against the pretix API rather than through MCP, because there
is deliberately no create_order tool — this needs PRETIX_API_TOKEN in the environment, which
the local test rig already exports.

Usage: uv run python localtest/drive_pii.py <bearer-token>
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import drive  # noqa: E402
from drive import EVENT, call, check, results, section  # noqa: E402

# An invented person, in the shape pretix stores one.
PERSON = {
    "email": "anna.schmid@example.org",
    "name": "Anna Schmid",
    "company": "Schmid GmbH",
    "street": "Hauptstrasse 1",
}
RAW_VALUES = list(PERSON.values())


def create_order(event: str) -> str:
    """A real paid order, created through pretix's own API."""
    base = os.environ["PRETIX_BASE_URL"].rstrip("/")
    organizer = os.environ["PRETIX_ORGANIZER"]
    headers = {"Authorization": f"Token {os.environ['PRETIX_API_TOKEN']}"}
    items = httpx.get(
        f"{base}/api/v1/organizers/{organizer}/events/{event}/items/", headers=headers, timeout=60
    )
    item = items.json()["results"][0]["id"]
    name_parts = {"_scheme": "full", "full_name": PERSON["name"]}
    response = httpx.post(
        f"{base}/api/v1/organizers/{organizer}/events/{event}/orders/",
        headers=headers,
        timeout=90,
        json={
            "email": PERSON["email"],
            "locale": "en",
            "sales_channel": "web",
            "status": "p",
            "invoice_address": {
                "name_parts": name_parts,
                "company": PERSON["company"],
                "street": PERSON["street"],
                "zipcode": "1010",
                "city": "Wien",
                "country": "AT",
            },
            "positions": [
                {
                    "item": item,
                    "price": "0.00",
                    "attendee_name_parts": name_parts,
                    "attendee_email": PERSON["email"],
                }
            ],
        },
    )
    response.raise_for_status()
    return response.json()["code"]


def main() -> int:
    drive.ENV = dict(os.environ)

    section("setup: a free live event with one real order")
    check(
        not call("create_event", event=EVENT, name="PII Test", date_from="2027-03-01T19:00:00+01:00")[1],
        "create_event",
    )
    product, err = call(
        "create_product", event=EVENT, name="Free Entry", default_price="0.00", admission=True
    )
    check(not err, "create_product at 0.00")
    check(
        not call("create_quota", event=EVENT, name="Seats", size=40, items=[product["created"]["id"]])[1],
        "create_quota",
    )
    code = create_order(EVENT)
    print(f"      order {code} created directly in pretix (no create_order tool exists)")

    section("PII_MODE=redacted, on a real order")
    order, err = call("get_order", event=EVENT, code=code)
    check(not err, "get_order")
    blob = json.dumps(order)
    for value in RAW_VALUES:
        check(value not in blob, f"raw {value!r} never reaches the agent")
    print("      what the agent sees:")
    print(f"        email:           {order.get('email')}")
    print(f"        invoice_address: {json.dumps(order.get('invoice_address'))}")
    position = (order.get("positions") or [{}])[0]
    print(f"        attendee:        {position.get('attendee_name')} / {position.get('attendee_email')}")
    check(
        order.get("invoice_address", {}).get("country") == "AT",
        "country survives: it identifies nobody and answers VAT questions",
    )
    check(position.get("item_name") == "Free Entry", "the product name is an object label, not PII")

    section("the same order through every other PII-bearing tool")
    for tool in ("search_attendees", "search_orders"):
        found, err = call(tool, event=EVENT)
        check(not err, tool)
        leaked = [v for v in RAW_VALUES if v in json.dumps(found)]
        check(not leaked, f"{tool} leaks nothing", f"leaked {leaked}" if leaked else "")

    section("aggregates: no PII, and still correct")
    summary, err = call("sales_summary", event=EVENT)
    check(not err, "sales_summary")
    check(not any(v in json.dumps(summary) for v in RAW_VALUES), "no PII in an aggregate")
    check(
        summary.get("orders", {}).get("paid") == 1,
        "counted the real paid order",
        json.dumps(summary.get("orders")),
    )
    check(summary.get("tickets") == 1, "counted the ticket")

    section("the audit log outlives the request, so it must be clean too")
    lines = [json.loads(line) for line in open(os.environ["AUDIT_LOG"]) if line.strip()]
    text = json.dumps(lines)
    check(not any(v in text for v in RAW_VALUES), "no customer PII in the audit log")
    check(os.environ["PRETIX_API_TOKEN"] not in text, "no pretix token in the audit log")
    check(os.environ["MCP_BEARER_TOKEN"] not in text, "no bearer token in the audit log")

    failed = [label for ok, label in results if not ok]
    print(f"\n\033[1m{len(results) - len(failed)}/{len(results)} checks passed\033[0m")
    for label in failed:
        print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
