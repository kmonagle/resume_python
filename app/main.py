"""Why this file exists: builds the FastAPI application: reads settings, creates the
database engine, and mounts the routes. Anything more interesting belongs in the
other modules.

JS/TS vs Python: there is no `app.listen()` here. In Python the web server is a
separate program: UVICORN (an ASGI server, the Python analogue of Node's http
module plus a process runner) is started from the command line and told where to
find the app:  `uvicorn app.main:create_app --factory`. It handles signals too: on
SIGTERM (how Render stops a service) it finishes in-flight requests and then runs
the shutdown half of `lifespan` below.
"""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import install_error_handlers, router
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker


# JS/TS vs Python: a FACTORY function builds the app instead of creating a global
# `app` at import time. That keeps importing this module free of side effects (no
# env reads, no connections), which is what lets tests build an app without a
# database. Uvicorn's `--factory` flag calls it for us.
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Lazy: creating the engine does not connect, so this is safe in tests.
    engine = create_engine(settings.database_url)

    # JS/TS vs Python: a CONTEXT MANAGER is code with a setup half and a teardown
    # half around a block, like try/finally with the two halves in one place. The
    # decorator turns this generator function into one: everything before `yield`
    # runs at startup, everything after runs at shutdown. FastAPI calls it as the
    # app's `lifespan`.
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield  # the app serves requests while we are suspended here
        finally:
            await engine.dispose()  # close every pooled connection on the way out

    app = FastAPI(
        title="Short link backend (Python)",
        version="1",
        summary="An implementation of the contract in the resume_nextjs repo.",
        lifespan=lifespan,
    )
    # app.state is the app-wide scratchpad that routes and dependencies read.
    app.state.sessionmaker = create_sessionmaker(engine)
    # Only the HASH of the token is kept in memory (see api.require_owner).
    app.state.token_hash = hashlib.sha256(settings.link_backend_token.encode()).digest()

    app.include_router(router)
    install_error_handlers(app)
    return app
