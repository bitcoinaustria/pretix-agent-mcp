"""Drive a running pretix-agent-mcp over the real wire, against a real pretix.

Speaks JSON-RPC over streamable HTTP directly (the same approach as tests/test_transport.py)
rather than through an SDK client, so every header — bearer token included — is explicit.

What the 555 unit tests cannot prove is exactly what this checks: that the endpoint paths and
field names match a real pretix, that the live-event guard fires against real event state, and
that the approval ceremony works end to end against a real database.

Usage: uv run python drive.py <bearer-token> [base-url]
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import subprocess
import sys

import httpx
from mcp_types import CLIENT_CAPABILITIES_META_KEY, LATEST_PROTOCOL_VERSION, PROTOCOL_VERSION_META_KEY

BEARER = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765"
# A fresh slug per run: pretix keeps what earlier runs created, and "already exists" is not
# the failure this script is looking for.
EVENT = os.environ.get("TEST_EVENT") or f"localtest-{secrets.token_hex(3)}"

PASS, FAIL = "\033[32m  ok \033[0m", "\033[31mFAIL \033[0m"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"{PASS if ok else FAIL} {label}" + (f"  — {detail}" if detail else ""))
    return ok


def rpc(method: str, params: dict | None = None, *, token: str | None = BEARER, name: str | None = None):
    body = dict(params or {})
    body["_meta"] = {
        PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
    }
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": LATEST_PROTOCOL_VERSION,
        "mcp-method": method,
    }
    if name:
        headers["mcp-name"] = name
    if token:
        headers["authorization"] = f"Bearer {token}"
    return httpx.post(
        f"{BASE}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": body},
        headers=headers,
        timeout=60,
    )


def _payload(response: httpx.Response) -> dict:
    """The JSON-RPC envelope, whether it arrived as JSON or as a one-event SSE stream."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("SSE response carried no data frame")
    return response.json()


def call(tool: str, **arguments):
    """Call a tool, returning (result_dict, is_error).

    Retries once on an empty body: this pretix runs emulated (amd64 image on arm64) and an
    overloaded gunicorn worker occasionally drops a response. That is a property of the test
    rig, not of the server under test — but a flake here would read as a failure.
    """
    for attempt in (1, 2):
        response = rpc("tools/call", {"name": tool, "arguments": arguments}, name=tool)
        if response.status_code != 200:
            return {"http_error": response.status_code, "body": response.text[:400]}, True
        try:
            payload = _payload(response)
            break
        except (json.JSONDecodeError, ValueError):
            if attempt == 2:
                return {"unparseable_body": response.text[:200], "len": len(response.text)}, True
            print(f"      (retrying {tool}: empty response from the emulated pretix)")
    if "error" in payload:
        return payload["error"], True
    result = payload["result"]
    if result.get("isError"):
        text = "".join(c.get("text", "") for c in result.get("content", []))
        return {"tool_error": text}, True
    if result.get("structuredContent") is not None:
        return result["structuredContent"], False
    text = "".join(c.get("text", "") for c in result.get("content", []))
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        return {"text": text}, False


def cli(*args: str) -> str:
    """Run the operator-side CLI, the way a human approves on the server."""
    out = subprocess.run(
        ["uv", "run", "pretix-agent-mcp", *args], capture_output=True, text=True, cwd=CWD, env=ENV
    )
    return (out.stdout + out.stderr).strip()


# The repo root, so the CLI runs where its config and venv are.
CWD = str(pathlib.Path(__file__).resolve().parent.parent)
ENV = None  # set by main()


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def main() -> int:
    global ENV

    ENV = dict(os.environ)
    print(f"test event slug: {EVENT}")

    section("1. the HTTP boundary")
    unauth = rpc("tools/list", token=None)
    check(unauth.status_code == 401, "no bearer token → 401", f"got {unauth.status_code}")
    check("list_events" not in unauth.text, "unauthenticated client sees no tool list")
    wrong = rpc("tools/list", token="not-the-token")
    check(wrong.status_code == 401, "wrong bearer token → 401", f"got {wrong.status_code}")

    section("2. discovery")
    listed = rpc("tools/list")
    check(listed.status_code == 200, "authenticated tools/list → 200", f"got {listed.status_code}")
    tools = listed.json()["result"]["tools"]
    names = [t["name"] for t in tools]
    check(len(names) > 0, f"{len(names)} tools advertised")
    check(names == sorted(names), "deterministic order (SEP-2549 cacheability)")
    banned = {"request", "http", "api", "raw", "fetch", "curl", "sql", "url", "endpoint"}
    check(not [n for n in names if banned & set(n.split("_"))], "no generic request tool exposed")
    check("Token" not in listed.text and "pretix-token" not in listed.text, "no credential in tool list")

    section("3. reads against a real pretix")
    events, err = call("list_events")
    check(not err, "list_events", json.dumps(events)[:160])

    section("4. the agent builds an event from scratch")
    created, err = call(
        "create_event",
        event=EVENT,
        name="Local Test Conference",
        date_from="2027-06-12T09:00:00+02:00",
        currency="EUR",
    )
    if check(not err, "create_event", json.dumps(created)[:200]):
        ev = created.get("created", {})
        check(ev.get("live") is False, "new event is not live", f"live={ev.get('live')}")
        check(ev.get("testmode") is True, "new event is in test mode", f"testmode={ev.get('testmode')}")

    product, err = call(
        "create_product", event=EVENT, name="Standard Ticket", default_price="42.00", admission=True
    )
    check(not err, "create_product with a decimal price", json.dumps(product)[:160])
    item_id = (product or {}).get("created", {}).get("id")

    quota, err = call("create_quota", event=EVENT, name="Seats", size=100, items=[item_id] if item_id else [])
    check(not err, "create_quota", json.dumps(quota)[:160])

    avail, err = call("get_availability", event=EVENT)
    check(not err, "get_availability", json.dumps(avail)[:200])

    # A paid event needs its payment plugin active before pretix will take it live. The
    # organizer allowed and configured the provider once (bootstrap.py); switching it on for
    # this event is ordinary agent work.
    plugins = call("get_event", event=EVENT)[0].get("plugins") or []
    out, err = call(
        "set_event_plugins", event=EVENT, plugins=sorted(set(plugins) | {"pretix.plugins.banktransfer"})
    )
    check(not err, "set_event_plugins enables the payment plugin", json.dumps(out)[:130])

    section("5. money validation against the real API")
    bad, err = call("create_product", event=EVENT, name="Float Ticket", default_price=19.99)
    check(err, "a float price is refused", json.dumps(bad)[:140])
    bad, err = call("create_voucher", event=EVENT, value="nan", price_mode="set")
    check(err, "voucher value 'nan' is refused", json.dumps(bad)[:140])

    section("6. an ordinary write on a draft event executes directly")
    voucher, err = call("create_voucher", event=EVENT, value="0.00", price_mode="set", item=item_id)
    check(not err, "create_voucher on a draft event", json.dumps(voucher)[:160])

    section("7. high-risk: publish needs an out-of-band approval")
    proposal, err = call("publish_event", event=EVENT)
    ok = check(not err and proposal.get("status") == "awaiting_approval", "publish_event only proposes")
    action_id = proposal.get("pending_action_id") if ok else None
    if action_id:
        print(
            "      preview shown to the operator:\n        " + proposal["preview"].replace("\n", "\n        ")
        )
        after = call("get_event", event=EVENT)[0]
        check(after.get("live") is False, "nothing was mutated while pending", f"live={after.get('live')}")

        early, err = call("execute_pending_action", pending_action_id=action_id)
        check(err, "executing before approval is refused", json.dumps(early)[:120])

        pending_out = cli("pending")
        check(action_id in pending_out, "the action shows up in `pretix-agent-mcp pending`")

        approved = cli("approve", action_id)
        check(
            "approved" in approved,
            "operator approves on the server",
            approved.splitlines()[0] if approved else "",
        )

        done, err = call("execute_pending_action", pending_action_id=action_id)
        check(not err, "the approved action executes", json.dumps(done)[:160])
        live = call("get_event", event=EVENT)[0]
        check(live.get("live") is True, "event is live", f"live={live.get('live')}")
        check(
            live.get("testmode") is False, "publish also left test mode", f"testmode={live.get('testmode')}"
        )

        again, err = call("execute_pending_action", pending_action_id=action_id)
        check(err, "an approved action runs at most once", json.dumps(again)[:120])

    section("8. the live-event guard now escalates ordinary writes")
    escalated, err = call("update_product", event=EVENT, item_id=item_id, default_price="99.00")
    if check(
        not err and escalated.get("status") == "awaiting_approval",
        "a price change on a LIVE event escalates to high-risk",
        json.dumps(escalated)[:120],
    ):
        still = call("get_product", event=EVENT, item_id=item_id)[0]
        check(
            str(still.get("default_price")) == "42.00",
            "the price did not change",
            f"{still.get('default_price')}",
        )
        cli("reject", escalated["pending_action_id"])
        rejected, err = call("execute_pending_action", pending_action_id=escalated["pending_action_id"])
        check(err, "a rejected action never executes", json.dumps(rejected)[:120])

    section("9. no arbitrary REST access")
    for escape in ["../../organizers/other-org/events", f"{EVENT}/../../foo", f"{EVENT}?export=1"]:
        out, err = call("get_event", event=escape)
        check(err, f"path escape refused: {escape[:34]}", json.dumps(out)[:90])

    section("10. sales summary and PII posture")
    summary, err = call("sales_summary", event=EVENT)
    if check(not err, "sales_summary", json.dumps(summary)[:200]):
        check("@" not in json.dumps(summary), "no email addresses in an aggregate result")

    orders, err = call("search_orders", event=EVENT)
    check(not err, "search_orders", json.dumps(orders)[:160])

    section("11. capability gate")
    out, err = call("delete_event", event=EVENT)
    check(
        (not err and out.get("status") == "awaiting_approval") or err,
        "delete_event cannot execute directly",
        json.dumps(out)[:110],
    )

    failed = [label for ok, label in results if not ok]
    print(f"\n\033[1m{len(results) - len(failed)}/{len(results)} checks passed\033[0m")
    if failed:
        print("\nfailed:")
        for label in failed:
            print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
