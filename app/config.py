"""Why this file exists: one place that reads and validates environment variables,
so a missing or malformed setting stops the process at startup with a clear message
instead of failing on some later request.

JS/TS vs Python: this is the counterpart of src/server/env.ts in the Next.js repo,
which uses zod. pydantic-settings is the standard Python answer: declare the
variables as a typed class, and it reads them from the environment and validates
them with pydantic. It is the same library that validates request bodies, so the
whole service has one validation vocabulary.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


# JS/TS vs Python: a BaseSettings subclass fills its fields from environment
# variables by name, case-insensitively: `database_url` is read from DATABASE_URL.
# A missing or invalid value raises a ValidationError that names every problem.
class Settings(BaseSettings):
    database_url: str
    # The shared secret the Next.js BFF sends as a bearer token (env var
    # LINK_BACKEND_TOKEN). Render's free tier has no private networking, so this
    # service is publicly reachable; without the token anyone could call it with
    # any X-Owner-Id.
    link_backend_token: str = Field(min_length=16)


# JS/TS vs Python: `lru_cache` memoises a function, so the environment is read
# and validated ONCE and every caller shares the result: a lazy singleton without a
# global variable. (Settings() with no arguments looks like a missing-argument
# error to a type checker; the values come from the environment at runtime.)
@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
