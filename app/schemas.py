"""Why this file exists: the request and response shapes, and the validation
rules for user input (the trust boundary). The rules and messages match
docs/openapi.yaml and the other backends, because the contract
tests hold every backend to the same behaviour.

JS/TS vs Python: this is the counterpart of src/shared/schemas/link-schema.ts
(zod) in the Next.js repo, and of internal/link/validate.go in the Go repo. The
Python ecosystem's answer is PYDANTIC: you describe a shape as a class with type
hints, and pydantic validates incoming data against it AT RUNTIME. FastAPI is
built on it: a route parameter typed as one of these classes is automatically
parsed from the request JSON, and a bad body never reaches your function.
"""

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator
from pydantic.alias_generators import to_camel

from app.domain import Link, Status


# JS/TS vs Python: Python style is snake_case (target_url), but the contract's
# JSON is camelCase (targetUrl). `alias_generator=to_camel` derives each JSON
# name from the field name, so the class stays idiomatic Python and the wire
# format stays what the contract says. `populate_by_name` also lets our own code
# (and tests) build these with snake_case keywords.
class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _blank(value: Any) -> bool:
    """HTML forms send "" for untouched fields; treat "" and whitespace as absent."""
    # JS/TS vs Python: `isinstance(x, str)` is the runtime type check (`typeof x
    # === "string"`). Python truthiness differs from JS: "" , 0, [] and {} are
    # falsy, but so is None, and there is no `undefined`.
    return value is None or (isinstance(value, str) and value.strip() == "")


# JS/TS vs Python: the `re` module has THREE ways to apply a pattern, and mixing them up
# is a common bug. `re.match` anchors only at the START, `re.search` finds it anywhere, and
# `re.fullmatch` requires the WHOLE string to match. JS's `^...$` habit is replaced by
# fullmatch here, which is why these patterns have no anchors. (A raw string, `r"..."`,
# stops backslashes being treated as escapes, so `\d` reaches the regex engine intact.)
_SHORT_CODE = re.compile(r"[A-Za-z0-9_-]{3,32}")
# Requires an explicit UTC offset (or Z), so the moment is unambiguous.
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?([Zz]|[+-]\d{2}:\d{2})")


class CreateLink(CamelModel):
    """The body of POST /links, after validation.

    JS/TS vs Python: the field types below are the schema. `target_url: str` with
    no default means REQUIRED; `title: str | None = None` means optional. There
    is no separate "raw" vs "validated" type as in the Go version: pydantic
    hands you this object only if everything passed.
    """

    target_url: str
    title: str | None = None
    expires_at: datetime | None = None
    max_clicks: int | None = None
    short_code: str | None = None

    # JS/TS vs Python: DECORATORS. `@field_validator("title", mode="before")` is a
    # function that wraps another function at definition time (like a TS
    # decorator, or a higher-order function applied by syntax). Here it registers
    # the method as a validator for one field. "before" means it sees the RAW
    # input, so it can clean it up first (trim, blank -> None) and reject
    # anything odd with a message of our choosing.
    # `@classmethod` gives the method the class instead of an instance (`cls`).
    @field_validator("target_url", mode="before")
    @classmethod
    def _check_target_url(cls, value: Any) -> str:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError("Enter a valid http(s) URL")
        # Only http(s): a shortener that redirects to `javascript:` or `data:`
        # URLs is an XSS gadget. (urlparse is lenient and never raises for
        # "nope", so we check the scheme and host ourselves.)
        parts = urlparse(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("Enter a valid http(s) URL")
        return value

    @field_validator("title", mode="before")
    @classmethod
    def _check_title(cls, value: Any) -> str | None:
        if _blank(value):
            return None
        if not isinstance(value, str):
            raise ValueError("Enter text")
        title = value.strip()
        # JS/TS vs Python: len() counts Unicode CODE POINTS (Python strings are
        # sequences of characters), unlike JS's UTF-16 `.length` and Go's byte
        # length.
        if len(title) > 100:
            raise ValueError("Too long")
        return title

    @field_validator("expires_at", mode="before")
    @classmethod
    def _check_expires_at(cls, value: Any) -> datetime | None:
        if _blank(value):
            return None
        text = value.strip() if isinstance(value, str) else ""
        if not _RFC3339.fullmatch(text):
            raise ValueError("Enter a valid date and time")
        try:
            # JS/TS vs Python: datetime is the equivalent of Date, but timezone
            # handling is explicit: this one is AWARE (it carries the offset).
            parsed = datetime.fromisoformat(text)
        except ValueError:
            # JS/TS vs Python: raising inside `except` normally CHAINS the original
            # exception (its traceback is shown too). `from None` drops it, since the
            # user-facing message is all we want here.
            raise ValueError("Enter a valid date and time") from None
        if parsed <= datetime.now(UTC):
            raise ValueError("Expiry must be in the future")
        return parsed

    @field_validator("max_clicks", mode="before")
    @classmethod
    def _check_max_clicks(cls, value: Any) -> int | None:
        if _blank(value):
            return None
        # JS/TS vs Python: a Python bool IS an int (True == 1), so a plain
        # isinstance(value, int) check would accept `true`. Rule it out first.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Enter a whole number")
        if value < 1:
            raise ValueError("Must be at least 1")
        if value > 1_000_000:  # underscores as digit separators, same as JS
            raise ValueError("Must be at most 1,000,000")
        return value

    @field_validator("short_code", mode="before")
    @classmethod
    def _check_short_code(cls, value: Any) -> str | None:
        if _blank(value):
            return None
        code = value.strip() if isinstance(value, str) else ""
        if not _SHORT_CODE.fullmatch(code):
            raise ValueError("3-32 characters: letters, numbers, - and _")
        return code


class SetActive(CamelModel):
    """The body of PATCH /links/{id}."""

    # JS/TS vs Python: pydantic is LENIENT by default and would coerce the string
    # "yes" (or 1, or "true") into True. StrictBool accepts only a real JSON
    # boolean, which is what the contract requires (anything else is a 400).
    is_active: StrictBool


class LinkDTO(CamelModel):
    """The wire format for a link (the contract's `Link` schema).

    JS/TS vs Python: it is a separate class from the domain `Link` on purpose,
    the same split as toLinkDto() in the Next.js repo: the domain type holds real
    datetimes, the wire type holds ISO strings. Declaring it as a pydantic model
    also lets FastAPI document the response and serialise it for us.
    """

    id: str
    short_code: str
    target_url: str
    title: str | None
    created_at: str
    expires_at: str | None
    max_clicks: int | None
    click_count: int
    is_active: bool
    status: Status


def _iso(moment: datetime) -> str:
    """Millisecond precision, UTC, "Z" suffix: identical to JavaScript's
    Date.toISOString(), so every backend serialises timestamps the same way."""
    utc = moment.astimezone(UTC)
    # JS/TS vs Python: f-string format spec `:03d` pads an int to 3 digits with
    # zeros. // is INTEGER division (`/` always gives a float in Python).
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def link_to_dto(link: Link, now: datetime) -> LinkDTO:
    return LinkDTO(
        id=link.id,
        short_code=link.short_code,
        target_url=link.target_url,
        title=link.title,
        created_at=_iso(link.created_at),
        expires_at=_iso(link.expires_at) if link.expires_at else None,
        max_clicks=link.max_clicks,
        click_count=link.click_count,
        is_active=link.is_active,
        status=link.status(now),
    )
