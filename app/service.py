"""Why this file exists: the business rules, between HTTP (api) and SQL (store):
demo size limits, retention cleanup, short-code generation with retry, and how a
redirect decides 404 vs 410. It is the Python counterpart of the Next.js app's
local adapter (src/server/link-api/local.ts) and of internal/service in the Go
repo, and the contract tests hold all three to the same behaviour.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.domain import Link, generate_code
from app.schemas import CreateLink

# Guard rails for a public demo; the same numbers as the other implementations.
MAX_LINKS_PER_OWNER = 20
MAX_LINKS_TOTAL = 5000
RETENTION_DAYS = 30
MAX_CODE_ATTEMPTS = 5

GONE_MESSAGES = {
    "expired": "This link has expired.",
    "max_clicks": "This link has reached its click limit.",
    "disabled": "This link has been deactivated.",
}


# JS/TS vs Python: a PROTOCOL is a structural interface, much like a TS
# `interface` or a Go interface: PostgresStore never says it implements Store;
# having these methods is enough. The difference from Go is WHEN it's checked:
# Python doesn't check at runtime at all. A type checker (mypy/pyright) would
# complain at development time, and the tests pass a hand-written fake.
class Store(Protocol):
    async def insert(self, owner_id: str, code: str, data: CreateLink) -> Link | None: ...
    async def list_by_owner(self, owner_id: str) -> list[Link]: ...
    async def count_by_owner(self, owner_id: str) -> int: ...
    async def count_all(self) -> int: ...
    async def delete_created_before(self, cutoff: datetime) -> None: ...
    async def set_active(self, owner_id: str, link_id: str, active: bool) -> Link | None: ...
    async def claim(self, code: str) -> tuple[str, str] | None: ...
    async def find_by_code(self, code: str) -> Link | None: ...
    async def insert_click_event(
        self, link_id: str, referrer: str | None, user_agent: str | None
    ) -> None: ...


# JS/TS vs Python: RESULT TYPES. Expected outcomes are values, not exceptions:
# the route decides what "code taken" means for the client. Each outcome is a
# tiny frozen dataclass, and a union of them is the Python version of a TS
# discriminated union: `Created | CodeTaken | LimitReached`. The route branches
# with `match`, and a type checker can verify it covered every case.
@dataclass(frozen=True)
class Created:
    link: Link


@dataclass(frozen=True)
class CodeTaken:
    pass


@dataclass(frozen=True)
class LimitReached:
    message: str


CreateResult = Created | CodeTaken | LimitReached


@dataclass(frozen=True)
class Followed:
    link_id: str
    target_url: str


@dataclass(frozen=True)
class NotFound:
    pass


@dataclass(frozen=True)
class Gone:
    # The human-readable reason: the contract's 410 body is plain text.
    message: str


FollowResult = Followed | NotFound | Gone


class Service:
    def __init__(
        self,
        store: Store,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        # JS/TS vs Python: functions are first-class values, as in JS, and
        # `lambda: ...` is an arrow function limited to ONE expression (anything
        # longer needs a `def`). Injecting the clock lets a test freeze time.
        self._clock = clock

    async def create(self, owner_id: str, data: CreateLink) -> CreateResult:
        # Lazy cleanup instead of a cron job: whoever creates a link also sweeps
        # expired demo data.
        cutoff = self._clock() - timedelta(days=RETENTION_DAYS)
        await self._store.delete_created_before(cutoff)

        # Soft limits: count-then-insert can be overshot by a burst of
        # concurrent requests. Fine for abuse control, unlike max_clicks, which
        # is a correctness guarantee and is enforced atomically in SQL.
        if await self._store.count_by_owner(owner_id) >= MAX_LINKS_PER_OWNER:
            return LimitReached(f"Demo limit: {MAX_LINKS_PER_OWNER} links per visitor.")
        if await self._store.count_all() >= MAX_LINKS_TOTAL:
            return LimitReached("Demo limit: the service is full right now.")

        # A custom code either works or is "taken"; retrying would not help.
        if data.short_code is not None:
            link = await self._store.insert(owner_id, data.short_code, data)
            return Created(link) if link else CodeTaken()

        # A generated code that collides is just bad luck: try a fresh one.
        for _ in range(MAX_CODE_ATTEMPTS):
            link = await self._store.insert(owner_id, generate_code(), data)
            if link is not None:
                return Created(link)
        # JS/TS vs Python: an unexpected failure IS raised, and propagates up to
        # the app's catch-all handler, which turns it into a 500.
        raise RuntimeError("could not generate a unique short code")

    # (Named list_links, not `list`: a method called `list` would SHADOW Python's
    # builtin `list` for any later annotation in this class, a classic trap.)
    async def list_links(self, owner_id: str) -> list[Link]:
        return await self._store.list_by_owner(owner_id)

    async def set_active(self, owner_id: str, link_id: str, active: bool) -> Link | None:
        """Returns None when the link does not exist for this owner."""
        return await self._store.set_active(owner_id, link_id, active)

    async def follow(self, code: str) -> FollowResult:
        claimed = await self._store.claim(code)
        if claimed is not None:
            # JS/TS vs Python: tuple UNPACKING, like `const [id, url] = claimed`.
            link_id, target_url = claimed
            return Followed(link_id, target_url)

        # Nothing was claimed: no such link (404) or it exists but is not
        # redeemable (410). This lookup may be non-atomic because it only
        # chooses the error message; the decision was made atomically above.
        link = await self._store.find_by_code(code)
        if link is None:
            return NotFound()

        status = link.status(self._clock())
        if status == "active":
            # The link changed between the two statements (for example it was
            # re-enabled). Report it as unavailable rather than guess.
            status = "disabled"
        return Gone(GONE_MESSAGES[status])

    async def record_click(
        self, link_id: str, referrer: str | None, user_agent: str | None
    ) -> None:
        """Logs the analytics row. Separate from follow() so the route can run it
        after the redirect has been sent."""
        await self._store.insert_click_event(link_id, referrer, user_agent)
