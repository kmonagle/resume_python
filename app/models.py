"""Why this file exists: SQLAlchemy's description of the two tables this service
reads and writes. They are a MIRROR of the schema, not its source: the tables are
defined and migrated by Drizzle in the Next.js repo, and this service never
creates or alters them. If the schema changes there, these classes must follow.

JS/TS vs Python: this is the counterpart of src/server/db/schema.ts (Drizzle) in
the Next.js repo. Same idea, a typed description of the tables, in Python's style:
a class per table whose annotated attributes ARE the columns.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Every table class inherits from this; it collects them into one registry."""


def _new_id() -> str:
    return str(uuid.uuid4())


# JS/TS vs Python: `Mapped[str]` is a column of type text that is NOT NULL, and
# `Mapped[str | None]` is nullable. The Python type hint is what decides nullability
# (compare a Drizzle column's .notNull()). `mapped_column(...)` adds the details.
class LinkRow(Base):
    __tablename__ = "links"

    # A Python-side default, like Drizzle's $defaultFn: the id is generated in
    # application code, and the column has no database default.
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_new_id)
    short_code: Mapped[str] = mapped_column(Text, unique=True)
    target_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    # Anonymous visitor that created the link. Every read and toggle filters on it.
    owner_id: Mapped[str] = mapped_column(Text)
    # server_default means "the DATABASE fills this in": we omit it on insert and
    # read it back from the RETURNING clause.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # onupdate is SQLAlchemy's twin of Drizzle's $onUpdate: it adds
    # `updated_at = now()` to every UPDATE we issue through SQLAlchemy.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_clicks: Mapped[int | None] = mapped_column(Integer)
    click_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class ClickEventRow(Base):
    __tablename__ = "click_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_new_id)
    # The real foreign key (with ON DELETE CASCADE) exists in Postgres; declaring it
    # here lets SQLAlchemy order inserts correctly.
    link_id: Mapped[str] = mapped_column(Text, ForeignKey("links.id", ondelete="CASCADE"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    referrer: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
