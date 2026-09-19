"""Why this file exists: the DATABASE_URL Neon hands out does not work with
SQLAlchemy + asyncpg as-is. These tests pin down the three adaptations that
engine_settings() makes, so a change can't quietly break production.
"""

from app.db import engine_settings

# JS/TS vs Python: a test module is just functions named `test_*`; pytest imports the file and
# runs them. There is no `describe` block and no setup boilerplate, and a test passes unless
# an `assert` fails or an exception escapes.
NEON = "postgres://user:pw@ep-x-pooler.neon.tech/db?sslmode=require&channel_binding=require"


def test_driver_is_switched_to_asyncpg():
    url, _ = engine_settings("postgres://u:p@host/db")
    assert url.drivername == "postgresql+asyncpg"
    assert (url.host, url.database, url.username) == ("host", "db", "u")


def test_libpq_parameters_are_translated_for_asyncpg():
    url, args = engine_settings(NEON)
    # sslmode/channel_binding must not remain in the URL: asyncpg would choke on them.
    assert dict(url.query) == {}
    assert args["ssl"] == "require"


def test_no_ssl_argument_without_sslmode_or_when_disabled():
    assert "ssl" not in engine_settings("postgres://u:p@host/db")[1]
    assert "ssl" not in engine_settings("postgres://u:p@host/db?sslmode=disable")[1]


def test_prepared_statements_are_disabled_for_pgbouncer():
    _, args = engine_settings(NEON)
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    # Every statement gets a unique name, so PgBouncer never sees a name collision.
    # JS/TS vs Python: functions are first-class objects stored in a dict, and calling one
    # is `fn()`. Two calls returning different values proves each name is unique.
    assert args["prepared_statement_name_func"]() != args["prepared_statement_name_func"]()
