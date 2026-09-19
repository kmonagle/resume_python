"""Why this file exists: the schemas are the trust boundary for user input. These
tests cover the security-relevant cases (URL schemes) and the form-specific ones
(blank fields, code format, expiry), matching the Go and Next.js suites.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas import CreateLink, SetActive, link_to_dto
from tests.test_domain import make_link

OK = {"targetUrl": "https://example.com/path"}


def parse(**extra) -> CreateLink:
    return CreateLink.model_validate({**OK, **extra})


def fields_with_errors(**extra) -> set[str]:
    # JS/TS vs Python: `pytest.raises` is `expect(() => ...).toThrow()`. Unlike
    # Go, Python validation FAILS BY RAISING; pydantic collects every problem into
    # one ValidationError, and `.errors()` lists them.
    with pytest.raises(ValidationError) as caught:
        parse(**extra)
    return {e["loc"][0] for e in caught.value.errors()}


def test_accepts_a_minimal_link():
    assert parse().target_url == "https://example.com/path"


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "data:text/html,hi", "ftp://x.com/a", "nope", "http://", 42]
)
def test_rejects_non_http_targets(url):
    with pytest.raises(ValidationError):
        CreateLink.model_validate({"targetUrl": url})


def test_target_url_is_required():
    with pytest.raises(ValidationError):
        CreateLink.model_validate({})


def test_blank_optional_fields_are_treated_as_absent():
    link = parse(title="  ", expiresAt="", maxClicks="", shortCode="")
    assert (link.title, link.expires_at, link.max_clicks, link.short_code) == (
        None,
        None,
        None,
        None,
    )


def test_title_is_trimmed_and_capped():
    assert parse(title="  Docs  ").title == "Docs"
    assert fields_with_errors(title="x" * 101) == {"title"}


@pytest.mark.parametrize("value", [0, -1, 1.5, "abc", True, 2_000_000])
def test_max_clicks_must_be_a_whole_number_in_range(value):
    assert fields_with_errors(maxClicks=value) == {"maxClicks"}


def test_max_clicks_accepts_a_valid_number():
    assert parse(maxClicks=5).max_clicks == 5


@pytest.mark.parametrize(
    ("code", "valid"),
    [
        ("my-promo_1", True),
        ("abc", True),
        ("ab", False),
        ("has space", False),
        ("a/b/c", False),
    ],
)
def test_short_code_format(code, valid):
    if valid:
        assert parse(shortCode=code).short_code == code
    else:
        assert fields_with_errors(shortCode=code) == {"shortCode"}


def test_expiry_must_be_an_iso_datetime_in_the_future():
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert parse(expiresAt=future).expires_at is not None
    assert fields_with_errors(expiresAt=past) == {"expiresAt"}
    assert fields_with_errors(expiresAt="2030-01-01") == {"expiresAt"}  # no time, no offset


def test_set_active_is_strict_about_booleans():
    assert SetActive.model_validate({"isActive": False}).is_active is False
    for bad in ("yes", 1, None):
        with pytest.raises(ValidationError):
            SetActive.model_validate({"isActive": bad})


def test_dto_uses_camel_case_and_iso_milliseconds():
    dto = link_to_dto(make_link(max_clicks=2, click_count=2), datetime(2026, 1, 1, tzinfo=UTC))
    body = dto.model_dump(by_alias=True)
    assert body["createdAt"] == "2026-01-01T00:00:00.000Z"
    assert body["shortCode"] == "abc1234"
    assert body["status"] == "max_clicks"
    assert body["expiresAt"] is None
