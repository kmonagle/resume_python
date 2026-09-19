"""Why this file exists: tests the business rules without a database, using a
hand-written fake of the Store protocol. Because Store is a small structural
interface, the fake is short and no mocking library is needed.

JS/TS vs Python: in Jest you'd `jest.mock` a module. Python's usual move is the
same as in Go: depend on a small interface and hand in a fake. FakeStore never
declares that it satisfies Store; having the methods is enough.
"""

from datetime import UTC, datetime

from app.schemas import CreateLink
from app.service import (
    MAX_LINKS_PER_OWNER,
    MAX_LINKS_TOTAL,
    CodeTaken,
    Created,
    Followed,
    Gone,
    LimitReached,
    NotFound,
    Service,
)
from tests.test_domain import make_link

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeStore:
    """Only what each test needs; anything left at its default means 'nothing special'."""

    def __init__(self, per_owner=0, total=0, taken=(), found=None, claim=None):
        self.per_owner, self.total, self.taken = per_owner, total, set(taken)
        self.found, self._claim = found, claim

    async def insert(self, owner_id, code, data):
        return None if code in self.taken else make_link(short_code=code)

    async def list_by_owner(self, owner_id):
        return []

    async def count_by_owner(self, owner_id):
        return self.per_owner

    async def count_all(self):
        return self.total

    async def delete_created_before(self, cutoff):
        return None

    async def set_active(self, owner_id, link_id, active):
        return None

    async def claim(self, code):
        return self._claim

    async def find_by_code(self, code):
        return self.found

    async def insert_click_event(self, link_id, referrer, user_agent):
        return None


def service(**kwargs) -> Service:
    return Service(FakeStore(**kwargs), clock=lambda: NOW)


DATA = CreateLink(target_url="https://a.co")


# JS/TS vs Python: with pytest-asyncio in "auto" mode (see pyproject.toml), a
# plain `async def test_...` just works: pytest awaits it on an event loop.
async def test_create_enforces_limits():
    assert isinstance(await service(per_owner=MAX_LINKS_PER_OWNER).create("o", DATA), LimitReached)
    assert isinstance(await service(total=MAX_LINKS_TOTAL).create("o", DATA), LimitReached)
    assert isinstance(await service().create("o", DATA), Created)


async def test_custom_code_that_is_taken():
    data = CreateLink(target_url="https://a.co", short_code="promo")
    assert isinstance(await service(taken={"promo"}).create("o", data), CodeTaken)


async def test_follow_claimed_link_redirects():
    result = await service(claim=("id-1", "https://target")).follow("x")
    assert result == Followed("id-1", "https://target")


async def test_follow_unknown_code_is_not_found():
    assert isinstance(await service().follow("x"), NotFound)


async def test_follow_reports_why_a_link_is_gone():
    limited = make_link(max_clicks=1, click_count=1)
    result = await service(found=limited).follow("x")
    assert result == Gone("This link has reached its click limit.")


async def test_follow_never_redirects_when_the_claim_failed_but_the_row_looks_active():
    # The link changed between the two statements: report unavailable, don't guess.
    result = await service(found=make_link()).follow("x")
    assert isinstance(result, Gone)
