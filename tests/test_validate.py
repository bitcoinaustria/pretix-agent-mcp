"""Input validation is the control that makes arbitrary REST access impossible.

Every agent-supplied value that becomes a URL path segment goes through here, so these
tests are about what gets *rejected* before an HTTP request is ever built.
"""

from __future__ import annotations

import pytest

from pretix_agent_mcp.validate import ValidationError, object_id, order_code, page_size, path_segments, slug

ESCAPE_ATTEMPTS = [
    "../../organizers/other-org/events",
    "..",
    ".",
    "conf27/../../secret",
    "conf27/orders",
    "/etc/passwd",
    "conf27?export=true",
    "conf27#fragment",
    "conf27%2f..%2f",
    "conf27 ",
    " conf27",
    "conf 27",
    "conf\n27",
    "conf\x0027",
    "http://evil.example/x",
    "-leading-dash-is-fine-but-not-first",
    "",
    "ä" * 3,
    "x" * 65,
]


@pytest.mark.parametrize("value", ESCAPE_ATTEMPTS)
def test_slug_rejects_anything_that_could_leave_the_resource(value):
    with pytest.raises(ValidationError):
        slug(value)


@pytest.mark.parametrize("value", ["conf27", "c", "Conf-2027", "conf.27_x", "2027"])
def test_slug_accepts_real_slugs(value):
    assert slug(value) == value


@pytest.mark.parametrize("value", [None, 1, True, [], {}, b"conf27"])
def test_slug_rejects_non_strings(value):
    with pytest.raises(ValidationError):
        slug(value)


def test_order_code_is_uppercased():
    assert order_code(" ab34c ") == "AB34C"


@pytest.mark.parametrize("value", ["AB3", "AB-34C", "AB/34", "abc*", "", "A" * 17, None, 12345])
def test_order_code_rejects_junk(value):
    with pytest.raises(ValidationError):
        order_code(value)


@pytest.mark.parametrize("value", [0, -1, "0", "-5", "1.5", 1.0, True, False, "١٢٣", " 5", "5 ", "5a", None])
def test_object_id_rejects_non_positive_integers(value):
    with pytest.raises(ValidationError):
        object_id(value)


def test_object_id_accepts_ints_and_plain_numeric_strings():
    assert object_id(42) == 42
    assert object_id("42") == 42


def test_path_segments_is_a_second_line_of_defence():
    assert path_segments("events", "conf27", "orders") == "events/conf27/orders"
    for bad in ("a/b", "..", ".", "", "a?b", "a#b", "a%2fb", "a\x00b", "a\\b"):
        with pytest.raises(ValidationError):
            path_segments("events", bad)


def test_page_size_is_capped():
    assert page_size(None) == 50
    assert page_size(10) == 10
    assert page_size(5000) == 50
    with pytest.raises(ValidationError):
        page_size(0)


def test_object_id_is_bounded_above():
    """An unbounded count is an allocation weapon: create_vouchers_batch(count=10**9)."""
    from pretix_agent_mcp.validate import MAX_ID

    assert object_id(MAX_ID) == MAX_ID
    with pytest.raises(ValidationError):
        object_id(MAX_ID + 1)
