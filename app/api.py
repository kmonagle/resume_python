"""Why this file exists: the HTTP layer, and nothing else. It authenticates the
caller, maps service results onto the status codes docs/openapi.yaml promises,
and shapes error responses. Business rules live in service; SQL lives in store.

JS/TS vs Python: this is the counterpart of the route handlers under src/app/api
in the Next.js repo (and of Express/Fastify). FastAPI's distinctive idea is that
you DECLARE what a route needs through its parameter types, and the framework
supplies it: a `body: CreateLink` parameter means "parse and validate the JSON
body into this model", and `Depends(...)` means "run this function first and
hand me the result". There is no `req.body` to unpack and no manual parsing.
"""

import hashlib
import hmac
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas import CreateLink, LinkDTO, SetActive, link_to_dto
from app.service import (
    CodeTaken,
    Created,
    Followed,
    Gone,
    LimitReached,
    NotFound,
    Service,
)
from app.store import SqlAlchemyStore

# JS/TS vs Python: the `logging` module replaces `console.log`. A logger is named after the
# module (`__name__`, the module's dotted name: a built-in variable), so log lines say where
# they came from, and levels (`.info`, `.error`, `.exception`) can be filtered by uvicorn's
# configuration. `logger.exception(...)` also prints the traceback of the exception being
# handled.
logger = logging.getLogger(__name__)

IMPLEMENTATION = "Python FastAPI + SQLAlchemy"
CONTRACT_VERSION = "1"

# Live, per-owner data must never be cached by browsers, proxies or the BFF.
NO_STORE = {"Cache-Control": "no-store"}
_OWNER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# JS/TS vs Python: an APIRouter is Express's `Router()`: a bag of routes that the
# app mounts. Routes are registered with DECORATORS (`@router.get(...)`): the
# decorator runs once at import time and records the function against a method
# and path. In Express you'd call `router.get(path, handler)` instead.
router = APIRouter()


# JS/TS vs Python: a `yield` dependency is a CONTEXT MANAGER in function form: the
# code before `yield` is setup, the value after it is what the route receives, and
# the code after it is teardown (like try/finally). Here, one database session
# (and so one transaction) per request:
#   - `session.begin()` COMMITS when the block exits normally, and ROLLS BACK if
#     the route raised, so a failed request never leaves half its changes behind;
#   - the session is closed either way.
# Every query in the request, including the cleanup + count + insert that make up
# "create a link", therefore runs in the same transaction.
async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session, session.begin():
        yield session


# GOTCHA: `scope="function"` is essential. By default FastAPI runs a yield
# dependency's teardown (here, our COMMIT) AFTER the response has been sent. Two
# things go wrong:
#   1. Visibility: a client can receive its `201` and ask for the list before the
#      row is committed, and a click can redirect before it was counted.
#   2. DEADLOCK under load (verified: the 12-parallel-request contract test hangs
#      for 30 s without this). The click's row lock is held until the late commit,
#      so concurrent requests wait on it while each holds one of the 5 pooled
#      connections; then the click-logging background task (see log_click) needs a
#      connection to finish, none is free, and nothing can proceed.
# `scope="function"` ends the transaction right after the route function returns,
# BEFORE the response goes out, which fixes both.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


# JS/TS vs Python: dependencies can depend on other dependencies, and FastAPI
# resolves the chain (session -> store -> service) per request. This is FastAPI's
# dependency injection: no container and no decorators on the consumer, just
# function parameters.
def get_store(session: SessionDep) -> SqlAlchemyStore:
    return SqlAlchemyStore(session)


def get_service(store: Annotated[SqlAlchemyStore, Depends(get_store)]) -> Service:
    return Service(store)


# `Annotated[Type, Depends(fn)]` says "this parameter is a Type, and FastAPI obtains
# it by calling fn". Naming the combination once keeps route signatures short. In
# tests, `app.dependency_overrides[get_service] = ...` swaps the whole chain for a
# fake, with no monkeypatching.
ServiceDep = Annotated[Service, Depends(get_service)]


async def log_click(
    sessionmaker: async_sessionmaker[AsyncSession],
    link_id: str,
    referrer: str | None,
    user_agent: str | None,
) -> None:
    """Runs AFTER the response has been sent (see follow_link), so it can't use the
    request's session, which is already committed and closed by then. It opens its
    own. Failures are logged and swallowed: nobody is waiting on the result."""
    try:
        async with sessionmaker() as session, session.begin():
            await Service(SqlAlchemyStore(session)).record_click(link_id, referrer, user_agent)
    # JS/TS vs Python: `except Exception` is `catch (e)`. Catching the base class is
    # normally too broad, but a background task has nobody to report to, so it must not
    # crash the worker. (`except:` with no class would also swallow Ctrl+C: avoid it.)
    except Exception:
        logger.exception("recording click event failed")


# JS/TS vs Python: this is AUTH MIDDLEWARE done the FastAPI way, as a dependency
# instead of `app.use(...)`. Any route that lists it as a parameter is protected;
# the open routes simply don't. `async def` is deliberate: FastAPI runs a plain
# `def` dependency in a thread pool, and this one is too cheap to be worth the
# thread hop. The order of the checks matters: 401 (bad token) before 400 (bad
# owner), and FastAPI resolves dependencies BEFORE validating the body, so an
# unauthenticated caller never learns anything about the body's validity.
async def require_owner(request: Request) -> str:
    """Rejects requests without the shared bearer token (401) or without a usable
    X-Owner-Id (400); returns the owner id."""
    prefix = "Bearer "
    header = request.headers.get("authorization", "")
    if not header.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")

    # Compare SHA-256 hashes with hmac.compare_digest. Hashing makes both sides
    # always 32 bytes (so length leaks nothing), and compare_digest takes the same
    # time whatever the input, so response timing can't be used to guess the token
    # byte by byte, which a plain `==` would allow.
    # JS/TS vs Python: `header[len(prefix):]` is a SLICE, like `header.slice(n)`. The
    # syntax is `[start:stop]`, either end may be omitted, and negative numbers count
    # from the end (`s[-3:]` is the last three characters). `.encode()` turns the string
    # into bytes, which the hash function requires (strings and bytes are different types).
    presented = hashlib.sha256(header[len(prefix) :].encode()).digest()
    if not hmac.compare_digest(presented, request.app.state.token_hash):
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")

    owner = request.headers.get("x-owner-id", "")
    if not _OWNER_ID.fullmatch(owner):
        raise HTTPException(
            status_code=400,
            detail="X-Owner-Id header is required (1-64 chars: letters, numbers, - and _)",
        )
    return owner


OwnerDep = Annotated[str, Depends(require_owner)]


def error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=NO_STORE)


# ---- routes ----------------------------------------------------------------


# JS/TS vs Python: `response_model=LinkDTO` tells FastAPI the SHAPE of a
# successful response. It validates what we return, serialises it (using the
# camelCase aliases) and documents it in /docs. `status_code=201` is the success
# code. `response: Response` is a handle on the outgoing response, used here only
# to add a header: FastAPI merges it into the automatically built response.
@router.post("/links", status_code=201, response_model=LinkDTO)
async def create_link(body: CreateLink, owner: OwnerDep, service: ServiceDep, response: Response):
    # By the time this runs, `body` has ALREADY been parsed and validated: if the
    # JSON were bad, FastAPI would have answered 400 (see the handler below) and
    # this function would never have been called.
    response.headers["Cache-Control"] = "no-store"

    # JS/TS vs Python: `match` is STRUCTURAL PATTERN MATCHING (Python 3.10+), like
    # a switch over a discriminated union that also destructures. `Created(link=link)`
    # matches an instance of Created and binds its `link` attribute to a name.
    match await service.create(owner, body):
        case Created(link=link):
            return link_to_dto(link, datetime.now(UTC))
        case CodeTaken():
            return error_response(409, "That code is already taken")
        case LimitReached(message=message):
            return error_response(429, message)


@router.get("/links", response_model=list[LinkDTO])
async def list_links(owner: OwnerDep, service: ServiceDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now(UTC)
    return [link_to_dto(link, now) for link in await service.list_links(owner)]


@router.patch("/links/{link_id}", response_model=LinkDTO)
async def set_link_active(
    link_id: str,
    body: SetActive,
    owner: OwnerDep,
    service: ServiceDep,
    response: Response,
):
    # {link_id} in the path becomes the `link_id: str` parameter automatically.
    link = await service.set_active(owner, link_id, body.is_active)
    # 404, not 403, for someone else's link: do not confirm it exists.
    if link is None:
        return error_response(404, "Link not found")
    response.headers["Cache-Control"] = "no-store"
    return link_to_dto(link, datetime.now(UTC))


@router.get("/r/{code}")
async def follow_link(
    code: str, request: Request, background: BackgroundTasks, service: ServiceDep
):
    match await service.follow(code):
        case Followed(link_id=link_id, target_url=target):
            # Log the click AFTER responding so analytics never slows the redirect.
            #
            # JS/TS vs Python: BackgroundTasks is the counterpart of Next's
            # `after()` and Go's goroutine. Tasks added here run once the response
            # has been sent. GOTCHA: this only works if FastAPI builds the response
            # for you. Because we return our OWN Response object, the tasks would
            # be silently dropped unless we hand them over with `background=`.
            background.add_task(
                log_click,
                request.app.state.sessionmaker,
                link_id,
                request.headers.get("referer"),
                request.headers.get("user-agent"),
            )
            return Response(
                status_code=307,
                # JS/TS vs Python: `**NO_STORE` UNPACKS a dict into another, exactly like
                # object spread `{ Location: target, ...NO_STORE }` in JS.
                headers={"Location": target, **NO_STORE},  # never cache: every hit must claim
                background=background,
            )
        case NotFound():
            return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
        case Gone(message=message):
            # 410 Gone (not 404): the link existed but is no longer available.
            return PlainTextResponse(message, status_code=410, headers=NO_STORE)


@router.get("/meta")
async def meta():
    # Changes only on redeploy, so briefly cacheable (unlike live link data).
    return JSONResponse(
        {"implementation": IMPLEMENTATION, "contractVersion": CONTRACT_VERSION},
        headers={"Cache-Control": "public, max-age=60"},
    )


# ---- error shapes ----------------------------------------------------------
# FastAPI's defaults don't match the contract: it answers validation problems
# with 422 and {"detail": [...]}, and HTTP errors with {"detail": "..."}. These
# handlers translate to the contract's shapes: 400 + {"error", "fieldErrors"},
# and {"error": "..."}.


def _field_errors(exc: RequestValidationError) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for err in exc.errors():
        # `loc` is where the problem was found, e.g. ("body", "targetUrl") for a
        # bad field or ("body", 0) for unparseable JSON (a position, not a name).
        loc = err["loc"]
        field = loc[1] if len(loc) > 1 and isinstance(loc[1], str) else "body"
        if err["type"] == "missing":
            message = "Required"
        else:
            # Validators raise ValueError("..."); pydantic prefixes "Value error, ".
            message = err["msg"].removeprefix("Value error, ")
        # setdefault returns the existing list or inserts and returns a new one.
        fields.setdefault(field, []).append(message)
    return fields


async def handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # narrows the type for checkers
    return JSONResponse(
        {"error": "Validation failed", "fieldErrors": _field_errors(exc)},
        status_code=400,
        headers=NO_STORE,
    )


async def handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    return error_response(exc.status_code, str(exc.detail))


async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    # The real cause is in the server log (uvicorn logs the traceback); the
    # client is told nothing internal.
    return error_response(500, "Internal error")


def install_error_handlers(app: Any) -> None:
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(HTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected)
