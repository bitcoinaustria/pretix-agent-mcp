"""Test harness: a fake pretix API and a ready-made :class:`App`.

No test ever talks to a real pretix instance, and no fixture contains a real URL,
token or person.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from pretix_agent_mcp import config
from pretix_agent_mcp import tools as _tools  # noqa: F401  — registers every tool
from pretix_agent_mcp.audit import Audit
from pretix_agent_mcp.pending import PendingStore
from pretix_agent_mcp.pretix import Pretix
from pretix_agent_mcp.registry import App

BASE_URL = "https://tickets.example.org"
ORGANIZER = "demo"
API_PREFIX = f"/api/v1/organizers/{ORGANIZER}/"
TOKEN = "pretix-token-" + "x" * 30
BEARER = "bearer-token-" + "y" * 20


class FakeAPI:
    """A pretix stand-in. Routes are keyed by method and organizer-relative path.

    ``api.route("GET", "events/conf27", {...})`` answers
    ``GET /api/v1/organizers/demo/events/conf27/``. Every request is recorded in
    ``api.requests`` so tests can assert what was (and was not) sent.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []

    def route(self, method: str, path: str, payload: Any = None, status: int = 200) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            if payload is None:
                return httpx.Response(status if status != 200 else 204)
            return httpx.Response(status, json=payload)

        self.routes[(method.upper(), path.strip("/"))] = handler

    def route_fn(self, method: str, path: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[(method.upper(), path.strip("/"))] = handler

    def page(self, method: str, path: str, results: list[Any], count: int | None = None) -> None:
        """A single-page paginated listing response."""
        self.route(
            method,
            path,
            {"count": count if count is not None else len(results), "next": None, "results": results},
        )

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            self.bodies.append(json.loads(request.content) if request.content else None)
            path = request.url.path
            assert path.startswith(API_PREFIX), f"request escaped the organizer scope: {path}"
            key = (request.method.upper(), path[len(API_PREFIX) :].strip("/"))
            handler = self.routes.get(key)
            if handler is None:
                return httpx.Response(404, json={"detail": f"no fake route for {key}"})
            return handler(request)

        return httpx.MockTransport(handle)

    def sent(self, method: str, path: str) -> list[Any]:
        """JSON bodies sent to one route, in order."""
        wanted = (method.upper(), path.strip("/"))
        return [
            body
            for request, body in zip(self.requests, self.bodies, strict=True)
            if (request.method.upper(), request.url.path[len(API_PREFIX) :].strip("/")) == wanted
        ]


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def make_config(tmp_path) -> Callable[..., config.Config]:
    def _make(**env: str) -> config.Config:
        base = {
            "PRETIX_BASE_URL": BASE_URL,
            "PRETIX_API_TOKEN": TOKEN,
            "PRETIX_ORGANIZER": ORGANIZER,
            "MCP_BEARER_TOKEN": BEARER,
            "AUDIT_LOG": str(tmp_path / "audit.jsonl"),
            "STATE_DB": str(tmp_path / "pending.sqlite3"),
        }
        base.update(env)
        return config.load(env=base, config_file=tmp_path / "no-config.json")

    return _make


@pytest.fixture
def make_app(api: FakeAPI, make_config) -> Callable[..., App]:
    """Build an :class:`App` wired to the fake API. Writes are enabled by default —
    the capability defaults are tested explicitly in test_capabilities.py."""

    def _make(**env: str) -> App:
        env.setdefault("MCP_CAPABILITIES", "read,write,write:high-risk")
        cfg = make_config(**env)
        return App(
            cfg=cfg,
            pretix=Pretix(
                cfg.pretix_base_url, cfg.pretix_api_token, cfg.organizer, transport=api.transport()
            ),
            audit=Audit(cfg.audit_log),
            pending=PendingStore(cfg.state_db, cfg.approval_ttl_seconds),
        )

    return _make


@pytest.fixture
def app(make_app) -> App:
    return make_app()


@pytest.fixture
def call(app: App):
    """Call a tool the way the server does: through the registry gate."""
    from pretix_agent_mcp.registry import REGISTRY, run_tool

    async def _call(tool_name: str, /, **kwargs: Any) -> dict:
        """Positional-only, so a tool's own ``name`` parameter can be passed as a kwarg."""
        return await run_tool(app, REGISTRY[tool_name], kwargs)

    return _call
