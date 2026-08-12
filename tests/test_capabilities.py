"""Defaults are closed, and a disabled tool is not advertised at all."""

from __future__ import annotations

import pytest

from pretix_agent_mcp.config import ConfigError
from pretix_agent_mcp.registry import REGISTRY, enabled_tools, run_tool
from pretix_agent_mcp.server import build_server
from pretix_agent_mcp.validate import ValidationError


def names(cfg) -> set[str]:
    return {spec.name for spec in enabled_tools(cfg)}


def test_default_configuration_is_read_only(make_config):
    exposed = names(make_config())
    assert "list_events" in exposed
    assert not [name for name in exposed if REGISTRY[name].capability != "read"]


def test_writes_are_opt_in(make_config):
    exposed = names(make_config(MCP_CAPABILITIES="read,write"))
    assert "create_event" in exposed
    assert "delete_event" not in exposed, "high-risk tools need their own capability class"
    assert "publish_event" not in exposed


def test_high_risk_class_exposes_high_risk_tools(make_config):
    assert "publish_event" in names(make_config(MCP_CAPABILITIES="read,write,write:high-risk"))


def test_tool_allowlist_wins_over_capability_classes(make_config):
    cfg = make_config(MCP_CAPABILITIES="read,write", MCP_TOOL_ALLOWLIST="list_events,get_event")
    assert names(cfg) == {"list_events", "get_event"}


def test_disabled_tools_are_not_advertised(app, make_app):
    read_only = make_app(MCP_CAPABILITIES="read")
    advertised = {tool.name for tool in _list_tools(read_only)}
    assert "list_events" in advertised
    assert "create_event" not in advertised


def _list_tools(app):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(build_server(app).list_tools())


async def test_a_disabled_tool_refuses_even_if_called_directly(make_app):
    """Belt and braces: the gate re-checks, so a stale client cannot call a hidden tool."""
    read_only = make_app(MCP_CAPABILITIES="read")
    with pytest.raises(PermissionError):
        await run_tool(
            read_only, REGISTRY["create_event"], {"event": "x", "name": "X", "date_from": "2027-01-01"}
        )


async def test_event_allowlist_is_enforced(make_app, api):
    app = make_app(PRETIX_EVENT_ALLOWLIST="conf27")
    api.route("GET", "events/other", {"slug": "other"})
    with pytest.raises(ValidationError, match="allowlist"):
        await run_tool(app, REGISTRY["get_event"], {"event": "other"})
    assert api.requests == [], "a disallowed event must not reach pretix"


async def test_event_allowlist_filters_listings(make_app, api):
    app = make_app(PRETIX_EVENT_ALLOWLIST="conf27")
    api.page("GET", "events", [{"slug": "conf27"}, {"slug": "other"}])
    result = await run_tool(app, REGISTRY["list_events"], {})
    assert [event["slug"] for event in result["results"]] == ["conf27"]


def test_unknown_capability_class_is_a_configuration_error(make_config):
    with pytest.raises(ConfigError, match="unknown capability"):
        make_config(MCP_CAPABILITIES="read,write,admin")


def test_pii_mode_must_be_valid(make_config):
    with pytest.raises(ConfigError, match="PII_MODE"):
        make_config(PII_MODE="sometimes")


def test_every_tool_has_a_known_capability_class():
    for spec in REGISTRY.values():
        assert spec.capability in ("read", "write", "write:high-risk"), spec.name
        assert spec.fn.__doc__, f"{spec.name} needs a docstring — it is the agent's description"
