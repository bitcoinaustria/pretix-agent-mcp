"""Server configuration. Environment wins over the optional config file.

Defaults are closed: localhost bind, ``read`` capabilities only, PII redacted,
no writes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CAPABILITIES = ("read", "write", "write:high-risk")

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    pass


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Config:
    pretix_base_url: str
    pretix_api_token: str
    organizer: str
    event_allowlist: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=lambda: ["read"])
    tool_allowlist: list[str] = field(default_factory=list)
    # High-risk tools the operator reclassified to plain `write` (no approval ceremony).
    auto_approve: list[str] = field(default_factory=list)
    pii_mode: str = "redacted"
    mcp_bearer_token: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "info"
    audit_log: Path = Path("audit.jsonl")
    state_db: Path = Path("pending-actions.sqlite3")
    approval_ttl_seconds: int = 900
    approvals_web: bool = False
    approvals_web_token: str | None = None
    scan_cap: int = 5000

    @property
    def redact_pii(self) -> bool:
        return self.pii_mode != "full"

    def capability_enabled(self, capability: str) -> bool:
        return capability in self.capabilities

    def tool_enabled(self, name: str, capability: str) -> bool:
        if self.tool_allowlist:
            return name in self.tool_allowlist
        return self.capability_enabled(capability)

    def event_allowed(self, slug: str) -> bool:
        return not self.event_allowlist or slug in self.event_allowlist


def _from_file(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    return raw


def load(env: dict[str, str] | None = None, config_file: str | Path | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    file_values = _from_file(Path(config_file or env.get("PRETIX_AGENT_CONFIG", "config.json")))

    def get(key: str, default: str | None = None) -> str | None:
        if key in env and env[key] != "":
            return env[key]
        value = file_values.get(key.lower()) or file_values.get(key)
        return str(value) if value is not None and not isinstance(value, (list, dict)) else default

    def get_list(key: str) -> list[str]:
        if key in env:
            return _split(env[key])
        value = file_values.get(key.lower()) or file_values.get(key)
        if isinstance(value, list):
            return [str(v) for v in value]
        return _split(str(value)) if value else []

    base_url = (get("PRETIX_BASE_URL") or "").rstrip("/")
    token = get("PRETIX_API_TOKEN") or ""
    organizer = get("PRETIX_ORGANIZER") or ""
    missing = [
        name
        for name, value in (
            ("PRETIX_BASE_URL", base_url),
            ("PRETIX_API_TOKEN", token),
            ("PRETIX_ORGANIZER", organizer),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"missing required configuration: {', '.join(missing)}")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("PRETIX_BASE_URL must be an http(s) URL")

    capabilities = get_list("MCP_CAPABILITIES") or ["read"]
    unknown = [c for c in capabilities if c not in CAPABILITIES]
    if unknown:
        raise ConfigError(f"unknown capability class(es): {', '.join(unknown)}")

    pii_mode = (get("PII_MODE") or "redacted").lower()
    if pii_mode not in {"redacted", "full"}:
        raise ConfigError("PII_MODE must be 'redacted' or 'full'")

    cfg = Config(
        pretix_base_url=base_url,
        pretix_api_token=token,
        organizer=organizer,
        event_allowlist=get_list("PRETIX_EVENT_ALLOWLIST"),
        capabilities=capabilities,
        tool_allowlist=get_list("MCP_TOOL_ALLOWLIST"),
        auto_approve=get_list("MCP_AUTO_APPROVE"),
        pii_mode=pii_mode,
        mcp_bearer_token=get("MCP_BEARER_TOKEN"),
        host=get("MCP_HOST") or "127.0.0.1",
        port=int(get("MCP_PORT") or 8765),
        log_level=(get("LOG_LEVEL") or "info").lower(),
        audit_log=Path(get("AUDIT_LOG") or "audit.jsonl"),
        state_db=Path(get("STATE_DB") or "pending-actions.sqlite3"),
        approval_ttl_seconds=int(get("APPROVAL_TTL_SECONDS") or 900),
        approvals_web=(get("APPROVALS_WEB") or "").lower() in _TRUE,
        approvals_web_token=get("APPROVALS_WEB_TOKEN"),
        scan_cap=int(get("SALES_SCAN_CAP") or 5000),
    )
    # Validate the pinned organizer with the same rules agent input gets.
    from .validate import ValidationError, slug

    try:
        slug(cfg.organizer, field="PRETIX_ORGANIZER")
        for event in cfg.event_allowlist:
            slug(event, field="PRETIX_EVENT_ALLOWLIST entry")
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    return cfg


def check_http_bind(cfg: Config) -> None:
    """Refuse to start on a non-localhost bind without a bearer token."""
    localhost = cfg.host in {"127.0.0.1", "::1", "localhost"}
    if not cfg.mcp_bearer_token:
        if not localhost:
            raise ConfigError(
                f"refusing to bind {cfg.host} without MCP_BEARER_TOKEN. "
                "Set a bearer token, or bind 127.0.0.1 for local-only use."
            )
        raise ConfigError("MCP_BEARER_TOKEN is required for the HTTP transport")
    if len(cfg.mcp_bearer_token) < 24:
        raise ConfigError("MCP_BEARER_TOKEN must be at least 24 characters")
    if cfg.approvals_web and not cfg.approvals_web_token:
        raise ConfigError("APPROVALS_WEB requires APPROVALS_WEB_TOKEN")
