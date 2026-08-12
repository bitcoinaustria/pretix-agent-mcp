"""The HTTP client: organizer scoping, error surface, and pretix Hosted's rate limit."""

from __future__ import annotations

import httpx
import pytest

from pretix_agent_mcp.pretix import MAX_RETRY_WAIT, Pretix, PretixError

from .conftest import ORGANIZER, TOKEN


def client(api) -> Pretix:
    return Pretix("https://tickets.example.org", TOKEN, ORGANIZER, transport=api.transport())


async def test_a_429_is_retried_after_the_requested_delay(api, monkeypatch):
    """pretix Hosted allows 360 requests/minute per organizer and documents a 429 as safe
    to retry — the request was refused, not processed."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("pretix_agent_mcp.pretix.asyncio.sleep", fake_sleep)
    attempts = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"detail": "throttled"})
        return httpx.Response(200, json={"slug": "conf27"})

    api.route_fn("GET", "events/conf27", handler)
    assert (await client(api).get("events", "conf27")) == {"slug": "conf27"}
    assert slept == [2.0]


async def test_a_sustained_429_gives_up_instead_of_hanging(api, monkeypatch):
    async def fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("pretix_agent_mcp.pretix.asyncio.sleep", fake_sleep)
    api.route("GET", "events/conf27", {"detail": "throttled"}, status=429)
    with pytest.raises(PretixError) as exc:
        await client(api).get("events", "conf27")
    assert exc.value.status == 429
    assert len(api.requests) == 3


async def test_an_absurd_retry_after_is_capped(api, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("pretix_agent_mcp.pretix.asyncio.sleep", fake_sleep)
    api.route_fn(
        "GET",
        "events/conf27",
        lambda _: httpx.Response(429, headers={"Retry-After": "86400"}, json={}),
    )
    with pytest.raises(PretixError):
        await client(api).get("events", "conf27")
    assert slept == [MAX_RETRY_WAIT, MAX_RETRY_WAIT]


async def test_a_transport_failure_never_names_the_client(api):
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    api.route_fn("GET", "events/conf27", boom)
    with pytest.raises(PretixError) as exc:
        await client(api).get("events", "conf27")
    assert TOKEN not in str(exc.value)
    assert str(exc.value) == "pretix API error 0: could not reach pretix: ConnectError"


async def test_an_error_body_is_truncated(api):
    api.route("GET", "events/conf27", {"detail": "x" * 5000}, status=400)
    with pytest.raises(PretixError) as exc:
        await client(api).get("events", "conf27")
    assert len(exc.value.detail) <= 800


async def test_an_error_body_cannot_carry_pii_into_the_agents_context(api):
    """pretix quotes the offending value in some validation errors. The body is echoed to
    the agent to explain the failure, so it is scrubbed on the way out — redaction elsewhere
    is key-based and would never see inside an error string."""
    api.route("GET", "events/conf27", {"email": ["anna.schmid@example.com is registered"]}, status=400)
    with pytest.raises(PretixError) as exc:
        await client(api).get("events", "conf27")
    assert "anna.schmid@example.com" not in str(exc.value)
    assert "anna.schmid@example.com" not in exc.value.detail
    assert "is registered" in exc.value.detail
