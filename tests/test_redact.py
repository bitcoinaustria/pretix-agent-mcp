"""PII redaction is on by default, and the operator — never the agent — turns it off."""

from __future__ import annotations

from pretix_agent_mcp.redact import mask_email, mask_name, mask_phone, redact, redact_args, scrub_text

ORDER = {
    "code": "ABC12",
    "status": "p",
    "total": "42.00",
    "email": "maria.kowalski@example.org",
    "phone": "+43 660 1234567",
    "invoice_address": {
        "name": "Maria Kowalski",
        "company": "Kowalski GmbH",
        "street": "Hauptstrasse 1",
        "city": "Wien",
        "zipcode": "1010",
        "country": "AT",
        "vat_id": "ATU12345678",
    },
    "positions": [
        {
            "id": 7,
            "item": 3,
            "price": "42.00",
            "attendee_name": "Maria Kowalski",
            "attendee_email": "maria.kowalski@example.org",
            "attendee_name_parts": {
                "given_name": "Maria",
                "family_name": "Kowalski",
                "_scheme": "given_family",
            },
            "answers": [{"question": 1, "answer": "vegetarian"}],
        }
    ],
    "comment": "called about invoice",
}


def test_masking_primitives():
    assert mask_email("maria@example.org") == "m***@example.org"
    assert mask_name("Maria Kowalski") == "M*** K***"
    assert mask_phone("+43 660 1234567") == "***67"


def test_pii_is_masked_everywhere_it_appears():
    out = redact(ORDER)
    flat = repr(out)
    for secret in (
        "maria.kowalski@example.org",
        "Maria",
        "Kowalski",
        "Hauptstrasse",
        "ATU12345678",
        "vegetarian",
        "called about invoice",
    ):
        assert secret not in flat, f"{secret} leaked: {flat}"


def test_operational_fields_pass_through():
    out = redact(ORDER)
    assert out["code"] == "ABC12"
    assert out["status"] == "p"
    assert out["total"] == "42.00"
    assert out["positions"][0]["price"] == "42.00"
    assert out["positions"][0]["id"] == 7
    assert out["invoice_address"]["country"] == "AT"


def test_full_mode_returns_the_data_unchanged():
    assert redact(ORDER, enabled=False) == ORDER


def test_the_input_is_not_mutated():
    redact(ORDER)
    assert ORDER["email"] == "maria.kowalski@example.org"


def test_audit_args_are_always_redacted():
    out = redact_args({"event": "conf27", "email": "maria@example.org", "attendee_name": "Maria Kowalski"})
    assert out["event"] == "conf27"
    assert out["email"] == "m***@example.org"
    assert out["attendee_name"] == "M*** K***"


def test_unknown_personal_looking_keys_inside_a_name_container_are_masked():
    out = redact({"attendee_name_parts": {"middle_name": "Anna", "salutation": "Dr"}})
    assert "Anna" not in repr(out)


def test_free_text_addresses_are_masked_by_shape():
    """Key-based masking cannot reach into an error string, so scrub_text works on shape.
    This is the path a pretix validation error takes on its way into the agent's context."""
    text = 'pretix API error 400: {"email": ["anna.schmid@example.com is already registered"]}'
    out = scrub_text(text)
    assert "anna.schmid@example.com" not in out
    assert "a***@example.com" in out
    assert "already registered" in out, "the useful part of the error must survive"


def test_scrubbing_leaves_ordinary_text_alone():
    assert scrub_text("quota 3 is sold out; order ABC12 expires 2027-06-01") == (
        "quota 3 is sold out; order ABC12 expires 2027-06-01"
    )


def test_every_address_in_a_body_is_masked_not_just_the_first():
    out = scrub_text("a@x.example.org and b@y.example.org both exist")
    assert "a@x.example.org" not in out and "b@y.example.org" not in out
