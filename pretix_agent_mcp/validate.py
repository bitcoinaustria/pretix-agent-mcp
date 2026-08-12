"""Input validation for every agent-supplied value that becomes a URL path segment.

This is a security control, not ergonomics: it is what makes "arbitrary pretix REST
access through this server is impossible" true. Nothing reaches
:mod:`pretix_agent_mcp.pretix` as a path segment without passing through here.
"""

from __future__ import annotations

import re

SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-_]*$")
# pretix order codes are drawn from an unambiguous alphabet (no 0/1/I/O).
ORDER_CODE_RE = re.compile(r"^[A-Z0-9]{4,16}$")


class ValidationError(ValueError):
    """Raised when an agent-supplied value is rejected before any HTTP request is built."""


def slug(value: object, *, field: str = "slug") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if len(value) > 64 or not SLUG_RE.match(value):
        raise ValidationError(f"invalid {field}: {value!r}")
    return value


def order_code(value: object, *, field: str = "order code") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    upper = value.strip().upper()
    if not ORDER_CODE_RE.match(upper):
        raise ValidationError(f"invalid {field}: {value!r}")
    return upper


# No pretix resource id — and no batch size worth honouring — comes near this. An upper
# bound keeps an agent-supplied count from being used to allocate its way to a crash.
MAX_ID = 2**31 - 1


def object_id(value: object, *, field: str = "id") -> int:
    """Positive integer resource IDs only. Rejects bools, floats and numeric strings
    with any decoration (whitespace, signs, unicode digits)."""
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer")
    if isinstance(value, int):
        ivalue = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value):
        ivalue = int(value)
    else:
        raise ValidationError(f"invalid {field}: {value!r}")
    if not 1 <= ivalue <= MAX_ID:
        raise ValidationError(f"invalid {field}: {value!r}")
    return ivalue


def path_segments(*segments: str) -> str:
    """Join already-validated segments, refusing anything that could escape the path.

    Belt and braces: every caller validates first, this catches a caller that forgot.
    """
    for seg in segments:
        if not isinstance(seg, str) or not seg or "/" in seg or "\\" in seg or seg in {".", ".."}:
            raise ValidationError(f"illegal path segment: {seg!r}")
        if any(c in seg for c in "?#%") or any(ord(c) < 0x20 for c in seg):
            raise ValidationError(f"illegal path segment: {seg!r}")
    return "/".join(segments)


# Money is a decimal string with at most two places, everywhere pretix takes an amount.
PRICE_RE = re.compile(r"^-?[0-9]{1,10}(\.[0-9]{1,2})?$")


def price(value: object, *, field: str = "price") -> str | None:
    """Validate an agent-supplied money value. ``None`` passes through unchanged.

    The one money validator in this codebase — every price, amount, fee, tax rate and
    voucher value goes through it, applied by the registry to the parameters a tool
    declares in ``money=``. It refuses floats instead of rounding them (a float price is a
    rounding bug waiting to be charged to a customer), which is also what keeps ``nan``,
    ``inf``, ``1e5`` and a third decimal place out of a pretix payload.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a decimal string like '23.00', not {type(value).__name__}")
    if not PRICE_RE.match(value.strip()):
        raise ValidationError(f"invalid {field}: {value!r} — expected a decimal string like '23.00'")
    return value.strip()


def prices(value: object, *, field: str) -> dict[str, str]:
    """A mapping of object id to price, as the per-date price overrides take."""
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be a mapping of product id to price")
    out = {}
    for key, amount in value.items():
        if amount is None:
            raise ValidationError(f"{field}[{key}] must be a price, not null")
        out[key] = price(amount, field=f"{field}[{key}]")
    return out


def page_size(value: object, *, default: int = 50, cap: int = 50) -> int:
    if value is None:
        return default
    size = object_id(value, field="limit")
    return min(size, cap)
