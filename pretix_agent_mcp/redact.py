"""PII redaction.

Every tool result is assumed to end up in an LLM context, potentially in third-party
model logs. So results are masked by default and the operator — never the agent —
decides otherwise via ``PII_MODE=full``.

The masking works on a key allowlist walked over the whole response tree: any key in
:data:`PII_KEYS` is masked wherever it appears, at any depth. That is deliberately
blunt — a new pretix field we have never seen is masked if its name looks personal,
and passes through if it does not.
"""

from __future__ import annotations

from typing import Any

# Keys whose values are masked in `redacted` mode, matched case-insensitively against
# the last path element. pretix uses these across orders, positions, invoices and
# waiting-list entries.
NAME_KEYS = {
    "name",
    "attendee_name",
    "full_name",
    "given_name",
    "family_name",
    "company",
    "invoice_name",
    "contact_name",
}
EMAIL_KEYS = {"email", "attendee_email", "invoice_email", "contact_email", "sender"}
PHONE_KEYS = {"phone", "telephone", "mobile", "invoice_phone"}
ADDRESS_KEYS = {
    "street",
    "address",
    "zipcode",
    "city",
    "vat_id",
    "internal_reference",
    "invoice_address",
    "attendee_address",
}
# Free-text fields that routinely contain names or notes about a customer.
FREETEXT_KEYS = {"comment", "checkin_text", "answer", "text", "message"}

PII_KEYS = NAME_KEYS | EMAIL_KEYS | PHONE_KEYS | ADDRESS_KEYS | FREETEXT_KEYS

# `name_parts` is a dict of name components; mask every leaf inside it.
CONTAINER_KEYS = {"name_parts", "attendee_name_parts", "invoice_address"}

REDACTED = "***"


def mask_email(value: str) -> str:
    local, sep, domain = value.partition("@")
    if not sep:
        return mask_name(value)
    return f"{local[:1] or ''}{REDACTED}@{domain}"


def mask_name(value: str) -> str:
    parts = [p for p in value.split() if p]
    if not parts:
        return REDACTED
    return " ".join(f"{p[:1]}{REDACTED}" for p in parts)


def mask_phone(value: str) -> str:
    digits = [c for c in value if c.isdigit()]
    return f"{REDACTED}{''.join(digits[-2:])}" if len(digits) >= 2 else REDACTED


def _mask_scalar(key: str, value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str) or not value:
        return value
    if key in EMAIL_KEYS or "@" in value and key in PII_KEYS:
        return mask_email(value)
    if key in PHONE_KEYS:
        return mask_phone(value)
    if key in NAME_KEYS:
        return mask_name(value)
    return REDACTED


def redact(data: Any, *, enabled: bool = True, _key: str = "", _in_pii: bool = False) -> Any:
    """Return a copy of ``data`` with PII-looking values masked.

    ``enabled=False`` (``PII_MODE=full``) returns the data unchanged.
    """
    if not enabled:
        return data
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            lkey = key.lower() if isinstance(key, str) else str(key)
            inside = _in_pii or lkey in CONTAINER_KEYS
            if isinstance(value, (dict, list)):
                out[key] = redact(value, enabled=True, _key=lkey, _in_pii=inside)
            elif inside or lkey in PII_KEYS:
                out[key] = _mask_scalar(lkey if lkey in PII_KEYS else _pii_kind(lkey, value), value)
            else:
                out[key] = value
        return out
    if isinstance(data, list):
        return [redact(v, enabled=True, _key=_key, _in_pii=_in_pii) for v in data]
    if _in_pii:
        return _mask_scalar(_key, data)
    return data


def _pii_kind(key: str, value: Any) -> str:
    """Pick a masking style for a leaf inside a PII container whose own key is unknown."""
    if isinstance(value, str) and "@" in value:
        return "email"
    if key in PHONE_KEYS:
        return "phone"
    return "name"


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Redact tool arguments for the audit log. Always on — the audit log is a file on
    disk that outlives the request and must never hold unredacted PII."""
    return redact(args, enabled=True)
