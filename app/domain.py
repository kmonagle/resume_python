"""Why this file exists: the domain model, free of HTTP and SQL. It holds the one
rule every implementation of the contract must agree on: "is this link usable,
and if not, why?". The SQL in store.claim() applies the same rules; keep them in
step. Also generates short codes.

JS/TS vs Python: dependencies flow one way (api -> service -> store -> domain), and
this module imports only the standard library. Keeping the domain free of I/O is
a convention, but the import list at the top makes it easy to check.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# JS/TS vs Python: `Literal[...]` is a string-literal union, exactly like the TS
# type "active" | "expired" | ... . Python's type hints are NOT enforced at
# runtime (`Status` is only a hint); a checker such as mypy or pyright reads
# them. FastAPI/pydantic are the exception: they READ the hints at runtime and
# use them to validate data, which is what makes them feel magical.
Status = Literal["active", "expired", "max_clicks", "disabled"]


# JS/TS vs Python: `slots=True` stores fields in a fixed layout, so a typo like
# `link.clik_count = 1` raises instead of silently creating a new attribute
# (Python objects are open bags of attributes by default, unlike a TS class).
@dataclass(frozen=True, slots=True)
class Link:
    id: str
    short_code: str
    target_url: str
    # JS/TS vs Python: `str | None` is `string | null`. Python has ONE null value,
    # `None` (no undefined), and it is a singleton compared with `is`, not `==`.
    title: str | None
    created_at: datetime
    expires_at: datetime | None
    max_clicks: int | None
    click_count: int
    is_active: bool

    # JS/TS vs Python: a method takes `self` explicitly as its first parameter
    # (JS's implicit `this`). `now` is passed in instead of calling
    # datetime.now() inside, which keeps the function deterministic and testable.
    def status(self, now: datetime) -> Status:
        """Mirrors the contract's precedence: disabled, then expired, then
        max_clicks, otherwise active."""
        if not self.is_active:
            return "disabled"
        # JS/TS vs Python: comparing datetimes with `<` just works. (Both sides
        # are timezone-AWARE, which matters: Python refuses to compare an aware
        # datetime with a naive one and raises TypeError.)
        if self.expires_at is not None and self.expires_at < now:
            return "expired"
        if self.max_clicks is not None and self.click_count >= self.max_clicks:
            return "max_clicks"
        return "active"


_CODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CODE_LENGTH = 7


def generate_code() -> str:
    """A random 7-character base62 code.

    JS/TS vs Python: `secrets` is the CSPRNG module (JS: crypto.getRandomValues);
    the `random` module is fast but predictable and must never be used for
    anything guessable. Picking the wrong import is the classic Python security
    mistake, so the import above is worth a glance.
    """
    # JS/TS vs Python: a GENERATOR EXPRESSION inside join(), like
    # Array.from({length}, () => ...).join(""). `_` is the conventional name for
    # a loop variable you don't use.
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
