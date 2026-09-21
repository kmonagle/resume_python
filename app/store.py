"""Why this file exists: the ONLY module that runs queries. Everything above it
(service, api) works with plain domain objects, so the queries can be read, and
audited against the shared schema, in one place. The schema is owned by the
Next.js repo's migrations; this service never migrates.

JS/TS vs Python: this is the only code that runs queries (the Next.js
app holds no data, so there is no counterpart there; compare a typical Drizzle or
Prisma data layer in a Node app). SQLAlchemy 2.0 is Python's standard database toolkit: it is both an
ORM (mapped classes, sessions) and a SQL expression language (`select(...)`,
`update(...)`). The statements below are built from Python objects, not SQL
strings, so a typo in a column name is an AttributeError at the point of writing.
The one query that matters most, `claim`, is still written as a single explicit
UPDATE: see its docstring.
"""

from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import Link
from app.models import ClickEventRow, LinkRow
from app.schemas import CreateLink


def _to_link(row: LinkRow) -> Link:
    """Map the database row to the domain object, so nothing above the store ever
    sees an ORM instance (and never triggers lazy loading by accident)."""
    return Link(
        id=row.id,
        short_code=row.short_code,
        target_url=row.target_url,
        title=row.title,
        created_at=row.created_at,
        expires_at=row.expires_at,
        max_clicks=row.max_clicks,
        click_count=row.click_count,
        is_active=row.is_active,
    )


class SqlAlchemyStore:
    """All queries, running inside the ONE session (and so the one transaction) it
    is given. The session is created per request; see api.get_session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # JS/TS vs Python: `await session.execute(stmt)` sends the statement; the
    # result is an iterable of rows, and helpers like `.scalar_one_or_none()`,
    # `.first()` and `.scalars().all()` pick out the shape you want.
    async def insert(self, owner_id: str, code: str, data: CreateLink) -> Link | None:
        """Returns the new link, or None when the short code is already taken.

        ON CONFLICT DO NOTHING lets Postgres arbitrate a race between two requests
        for the same code. SELECT-then-INSERT would leave a gap in which both pass
        the check and one then fails with a unique violation. (`pg_insert` is the
        PostgreSQL-specific insert construct; plain `insert` has no ON CONFLICT.)
        """
        stmt = (
            pg_insert(LinkRow)
            .values(
                short_code=code,
                target_url=data.target_url,
                title=data.title,
                owner_id=owner_id,
                expires_at=data.expires_at,
                max_clicks=data.max_clicks,
            )
            .on_conflict_do_nothing(index_elements=[LinkRow.short_code])
            .returning(LinkRow)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_link(row) if row is not None else None

    async def list_by_owner(self, owner_id: str) -> list[Link]:
        stmt = (
            select(LinkRow).where(LinkRow.owner_id == owner_id).order_by(LinkRow.created_at.desc())
        )
        return [_to_link(row) for row in (await self._session.scalars(stmt)).all()]

    async def count_by_owner(self, owner_id: str) -> int:
        stmt = select(func.count()).select_from(LinkRow).where(LinkRow.owner_id == owner_id)
        return (await self._session.execute(stmt)).scalar_one()

    async def count_all(self) -> int:
        stmt = select(func.count()).select_from(LinkRow)
        return (await self._session.execute(stmt)).scalar_one()

    async def delete_created_before(self, cutoff: datetime) -> None:
        # click_events rows go with their link via the foreign key's ON DELETE CASCADE.
        await self._session.execute(delete(LinkRow).where(LinkRow.created_at < cutoff))

    async def set_active(self, owner_id: str, link_id: str, active: bool) -> Link | None:
        """Scoped by owner: another owner's id matches zero rows, so the caller
        sees "not found" and can never toggle a link it does not own. (updated_at is
        bumped automatically by the model's `onupdate`.)"""
        stmt = (
            update(LinkRow)
            .where(LinkRow.id == link_id, LinkRow.owner_id == owner_id)
            .values(is_active=active)
            .returning(LinkRow)
            # We never keep ORM objects around, so skip SQLAlchemy's bookkeeping of
            # matching them against the change.
            .execution_options(synchronize_session=False)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_link(row) if row is not None else None

    async def claim(self, code: str) -> tuple[str, str] | None:
        """Checks every redeemability rule AND counts the click in ONE statement.

        This is deliberately an explicit UPDATE ... WHERE ... RETURNING and not
        the usual ORM sequence (load the object, check its fields in Python,
        modify it, commit). That sequence would let two concurrent requests both
        load "9 of 10", both pass the check, and both redirect (ending at 11).
        In a single UPDATE, Postgres locks the row: the second request waits, then
        re-evaluates the WHERE clause against the already-incremented row (10) and
        matches nothing. The database enforces the limit however many instances or
        languages are calling it. Must stay identical to the other
        backends.

        Returns (link id, target url), or None when the link is missing or no
        longer redeemable.
        """
        stmt = (
            update(LinkRow)
            .where(
                LinkRow.short_code == code,
                LinkRow.is_active.is_(True),
                or_(LinkRow.expires_at.is_(None), LinkRow.expires_at > func.now()),
                or_(LinkRow.max_clicks.is_(None), LinkRow.click_count < LinkRow.max_clicks),
            )
            # `LinkRow.click_count + 1` becomes SQL (`click_count = click_count + 1`),
            # evaluated by Postgres on the current row, NOT a Python addition.
            .values(click_count=LinkRow.click_count + 1)
            .returning(LinkRow.id, LinkRow.target_url)
            .execution_options(synchronize_session=False)
        )
        row = (await self._session.execute(stmt)).first()
        # JS/TS vs Python: a result row is a named tuple, so it unpacks like an
        # array (`id, url = row`) or reads by name (`row.id`).
        return (row.id, row.target_url) if row is not None else None

    async def find_by_code(self, code: str) -> Link | None:
        stmt = select(LinkRow).where(LinkRow.short_code == code)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_link(row) if row is not None else None

    async def insert_click_event(
        self, link_id: str, referrer: str | None, user_agent: str | None
    ) -> None:
        # JS/TS vs Python: session.add() is the ORM "unit of work" style: queue an
        # object now, and SQLAlchemy writes it when the transaction commits (or is
        # flushed). Compare with the explicit statements above.
        self._session.add(ClickEventRow(link_id=link_id, referrer=referrer, user_agent=user_agent))
