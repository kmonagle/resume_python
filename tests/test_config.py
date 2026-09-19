"""Why this file exists: environment mistakes should fail fast and clearly, and the
token's minimum length is a security rule worth pinning down.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


# JS/TS vs Python: `monkeypatch` is a pytest FIXTURE: a test asks for it by naming
# it as a parameter, and pytest supplies it (dependency injection for tests, much
# like FastAPI's Depends). It edits the environment for this test only and undoes
# the change afterwards. `_env_file=None` stops the settings class from also reading
# a local .env file, so the test sees only what we set.
def load(monkeypatch, **env: str) -> Settings:
    for name in ("DATABASE_URL", "LINK_BACKEND_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_reads_the_environment(monkeypatch):
    settings = load(
        monkeypatch, DATABASE_URL="postgres://x/db", LINK_BACKEND_TOKEN="0123456789abcdef"
    )
    assert settings.database_url == "postgres://x/db"
    assert settings.link_backend_token == "0123456789abcdef"


def test_both_variables_are_required(monkeypatch):
    with pytest.raises(ValidationError) as caught:
        load(monkeypatch)
    missing = {e["loc"][0] for e in caught.value.errors()}
    assert missing == {"database_url", "link_backend_token"}


def test_token_must_be_at_least_sixteen_characters(monkeypatch):
    with pytest.raises(ValidationError):
        load(monkeypatch, DATABASE_URL="postgres://x/db", LINK_BACKEND_TOKEN="too-short")
