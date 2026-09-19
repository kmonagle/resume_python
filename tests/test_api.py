"""Why this file exists: tests the parts of the HTTP layer that need no database
(authentication, the owner header, the open /meta endpoint, the error shapes) by
building the app with a fake service.

JS/TS vs Python: TestClient plays supertest's role: it calls the ASGI app
in-process (no server, no port), and returns real response objects.
"""

from fastapi.testclient import TestClient

from app.api import get_service
from app.config import Settings
from app.main import create_app
from app.service import Service
from tests.test_service import FakeStore

TOKEN = "test-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def client() -> TestClient:
    # The engine is lazy, so building the app needs no database. FastAPI's
    # `dependency_overrides` is the idiomatic way to swap a dependency in tests: it
    # replaces the whole session -> store -> service chain with a fake, with no
    # monkeypatching. (A TestClient used WITHOUT `with` also skips the startup
    # lifespan.)
    app = create_app(Settings(database_url="postgresql://unused/db", link_backend_token=TOKEN))
    app.dependency_overrides[get_service] = lambda: Service(FakeStore())
    return TestClient(app)


def test_api_routes_need_the_bearer_token():
    for method, path in [("GET", "/links"), ("POST", "/links"), ("PATCH", "/links/abc")]:
        response = client().request(method, path, json={})
        assert response.status_code == 401, (method, path)
        assert response.json() == {"error": "Missing or invalid bearer token"}


def test_wrong_scheme_or_token_is_rejected():
    for header in ["Bearer nope", f"Basic {TOKEN}", TOKEN]:
        response = client().get("/links", headers={"Authorization": header, "X-Owner-Id": "abc"})
        assert response.status_code == 401


def test_token_without_a_usable_owner_is_a_400():
    for owner in [None, "has space", "x" * 65]:
        headers = dict(AUTH, **({"X-Owner-Id": owner} if owner else {}))
        assert client().get("/links", headers=headers).status_code == 400


def test_authentication_is_checked_before_the_body_is_validated():
    # An unauthenticated caller must not learn whether its body was valid.
    assert client().post("/links", json={"targetUrl": "nope"}).status_code == 401


def test_validation_errors_use_the_contracts_shape():
    response = client().post(
        "/links", json={"targetUrl": "javascript:alert(1)"}, headers=AUTH | {"X-Owner-Id": "o"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Validation failed"
    assert body["fieldErrors"] == {"targetUrl": ["Enter a valid http(s) URL"]}


def test_unparseable_json_is_a_400_not_a_500():
    response = client().post(
        "/links",
        content="{not json",
        headers=AUTH | {"X-Owner-Id": "o", "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "body" in response.json()["fieldErrors"]


def test_meta_is_open_and_briefly_cacheable():
    response = client().get("/meta")
    assert response.status_code == 200
    assert response.json() == {
        "implementation": "Python FastAPI + SQLAlchemy",
        "contractVersion": "1",
    }
    assert response.headers["cache-control"] == "public, max-age=60"


def test_unknown_short_code_is_404_without_a_token():
    assert client().get("/r/nope").status_code == 404
