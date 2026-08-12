"""Pending high-risk actions, stored in SQLite.

Why a store and not a ``confirm: true`` parameter: the agent sets its own parameters,
so an in-band confirmation is forgeable by a prompt-injected agent. Approval happens
out of band (``pretix-agent-mcp approve <id>`` on the server), which the agent cannot
reach. The ID handed back to the agent is a server-minted handle, the pattern the
2026-07-28 spec prescribes for cross-call state.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id          TEXT PRIMARY KEY,
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL,
    preview     TEXT NOT NULL,
    snapshot    TEXT,
    state       TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    decided_at  REAL,
    executed_at REAL
);
"""


class ApprovalError(RuntimeError):
    """Raised when a pending action is missing, expired, or not approved."""


@dataclass(frozen=True)
class PendingAction:
    id: str
    tool: str
    args: dict[str, Any]
    preview: str
    snapshot: Any
    state: str
    created_at: float
    expires_at: float

    @property
    def expired(self) -> bool:
        return self.state == "pending" and time.time() > self.expires_at


class PendingStore:
    def __init__(self, path: Path | str, ttl_seconds: int = 900) -> None:
        self.path = Path(path)
        self.ttl = ttl_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        return db

    def propose(self, tool: str, args: dict[str, Any], preview: str, snapshot: Any = None) -> PendingAction:
        now = time.time()
        action = PendingAction(
            # 64 bits: the id is handed to the agent, so it need not be unguessable, but a
            # birthday collision on a TEXT PRIMARY KEY would surface as an INSERT error.
            id=secrets.token_hex(8),
            tool=tool,
            args=args,
            preview=preview,
            snapshot=snapshot,
            state="pending",
            created_at=now,
            expires_at=now + self.ttl,
        )
        with self._db() as db:
            db.execute(
                "INSERT INTO pending_actions"
                " (id, tool, args, preview, snapshot, state, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    action.id,
                    tool,
                    json.dumps(args, default=str),
                    preview,
                    json.dumps(snapshot, default=str) if snapshot is not None else None,
                    "pending",
                    action.created_at,
                    action.expires_at,
                ),
            )
        return action

    def get(self, action_id: str) -> PendingAction | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
        return _row_to_action(row) if row else None

    def list(self, state: str | None = "pending") -> list[PendingAction]:
        query = "SELECT * FROM pending_actions"
        params: tuple[Any, ...] = ()
        if state:
            query += " WHERE state = ?"
            params = (state,)
        query += " ORDER BY created_at"
        with self._db() as db:
            rows = db.execute(query, params).fetchall()
        return [_row_to_action(row) for row in rows]

    def decide(self, action_id: str, state: str) -> PendingAction:
        """Approve or reject a pending action. Only a live pending action can be decided.

        The state change is one conditional UPDATE, so two concurrent approvals cannot
        both win. Diagnosing *why* an update matched nothing happens afterwards, outside
        the transaction — raising inside it would roll back the bookkeeping.
        """
        if state not in {"approved", "rejected"}:
            raise ValueError("state must be 'approved' or 'rejected'")
        now = time.time()
        with self._db() as db:
            changed = db.execute(
                "UPDATE pending_actions SET state = ?, decided_at = ?"
                " WHERE id = ? AND state = 'pending' AND expires_at > ?",
                (state, now, action_id, now),
            ).rowcount
        if changed:
            action = self.get(action_id)
            assert action is not None
            return action
        raise self._why_not(action_id, wanted="pending")

    def claim(self, action_id: str) -> PendingAction:
        """Atomically move an approved action to ``executing``, so it runs at most once."""
        with self._db() as db:
            changed = db.execute(
                "UPDATE pending_actions SET state = 'executing', executed_at = ?"
                " WHERE id = ? AND state = 'approved'",
                (time.time(), action_id),
            ).rowcount
        if changed:
            action = self.get(action_id)
            assert action is not None
            return action
        raise self._why_not(action_id, wanted="approved")

    def _why_not(self, action_id: str, *, wanted: str) -> ApprovalError:
        action = self.get(action_id)
        if action is None:
            return ApprovalError(f"no pending action {action_id!r}")
        if action.state == "pending" and time.time() > action.expires_at:
            self._set_state(action_id, "expired")
            return ApprovalError(f"action {action_id} expired")
        if action.state == "pending" and wanted == "approved":
            return ApprovalError(
                f"action {action_id} is still awaiting approval — a human must run "
                f"`pretix-agent-mcp approve {action_id}` on the server"
            )
        return ApprovalError(f"action {action_id} is {action.state}, not {wanted}")

    def _set_state(self, action_id: str, state: str) -> None:
        with self._db() as db:
            db.execute("UPDATE pending_actions SET state = ? WHERE id = ?", (state, action_id))

    def finish(self, action_id: str, outcome: str) -> None:
        with self._db() as db:
            db.execute("UPDATE pending_actions SET state = ? WHERE id = ?", (outcome, action_id))

    def expire_stale(self) -> int:
        with self._db() as db:
            cur = db.execute(
                "UPDATE pending_actions SET state = 'expired' WHERE state = 'pending' AND expires_at < ?",
                (time.time(),),
            )
            return cur.rowcount


def _row_to_action(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        id=row["id"],
        tool=row["tool"],
        args=json.loads(row["args"]),
        preview=row["preview"],
        snapshot=json.loads(row["snapshot"]) if row["snapshot"] else None,
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
