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
    if ivalue < 1:
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


def page_size(value: object, *, default: int = 50, cap: int = 50) -> int:
    if value is None:
        return default
    size = object_id(value, field="limit")
    return min(size, cap)
