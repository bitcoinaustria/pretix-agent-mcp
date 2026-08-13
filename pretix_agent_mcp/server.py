"""MCP server wiring: tool registration, transports, bearer auth.

Protocol: MCP revision 2026-07-28 via the official SDK, which keeps backward
compatibility with the initialization-based revisions (2025-06-18 / 2025-11-25) and
answers ``server/discover`` and the ``tools/list`` cache hints for us.

Authorization is a static bearer token, constant-time compared. That is a documented
deviation from the spec's OAuth 2.1 framework (authorization is OPTIONAL in the spec)
and appropriate for a single-operator self-hosted deployment: the operator controls
both ends. The spec's token rules still hold — header only, never the query string;
missing or wrong token gets HTTP 401.
"""

from __future__ import annotations

import inspect
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from . import tools  # noqa: F401  — importing the package registers every tool
from .audit import Audit
from .config import Config, check_http_bind
from .pending import PendingStore
from .pretix import Pretix
from .registry import App, ToolSpec, enabled_tools, run_tool

__version__ = "0.1.0"


def build_app(cfg: Config) -> App:
    return App(
        cfg=cfg,
        pretix=Pretix(cfg.pretix_base_url, cfg.pretix_api_token, cfg.organizer),
        audit=Audit(cfg.audit_log),
        pending=PendingStore(cfg.state_db, cfg.approval_ttl_seconds),
    )


def _bind(app: App, spec: ToolSpec) -> Callable[..., Awaitable[Any]]:
    """Expose a registry tool to the SDK: same signature minus the leading ``app``."""

    async def endpoint(**kwargs: Any) -> dict[str, Any]:
        return await run_tool(app, spec, kwargs)

    signature = inspect.signature(spec.fn, eval_str=True)
    parameters = list(signature.parameters.values())[1:]  # drop `app`
    endpoint.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    endpoint.__annotations__ = {p.name: p.annotation for p in parameters} | {"return": dict[str, Any]}
    endpoint.__name__ = spec.name
    endpoint.__doc__ = spec.description
    endpoint.__module__ = spec.fn.__module__
    return endpoint


def build_server(app: App) -> MCPServer:
    mcp = MCPServer(
        name="pretix-agent-mcp",
        title="pretix",
        version=__version__,
        instructions=(
            f"Administers the pretix organizer {app.cfg.organizer!r} through purpose-built tools. "
            "There is no generic API access. Personal data is masked unless the operator enabled "
            "PII_MODE=full. Irreversible operations return a pending_action_id that a human must "
            "approve on the server before you call execute_pending_action."
        ),
        # tools/list is stable for a deployment's lifetime; scope it per authorization context.
        cache_hints={"tools/list": CacheHint(ttl_ms=300_000, scope="private")},
    )
    for spec in enabled_tools(app.cfg):
        mcp.add_tool(_bind(app, spec), name=spec.name, title=spec.title, description=spec.description)
    return mcp


class BearerAuth:
    """Require ``Authorization: Bearer <token>`` on every HTTP request."""

    def __init__(self, asgi_app: Any, token: str) -> None:
        self.inner = asgi_app  # the Starlette app, whose lifespan starts the MCP session manager
        # Compared as bytes: secrets.compare_digest raises TypeError on a non-ASCII str, which
        # would turn an unauthenticated request into a 500 with a traceback instead of a 401.
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.inner(scope, receive, send)
            return
        if scope["type"] != "http":  # no websocket routes exist; do not pass one through unchecked
            return
        scheme, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.strip().encode("utf-8", "surrogatepass"), self._token
        ):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="pretix-agent-mcp"'},
            )
            await response(scope, receive, send)
            return
        await self.inner(scope, receive, send)


def transport_security(cfg: Config) -> TransportSecuritySettings:
    """Which ``Host`` headers the SDK's DNS-rebinding protection accepts.

    The protection defaults to the bind address, which breaks the deployment this project
    recommends: bind ``127.0.0.1``, put a reverse proxy in front, and nginx or Caddy forwards
    the public hostname — which the server then answers with 421 Misdirected Request. Naming
    the hostname in ``MCP_ALLOWED_HOSTS`` is the fix. Setting it to ``*`` turns the check off
    for operators who terminate it in the proxy instead.
    """
    hosts = list(cfg.allowed_hosts)
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # The bind address stays valid, so a local client and a health check keep working.
    for host in (cfg.host, f"{cfg.host}:{cfg.port}"):
        if host not in hosts:
            hosts.append(host)
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=hosts)


def http_app(app: App) -> Any:
    """The authenticated ASGI app. Nothing is reachable without the bearer token."""
    check_http_bind(app.cfg)
    assert app.cfg.mcp_bearer_token  # check_http_bind guarantees this
    inner = build_server(app).streamable_http_app(
        stateless_http=True, host=app.cfg.host, transport_security=transport_security(app.cfg)
    )
    return BearerAuth(inner, app.cfg.mcp_bearer_token)


def serve_http(cfg: Config) -> None:
    import uvicorn

    app = build_app(cfg)
    uvicorn.run(http_app(app), host=cfg.host, port=cfg.port, log_level=cfg.log_level)


def serve_stdio(cfg: Config) -> None:
    """Same-machine development transport. No bearer token: the peer is the parent process."""
    build_server(build_app(cfg)).run("stdio")
