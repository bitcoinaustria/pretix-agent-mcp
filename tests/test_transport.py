"""The HTTP boundary: an unauthenticated client gets nothing.

These tests speak the 2026-07-28 wire protocol directly (stateless: no initialize
handshake, no session id, protocol version in ``params._meta`` and mirrored in the
headers) against the real ASGI app, so they cover the auth middleware as deployed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from mcp_types import CLIENT_CAPABILITIES_META_KEY, LATEST_PROTOCOL_VERSION, PROTOCOL_VERSION_META_KEY

from pretix_agent_mcp.config import ConfigError, check_http_bind
from pretix_agent_mcp.server import http_app

from .conftest import BEARER

# The SDK enables DNS-rebinding protection for a localhost bind, so requests must
# arrive with a localhost Host header — as they do behind a reverse proxy.
BASE_URL = "http://127.0.0.1:8765"


def rpc(method: str, params: dict | None = None) -> dict:
    body = dict(params or {})
    body["_meta"] = {
        PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": body}


def headers(method: str, token: str | None, name: str | None = None) -> dict[str, str]:
    out = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": LATEST_PROTOCOL_VERSION,
        "mcp-method": method,
    }
    if name:
        out["mcp-name"] = name
    if token is not None:
        out["authorization"] = f"Bearer {token}"
    return out


@asynccontextmanager
async def serve(app, *, url_suffix: str = ""):
    """The deployed ASGI app, in-process, with its lifespan running."""
    asgi = http_app(app)
    async with asgi.inner.router.lifespan_context(asgi.inner):
        transport = httpx.ASGITransport(app=asgi)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:

            async def post(method: str, params: dict | None = None, *, token: str | None = BEARER, name=None):
                return await http.post(
                    f"/mcp{url_suffix}", json=rpc(method, params), headers=headers(method, token, name)
                )

            yield post


async def test_no_token_gets_nothing(app):
    async with serve(app) as post:
        response = await post("tools/list", token=None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert "tools" not in response.text


async def test_wrong_token_gets_nothing(app):
    async with serve(app) as post:
        response = await post("tools/list", token="not-the-token")
    assert response.status_code == 401
    assert "list_events" not in response.text


async def test_token_in_query_string_is_not_accepted(app):
    """The spec forbids the token in the URL; we only ever read the header."""
    async with serve(app, url_suffix=f"?access_token={BEARER}") as post:
        response = await post("tools/list", token=None)
    assert response.status_code == 401


async def test_authenticated_tools_list(app):
    async with serve(app) as post:
        response = await post("tools/list")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    names = [tool["name"] for tool in result["tools"]]
    assert "list_events" in names
    assert names == sorted(names), "tools/list must be deterministically ordered"
    # SEP-2549 freshness hints are required in this revision.
    assert result["ttlMs"] > 0
    assert result["cacheScope"] in {"public", "private"}


async def test_no_generic_request_tool_is_exposed(app):
    async with serve(app) as post:
        response = await post("tools/list")
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    forbidden = {"request", "http", "api", "query", "raw", "fetch", "curl", "sql", "endpoint", "url"}
    assert not [n for n in names if forbidden & set(n.split("_"))], names


async def test_tool_call_over_the_wire(app, api):
    api.page("GET", "events", [{"slug": "conf27", "name": {"en": "Conf 27"}, "live": False}])
    async with serve(app) as post:
        response = await post("tools/call", {"name": "list_events", "arguments": {}}, name="list_events")
    assert response.status_code == 200, response.text
    assert "conf27" in response.text
    assert "Token" not in response.text  # no credential echoed back


async def test_server_refuses_non_localhost_bind_without_token(make_config):
    with pytest.raises(ConfigError, match="refusing to bind"):
        check_http_bind(make_config(MCP_HOST="0.0.0.0", MCP_BEARER_TOKEN=""))


async def test_short_bearer_token_is_refused(make_config):
    with pytest.raises(ConfigError, match="at least 24"):
        check_http_bind(make_config(MCP_BEARER_TOKEN="short"))
