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

import re
from typing import Any

# Keys whose values are masked in `redacted` mode, matched case-insensitively against
# the last path element. pretix uses these across orders, positions, invoices and
# waiting-list entries.
NAME_KEYS = {
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

# A bare `name` is usually an object label — an event, a product, a quota, a check-in
# list — and masking those would make every result unreadable. It is a person's name
# only inside a record that also carries personal fields, which is what PERSON_MARKERS
# detects (a waiting-list entry, an invoice address).
PERSON_MARKERS = {"email", "attendee_email", "name_parts", "attendee_name_parts", "attendee_name"}
CONTEXTUAL_NAME_KEYS = {"name"}

# `name_parts` is a dict of name components; mask every leaf inside it.
CONTAINER_KEYS = {"name_parts", "attendee_name_parts", "invoice_address"}
# Leaves inside those containers that identify nobody but answer real questions
# (country for VAT/reverse-charge, the name scheme for form handling).
SAFE_IN_CONTAINER = {"country", "is_business", "id", "scheme", "_scheme"}

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
        # A `name` next to an email or a name_parts block belongs to a person, not an object.
        personal = _in_pii or bool(PERSON_MARKERS & {str(k).lower() for k in data})
        for key, value in data.items():
            lkey = key.lower() if isinstance(key, str) else str(key)
            inside = _in_pii or lkey in CONTAINER_KEYS
            if isinstance(value, (dict, list)):
                out[key] = redact(value, enabled=True, _key=lkey, _in_pii=inside)
            elif lkey in SAFE_IN_CONTAINER and lkey not in PII_KEYS:
                out[key] = value
            elif personal and lkey in CONTEXTUAL_NAME_KEYS:
                out[key] = mask_name(value) if isinstance(value, str) else value
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


# Free text that never went through a pretix field name: an API error body. The key-based
# masking above cannot help there, so addresses are found by shape instead.
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def scrub_text(text: str) -> str:
    """Mask email addresses anywhere in free text.

    pretix quotes the offending value in some validation errors, and those error bodies are
    echoed to the agent to explain what went wrong. Always on, in either PII mode: an error
    message is never the place a deployment needs a real address.
    """
    return EMAIL_IN_TEXT.sub(lambda match: mask_email(match.group()), text)


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Redact tool arguments for the audit log. Always on — the audit log is a file on
    disk that outlives the request and must never hold unredacted PII."""
    return redact(args, enabled=True)
