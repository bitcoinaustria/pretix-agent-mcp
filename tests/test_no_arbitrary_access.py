"""Arbitrary pretix REST access must be impossible through MCP.

Rather than trusting each of the 64 tools to validate its own inputs, this walks the
whole registry and feeds an escape attempt into every string parameter. Whatever a tool
does with it, the request it builds must stay inside the configured organizer's
namespace — or never be built at all.
"""

from __future__ import annotations

import inspect
from typing import Any, get_args, get_origin

import pytest

from pretix_agent_mcp.pending import ApprovalError
from pretix_agent_mcp.pretix import PretixError
from pretix_agent_mcp.registry import REGISTRY, run_tool
from pretix_agent_mcp.validate import ValidationError

from .conftest import API_PREFIX

ESCAPES = [
    "../../organizers/other-org/events",
    "..%2f..%2forganizers%2fother-org",
    "conf27/../../../organizers/other-org/events",
    "conf27?export=1",
    "/absolute/path",
]

EXPECTED = (ValidationError, PretixError, ApprovalError, ValueError, TypeError, KeyError)


def hostile_value(annotation: Any, escape: str) -> Any:
    """An escape attempt shaped like the parameter's declared type."""
    origin = get_origin(annotation)
    if origin is not None:  # Optional[X], list[X], dict[K, V]
        args = [a for a in get_args(annotation) if a is not type(None)]
        if origin in (list, tuple, set):
            return [hostile_value(args[0], escape)] if args else [escape]
        if origin is dict:
            return {escape: escape}
        return hostile_value(args[0], escape) if args else escape
    if annotation is bool:
        return True
    if annotation is int:
        return escape  # a non-integer id must be refused, not coerced
    if annotation is dict:
        return {escape: escape}
    if annotation is list:
        return [escape]
    return escape


@pytest.mark.parametrize("escape", ESCAPES)
@pytest.mark.parametrize("tool_name", sorted(REGISTRY))
async def test_no_tool_can_be_steered_out_of_the_organizer(tool_name, escape, app, api):
    spec = REGISTRY[tool_name]
    signature = inspect.signature(spec.fn, eval_str=True)
    kwargs = {
        name: hostile_value(param.annotation, escape)
        for name, param in list(signature.parameters.items())[1:]  # skip `app`
    }
    try:
        await run_tool(app, spec, kwargs)
    except EXPECTED:
        pass  # refused — the desired outcome

    for request in api.requests:
        path = request.url.path
        assert path.startswith(API_PREFIX), f"{tool_name} escaped the organizer: {path}"
        assert ".." not in path, f"{tool_name} built a traversal: {path}"
        assert "other-org" not in str(request.url), f"{tool_name} reached another organizer: {request.url}"


async def test_the_registry_has_no_generic_access_tool():
    """No tool takes a URL, a path, a method or a raw body — that is the whole point."""
    banned = {"url", "path", "method", "endpoint", "body", "payload", "query", "sql", "resource"}
    for name, spec in REGISTRY.items():
        parameters = set(inspect.signature(spec.fn, eval_str=True).parameters) - {"app"}
        assert not (banned & parameters), f"{name} exposes {banned & parameters}"
