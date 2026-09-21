"""Why this file exists: everything about CONNECTING to Postgres: turning the
DATABASE_URL we are given into what SQLAlchemy's async engine needs, and building
the engine and session factory. Queries live in store.py; this is only plumbing.

JS/TS vs Python: this is what a Node app would do with a Drizzle or
postgres.js client setup (the Next.js app holds no data, so it has none). Same jobs,
same Neon quirks, different library.
"""

import uuid
from typing import Any

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def engine_settings(database_url: str) -> tuple[URL, dict[str, Any]]:
    """Adapts a standard Postgres URL (as Neon and Render hand them out) for
    SQLAlchemy + asyncpg. Returns (url, connect_args).

    Three adaptations, all of which bite when you point SQLAlchemy at Neon:
    """
    url = make_url(database_url)
    query = dict(url.query)

    # 1. SQLAlchemy needs the driver named in the URL scheme. Neon gives
    #    "postgres://...", SQLAlchemy wants "postgresql+asyncpg://...".
    url = url.set(drivername="postgresql+asyncpg")

    # 2. asyncpg does not understand libpq's URL parameters. `sslmode` becomes
    #    asyncpg's own `ssl` argument, and `channel_binding` (which Neon's copy
    #    button adds) is dropped: passed through, asyncpg would either reject it
    #    or forward it to the server as an unknown setting.
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    url = url.set(query=query)

    connect_args: dict[str, Any] = {
        # 3. PgBouncer. Neon's pooled URL goes through PgBouncer in transaction
        #    mode, which cannot keep the prepared statements asyncpg and SQLAlchemy
        #    cache by default (the Python twin of the Next.js app's `prepare: false`).
        #    Turn both caches off and give every statement a unique name, per the
        #    SQLAlchemy docs' PgBouncer recipe.
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    }
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = sslmode
    return url, connect_args


def create_engine(database_url: str) -> AsyncEngine:
    url, connect_args = engine_settings(database_url)
    # JS/TS vs Python: creating the engine does NOT connect. It is a lazy pool that
    # opens connections on first use, which is what lets the tests build an app
    # with a URL that points nowhere.
    return create_async_engine(
        url,
        connect_args=connect_args,
        # Small pool: several services share one database.
        pool_size=5,
        max_overflow=0,
        # Neon suspends idle compute and drops connections; pre-ping tests a
        # pooled connection before handing it out, so a stale one is replaced
        # instead of failing a request.
        pool_pre_ping=True,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # JS/TS vs Python: a sessionmaker is a factory for sessions. A SESSION is
    # SQLAlchemy's unit of work: it holds one connection and one transaction, and
    # collects changes until you commit. `expire_on_commit=False` keeps loaded
    # objects readable after commit; with async, touching an expired attribute
    # would try to lazy-load and fail outside an awaitable context.
    return async_sessionmaker(engine, expire_on_commit=False)
