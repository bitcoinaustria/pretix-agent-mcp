"""Helpers shared by tool modules.

Tool results land in an LLM context, so results are *shaped*: a handful of useful
fields per object rather than pretix's full representation. Anything a tool omits is
still reachable through the matching ``get_*`` tool.
"""

from __future__ import annotations

from typing import Any

# pretix i18n fields ("name", "location", ...) come back as {"en": "...", "de": "..."}.
I18N_PREFERENCE = ("en", "de")


def i18n(value: Any) -> Any:
    """Flatten a pretix i18n field to a single string."""
    if isinstance(value, dict):
        for lang in I18N_PREFERENCE:
            if value.get(lang):
                return value[lang]
        for candidate in value.values():
            if candidate:
                return candidate
        return None
    return value


def pick(obj: dict[str, Any] | None, *fields: str) -> dict[str, Any]:
    """Subset a pretix object, flattening i18n fields on the way out."""
    if not obj:
        return {}
    return {field: i18n(obj.get(field)) for field in fields if field in obj}


def clean(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so a PATCH only touches the fields the agent named."""
    return {key: value for key, value in payload.items() if value is not None}


def listing(
    items: list[Any], *, total: int | None = None, truncated: bool = False, **extra: Any
) -> dict[str, Any]:
    """Uniform shape for list results, so counts and truncation are never implicit."""
    result: dict[str, Any] = {"count": len(items), "results": items}
    if total is not None:
        result["total_matching"] = total
    if truncated:
        result["truncated"] = True
        result["note"] = "Result capped server-side; narrow the filters for a complete answer."
    result.update(extra)
    return result
