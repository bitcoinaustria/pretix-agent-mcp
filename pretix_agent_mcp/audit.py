"""Append-only JSONL audit log.

Every write and every high-risk lifecycle event lands here. Arguments are always
run through :func:`pretix_agent_mcp.redact.redact_args` — the log is a file that
outlives the request, so it must never hold unredacted PII, and it never sees a
token because no token is ever a tool argument.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .redact import redact_args


class Audit:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()  # ponytail: one process; use syslog/DB if you shard

    def write(
        self,
        event: str,
        *,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        outcome: str | None = None,
        pending_action_id: str | None = None,
        **extra: Any,
    ) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            "tool": tool,
            "outcome": outcome,
            "pending_action_id": pending_action_id,
            "args": redact_args(args) if args else None,
            **extra,
        }
        line = json.dumps({k: v for k, v in record.items() if v is not None}, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
