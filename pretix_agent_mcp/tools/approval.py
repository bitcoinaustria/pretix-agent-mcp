"""The agent-facing half of the approval flow.

The agent can see what it proposed and run it once a human approved it out of band.
It cannot approve anything: there is no tool that writes the approval state.
"""

from __future__ import annotations

from ..registry import App, execute_approved, tool
from ..validate import ValidationError


def _action_id(value: object) -> str:
    if not isinstance(value, str) or not value.isalnum() or not 4 <= len(value) <= 32:
        raise ValidationError(f"invalid pending_action_id: {value!r}")
    return value


@tool("read")
async def get_pending_action(app: App, pending_action_id: str) -> dict:
    """Check the state of a high-risk action you proposed earlier.

    Returns its state ('pending', 'approved', 'executed', 'rejected', 'expired'),
    the preview a human sees, and when it expires. A human must approve it on the
    server before execute_pending_action will run it.
    """
    action = app.pending.get(_action_id(pending_action_id))
    if action is None:
        raise ValidationError(f"no pending action {pending_action_id!r}")
    return {
        "pending_action_id": action.id,
        "tool": action.tool,
        "state": "expired" if action.expired else action.state,
        "preview": action.preview,
        "expires_at": action.expires_at,
    }


@tool("write")
async def execute_pending_action(app: App, pending_action_id: str) -> dict:
    """Execute a high-risk action that a human approved on the server.

    Fails while the action is still awaiting approval, and after it expired.
    """
    return await execute_approved(app, _action_id(pending_action_id))
