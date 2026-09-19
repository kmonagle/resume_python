"""Why this file exists: pins down the domain rules (status precedence, code
generation) that must match the contract and the other implementations.

JS/TS vs Python: pytest is the de-facto test runner (Jest's role). Tests are plain
functions whose names start with `test_`; assertions are the built-in `assert`
statement, and pytest rewrites it so a failure prints both sides in detail, so
there is no `expect(...).toBe(...)` vocabulary to learn. Files sit in tests/ and
are found by name (`test_*.py`).
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.domain import Link, generate_code

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PAST = NOW - timedelta(hours=1)


def make_link(**overrides) -> Link:
    # JS/TS vs Python: `**overrides` collects extra keyword arguments into a dict,
    # like a rest parameter for named args; `{**defaults, **overrides}` is object
    # spread. It lets each test change just the field it cares about.
    fields = dict(
        id="1",
        short_code="abc1234",
        target_url="https://example.com",
        title=None,
        created_at=NOW,
        expires_at=None,
        max_clicks=None,
        click_count=0,
        is_active=True,
    )
    return Link(**{**fields, **overrides})


# JS/TS vs Python: `parametrize` is Jest's test.each: one test function, many
# cases, each reported separately by its `id`.
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({}, "active", id="active by default"),
        pytest.param(
            {"is_active": False, "expires_at": PAST}, "disabled", id="disabled beats expired"
        ),
        pytest.param({"expires_at": PAST}, "expired", id="expired"),
        pytest.param({"max_clicks": 3, "click_count": 2}, "active", id="under the limit"),
        pytest.param({"max_clicks": 3, "click_count": 3}, "max_clicks", id="at the limit"),
        pytest.param(
            {"expires_at": PAST, "max_clicks": 1, "click_count": 1},
            "expired",
            id="expired beats max_clicks",
        ),
    ],
)
def test_status(overrides, expected):
    assert make_link(**overrides).status(NOW) == expected


def test_generated_codes_are_seven_base62_characters_and_random():
    codes = {generate_code() for _ in range(200)}  # a SET comprehension: dedupes
    assert all(re.fullmatch(r"[0-9A-Za-z]{7}", code) for code in codes)
    # 62^7 possibilities: a collision within 200 draws would mean a broken RNG.
    assert len(codes) == 200
