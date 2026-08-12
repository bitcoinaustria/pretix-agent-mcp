"""``pretix-agent-mcp`` — serve the MCP endpoint and approve high-risk actions.

The approval commands are the whole approval surface. They deliberately live on the
server, out of the agent's reach: a chat-based confirmation is forgeable by a
prompt-injected agent, a shell command on the server is not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from .config import Config, ConfigError, load
from .pending import ApprovalError, PendingStore
from .registry import execute_approved
from .server import build_app, serve_http, serve_stdio


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pretix-agent-mcp", description=__doc__)
    parser.add_argument("--config", help="path to a JSON config file (env vars take precedence)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=("http", "stdio"), default="http")

    sub.add_parser("pending", help="list high-risk actions awaiting approval")
    sub.add_parser("tools", help="list the tools this configuration exposes")

    approve = sub.add_parser("approve", help="approve a pending high-risk action")
    approve.add_argument("id")
    approve.add_argument("--run", action="store_true", help="execute it immediately instead of waiting for the agent")

    reject = sub.add_parser("reject", help="reject a pending high-risk action")
    reject.add_argument("id")

    args = parser.parse_args(argv)
    try:
        cfg = load(config_file=args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        return _dispatch(args, cfg)
    except (ApprovalError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, cfg: Config) -> int:
    match args.command:
        case "serve":
            if args.transport == "stdio":
                serve_stdio(cfg)
            else:
                serve_http(cfg)
            return 0
        case "pending":
            return _pending(cfg)
        case "tools":
            return _tools(cfg)
        case "approve":
            return _approve(cfg, args.id, run=args.run)
        case "reject":
            store = PendingStore(cfg.state_db, cfg.approval_ttl_seconds)
            store.decide(args.id, "rejected")
            print(f"rejected {args.id}")
            return 0
    return 2  # pragma: no cover - argparse rejects unknown commands


def _pending(cfg: Config) -> int:
    store = PendingStore(cfg.state_db, cfg.approval_ttl_seconds)
    store.expire_stale()
    actions = store.list("pending")
    if not actions:
        print("nothing awaiting approval")
        return 0
    for action in actions:
        left = int(action.expires_at - time.time())
        print(f"\n{action.id}  {action.tool}  (expires in {left}s)")
        for line in action.preview.splitlines():
            print(f"    {line}")
    print(f"\napprove with: pretix-agent-mcp approve <id>   ({len(actions)} pending)")
    return 0


def _tools(cfg: Config) -> int:
    from .registry import REGISTRY, enabled_tools, static_capability

    enabled = {spec.name for spec in enabled_tools(cfg)}
    for spec in sorted(REGISTRY.values(), key=lambda s: s.name):
        mark = "on " if spec.name in enabled else "off"
        capability = static_capability(spec, cfg)
        guard = " [live-guard]" if spec.live_guard else ""
        print(f"{mark} {spec.name:28} {capability}{guard}")
    return 0


def _approve(cfg: Config, action_id: str, *, run: bool) -> int:
    app = build_app(cfg)
    action = app.pending.decide(action_id, "approved")
    app.audit.write("approved", tool=action.tool, args=action.args, pending_action_id=action.id, outcome="approved")
    print(f"approved {action.id} ({action.tool})")
    if not run:
        print("the agent can now call execute_pending_action with this id")
        return 0
    result = asyncio.run(_run(app, action_id))
    print(json.dumps(result, indent=2, default=str))
    return 0


async def _run(app: object, action_id: str) -> dict:
    from .registry import App

    assert isinstance(app, App)
    try:
        return await execute_approved(app, action_id)
    finally:
        await app.pretix.aclose()
