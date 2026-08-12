"""Tool modules. Importing this package registers every tool in the registry.

Each module owns one pretix domain and declares tools with
:func:`pretix_agent_mcp.registry.tool`.
"""

from __future__ import annotations

from . import approval, catalog, checkin, events, orders, subevents, vouchers  # noqa: F401

__all__ = ["approval", "catalog", "checkin", "events", "orders", "subevents", "vouchers"]
