"""Tool registry: capability gating, the live-event guard, redaction and audit.

Tool modules declare plain async functions whose first parameter is the :class:`App`::

    @tool("read")
    async def list_events(app: App, limit: int = 50) -> dict:
        '''One-line summary the agent sees as the tool description.'''
        ...

Everything a tool must not forget happens here instead of in each tool:

* the tool is only advertised if its capability class is enabled in config,
* an ``event`` argument is checked against the event allowlist,
* every parameter the tool declares in ``money=`` is validated — before the approval
  branch below, because a queued call does not run its body until a human approved it,
* a ``write`` against a **live** event is escalated to ``write:high-risk``
  (the live-event guard) when the tool declares ``live_guard=True``,
* a ``write:high-risk`` call mutates nothing: it records a pending action and
  returns a preview plus a handle,
* results are PII-redacted unless the operator configured ``PII_MODE=full``,
* writes and high-risk lifecycle events are audited.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .audit import Audit
from .config import Config
from .pending import ApprovalError, PendingStore
from .pretix import Pretix, PretixError
from .redact import redact, redact_args
from .validate import ValidationError, price, prices, slug

CAPABILITIES = ("read", "write", "write:high-risk")


@dataclass
class App:
    """Everything a tool needs. One per server process."""

    cfg: Config
    pretix: Pretix
    audit: Audit
    pending: PendingStore

    async def event(self, event_slug: str) -> dict[str, Any]:
        """Fetch an event, honouring the allowlist. Used by tools and by the live guard."""
        return await self.pretix.get("events", self.check_event(event_slug))

    def check_event(self, event_slug: object) -> str:
        value = slug(event_slug, field="event slug")
        if not self.cfg.event_allowed(value):
            raise ValidationError(f"event {value!r} is not in the configured event allowlist")
        return value


PreviewFn = Callable[[App, dict[str, Any]], Awaitable[tuple[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Awaitable[Any]]
    capability: str
    live_guard: bool = False
    preview: PreviewFn | None = None
    title: str | None = None
    # Parameters holding an amount. Validated here rather than in the tool body, because a
    # high-risk or escalated call never reaches its body until after a human approved it —
    # an unparseable price must be refused before it is queued, not after the ceremony.
    money: tuple[str, ...] = ()

    @property
    def description(self) -> str:
        text = inspect.cleandoc(self.fn.__doc__ or self.name)
        if self.capability == "write:high-risk":
            text += (
                "\n\nHigh-risk: this call does not mutate anything. It returns a preview and a "
                "pending_action_id; a human approves it on the server, then call "
                "execute_pending_action with that id."
            )
        elif self.live_guard:
            text += (
                "\n\nAgainst a live event this call is escalated to high-risk and returns a "
                "pending_action_id for out-of-band approval instead of mutating. On a draft or "
                "test-mode event it executes directly."
            )
        return text


REGISTRY: dict[str, ToolSpec] = {}


def tool(
    capability: str,
    *,
    live_guard: bool = False,
    name: str | None = None,
    title: str | None = None,
    preview: PreviewFn | None = None,
    money: tuple[str, ...] = (),
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        parameters = set(inspect.signature(fn).parameters)
        if unknown := sorted(set(money) - parameters):
            raise ValueError(f"{fn.__name__} declares money={unknown} it does not take")
        spec = ToolSpec(
            name=name or fn.__name__,
            fn=fn,
            capability=capability,
            live_guard=live_guard,
            preview=preview,
            title=title,
            money=money,
        )
        if spec.name in REGISTRY:
            raise ValueError(f"duplicate tool {spec.name!r}")
        REGISTRY[spec.name] = spec
        return fn

    return decorator


def static_capability(spec: ToolSpec, cfg: Config) -> str:
    """The tool's capability class after config reclassification.

    An operator can downgrade individual high-risk tools to plain ``write`` — an
    explicit, logged, per-tool decision (`MCP_AUTO_APPROVE`).
    """
    if spec.capability == "write:high-risk" and spec.name in cfg.auto_approve:
        return "write"
    return spec.capability


def enabled_tools(cfg: Config) -> list[ToolSpec]:
    specs = [spec for spec in REGISTRY.values() if cfg.tool_enabled(spec.name, static_capability(spec, cfg))]
    return sorted(specs, key=lambda s: s.name)  # deterministic tools/list order


async def run_tool(app: App, spec: ToolSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool call through the gate. Returns the tool's result, redacted."""
    capability = static_capability(spec, app.cfg)
    if not app.cfg.tool_enabled(spec.name, capability):
        raise PermissionError(f"tool {spec.name} is not enabled on this server")

    event_slug = kwargs.get("event")
    if event_slug is not None:
        kwargs["event"] = app.check_event(event_slug)

    for field in spec.money:
        if kwargs.get(field) is not None:
            kwargs[field] = (
                prices(kwargs[field], field=field)
                if isinstance(kwargs[field], dict)
                else price(kwargs[field], field=field)
            )

    if capability == "write" and spec.live_guard and kwargs.get("event"):
        event = await app.event(kwargs["event"])
        if event.get("live") and not event.get("testmode"):
            capability = "write:high-risk"

    if capability == "write:high-risk":
        return await _propose(app, spec, kwargs)
    return await _execute(app, spec, kwargs, capability=capability)


async def execute_approved(app: App, action_id: str) -> dict[str, Any]:
    """Run a previously approved pending action. Called by ``execute_pending_action``
    and by ``pretix-agent-mcp approve --run``."""
    action = app.pending.claim(action_id)
    spec = REGISTRY.get(action.tool)
    if spec is None:  # pragma: no cover - only reachable across versions
        app.pending.finish(action_id, "failed")
        raise ApprovalError(f"tool {action.tool!r} no longer exists")
    try:
        result = await _execute(
            app, spec, dict(action.args), capability="write:high-risk", action_id=action_id
        )
    except Exception:
        app.pending.finish(action_id, "failed")
        raise
    app.pending.finish(action_id, "executed")
    return result


async def _propose(app: App, spec: ToolSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    preview, snapshot = await _build_preview(app, spec, kwargs)
    action = app.pending.propose(spec.name, kwargs, preview, snapshot)
    app.audit.write(
        "proposed",
        tool=spec.name,
        args=kwargs,
        outcome="awaiting_approval",
        pending_action_id=action.id,
    )
    return {
        "status": "awaiting_approval",
        "pending_action_id": action.id,
        "preview": preview,
        "expires_at": _iso(action.expires_at),
        "approve_with": f"pretix-agent-mcp approve {action.id}",
        "next_step": (
            "Nothing was changed. Ask the operator to run the approve command on the server, "
            f"then call execute_pending_action with pending_action_id={action.id!r}."
        ),
    }


async def _build_preview(app: App, spec: ToolSpec, kwargs: dict[str, Any]) -> tuple[str, Any]:
    if spec.preview is not None:
        return await spec.preview(app, kwargs)
    args = json.dumps(redact_args(kwargs), ensure_ascii=False, sort_keys=True)
    return f"{spec.name} {args}", None


async def _execute(
    app: App,
    spec: ToolSpec,
    kwargs: dict[str, Any],
    *,
    capability: str,
    action_id: str | None = None,
) -> dict[str, Any]:
    try:
        result = await spec.fn(app, **kwargs)
    except (ValidationError, PretixError, ApprovalError, PermissionError) as exc:
        if capability != "read":
            app.audit.write(
                "failed",
                tool=spec.name,
                args=kwargs,
                outcome=type(exc).__name__,
                pending_action_id=action_id,
                error=str(exc),
            )
        raise
    payload = result if isinstance(result, dict) else {"result": result}
    if capability != "read":
        # A tool that partially succeeded says so in its own result (a batch that failed
        # halfway returns what it created); the audit record must not flatten that to "ok".
        outcome = payload.get("status") if isinstance(payload.get("status"), str) else "ok"
        app.audit.write("executed", tool=spec.name, args=kwargs, outcome=outcome, pending_action_id=action_id)
    return redact(payload, enabled=app.cfg.redact_pii)


def _iso(timestamp: float) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
