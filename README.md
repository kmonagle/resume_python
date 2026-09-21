# resume_python — the link backend, in Python (FastAPI)

A third implementation of the short-link API. The Next.js app in
[`resume_nextjs`](https://github.com/kmonagle/resume_nextjs) can serve links itself
out of its own code, and the [Go service](https://github.com/kmonagle/resume_go) does
the same job; this one does it in Python. All three are held to the **identical
contract test suite**, which is the point: the contract, not the language, defines
the system.

**Implements contract `contract-v1`** (the tag pinned in `.github/workflows/ci.yml`).

Read this README for how the services fit together and why the design is the way
it is. Read the code for the Python:
every file opens with a docstring on why it exists, and comments tagged
**`JS/TS vs Python:`** call out where Python behaves differently from what a
JavaScript/TypeScript developer would expect (`grep -rn "JS/TS vs Python" .`). A ten-point crib
sheet of the biggest differences (indentation, `None`, hints that aren't enforced, `self`, the
event loop...) is in the docstring of `app/__init__.py`.

> The integration story (browser → Next.js → backend → Postgres, the token, the
> redirect flow, cold starts, the shared database) is identical for every backend.
> The [Go README](https://github.com/kmonagle/resume_go#readme) has the long
> version; the essentials are repeated below so this one stands alone.

---

## The big picture

```
                    cookie: visitor_id                Authorization: Bearer <token>
                    (httpOnly, Next's domain)         X-Owner-Id: <visitor id>
┌─────────┐  HTTPS  ┌──────────────────────┐  HTTPS   ┌──────────────┐  SQL   ┌──────────┐
│ Browser │ ──────► │ Next.js (UI + BFF)   │ ───────► │ this Python  │ ─────► │  Neon    │
│         │ ◄────── │ resume_nextjs        │ ◄─────── │ service      │ ◄───── │ Postgres │
└─────────┘         └──────────────────────┘          └──────────────┘        └──────────┘
```

- The **browser never talks to this service**. It only sees the Next.js domain, so
  there is no CORS or cross-site-cookie problem, and the bearer token and this
  service's URL never reach client JavaScript. Next.js is a **BFF**
  (backend-for-frontend).
- Next.js picks its backend with an environment variable, `LINK_BACKEND`:
  **`local`** (its own Drizzle code) or **`remote`** (call one of these services, at
  `LINK_BACKEND_URL`). Point it at Go or at Python and the UI can't tell.
- **All backends share one Postgres database**, so links carry across them.

### Who owns what

| Concern | Owner |
|---|---|
| UI, dashboard polling, forms, the visitor cookie | Next.js |
| Input validation | **Both**: Next validates first (fast form errors); this service validates again because it must not trust its caller. Rules and messages match. |
| Business rules (limits, retention, 404 vs 410), atomic click counting | **Every backend**, with the same behaviour |
| Click event logging | Whoever serves the redirect (here). Next.js must not log too or clicks double count. |
| **Database schema and migrations** | **The Next.js repo.** This service never migrates. |
| The contract (OpenAPI spec + tests) | The Next.js repo, pinned by tag |

### How each Next.js feature becomes calls to this service

| In the UI | Next.js calls | Auth |
|---|---|---|
| Create a link | `POST /links` | bearer + `X-Owner-Id` |
| Dashboard (polls every 5 s) | `GET /links` | bearer + `X-Owner-Id` |
| Activate / Deactivate | `PATCH /links/{id}` | bearer + `X-Owner-Id` |
| Follow a short link `/r/{code}` | `GET /r/{code}` (redirect **not** followed) | none (public) |
| Footer "Served by: …" | `GET /meta` | none (public) |

### Identity and trust

Next.js gives each browser a random `visitor_id` cookie and copies it into an
`X-Owner-Id` header on every call. This service trusts that header **only because
the request also carries the shared bearer token** (`LINK_BACKEND_TOKEN`).
Render's free tier has no private networking, so the service is reachable from the
internet and the token is the only thing stopping anyone from claiming to be any
owner. Every query is also scoped by owner in SQL (`WHERE id = $1 AND owner_id =
$2`), so someone else's link id returns `404` (not `403`, which would confirm it
exists). The token is compared as SHA-256 hashes with `hmac.compare_digest`, so
timing can't be used to guess it. (See `require_owner` in `app/api.py`.)

The cookie identifies a *browser*, not a person: it is scoping for a login-free
demo, not authentication.

### The redirect

```
browser ─ GET /r/abc ─► Next.js ─ GET /r/abc ─► this service
                          (redirect: "manual",   │ 1. one atomic UPDATE: check every rule
                           forwards Referer +    │    AND count the click
                           User-Agent)           │ 2. reply 307 + Location
                                                 │ 3. BackgroundTask logs the click_events row
browser ◄─ 307 Location ─ Next.js ◄─ 307 ───────┘
```

Two things specific to this implementation:

- **The click is logged after the response is sent**, using FastAPI's
  `BackgroundTasks`, so analytics never slows the redirect. Two gotchas: background
  tasks only run automatically when FastAPI builds the response for you, and the
  redirect returns its own `Response`, so the tasks are handed over with
  `Response(..., background=background)` (otherwise they are silently dropped and
  no click is ever logged). And the task can't reuse the request's database
  session, which is already committed and closed by then, so it opens its own.
  (Verified: the row is written with the visitor's forwarded `Referer` and
  `User-Agent`.)
- **Click limits are enforced by one SQL statement** (`PostgresStore.claim`): check
  and increment together. SELECT, check in Python, then UPDATE would let two
  simultaneous requests both read "9 of 10", both pass, and both redirect. In a
  single `UPDATE`, Postgres locks the row, so the second request waits and then
  fails the `WHERE` clause. The contract suite fires 12 requests in parallel at a
  limit-2 link and expects exactly 2 redirects.

### One request, end to end

What happens to a `POST /links` from the moment it arrives (the shape is the same for every
route; the redirect adds a background task at the end):

```
Next.js ── POST /links ──► uvicorn accepts the connection and parses the HTTP request
                            │
                            ▼  FastAPI matches the route, then resolves its dependencies:
                            │    require_owner  → 401 / 400 here if the token or owner header
                            │                     is wrong (before the body is even looked at)
                            │    get_session    → opens a database session; BEGIN a transaction
                            │    get_store → get_service → SqlAlchemyStore(session), Service(store)
                            ▼
                            │  the JSON body is parsed and validated into CreateLink;
                            │  a bad body ends here as a 400 (the session is rolled back)
                            ▼
                            │  create_link() runs: Service.create → cleanup DELETE, two COUNTs,
                            │  INSERT: all in the one transaction
                            ▼
                            │  the route returns a LinkDTO; FastAPI serialises it (camelCase
                            │  JSON, status 201)
                            ▼
                            │  get_session's teardown COMMITs   ← scope="function": this happens
                            │                                     BEFORE the response is sent
                            ▼
Next.js ◄── 201 + JSON ─────┘   (for a redirect, the BackgroundTask runs now, in its own session)
```

### How Next.js calls this service

The Next.js side of the conversation is `src/server/link-api/remote.ts`. What it does, and so
what this service has to uphold:

- **Every call to `/links`** carries `Authorization: Bearer <token>` and `X-Owner-Id`, uses
  `cache: "no-store"` (live data must never be cached by Next's fetch layer), and has a 90-second
  timeout (a free-tier cold start is a slow request, not an error).
- **Redirects** use `redirect: "manual"`, because the caller wants this service's `Location`
  header itself and not the destination site's HTML. It forwards the visitor's `Referer` and
  `User-Agent` so the click log holds the real browser. `GET /r/{code}` needs no token.
- **Responses are validated with zod against the contract**, not trusted. So the shape has to be
  exact: camelCase keys, timestamps as ISO strings with milliseconds and a `Z`, nullable fields sent
  as `null` (never omitted), and `status` one of `active | expired | max_clicks | disabled`. The
  `LinkDTO` response model and its camel-case aliases are what guarantee that here. A response that
  breaks the contract is treated by Next.js as "backend unavailable".
- **Errors are mapped by status code** (table below), so returning the *right* status matters
  more than the message text, with one exception: a `410` body is shown to the visitor as-is, and
  `429`'s message is shown on the form.
- **The footer's "Served by" line** comes from `GET /meta` (via Next.js's own `/api/meta`), which
  is why `/meta` is open and never touches the database.

### What Next.js does when this service misbehaves

| This service answers | Next.js treats it as |
|---|---|
| `201` on create | success |
| `409` on create | "that code is taken" (a field error on the form) |
| `429` on create | "limit reached" (this service's message is shown) |
| `404` on toggle | link not found (someone else's, or it doesn't exist) |
| `404` / `410` on a redirect | Next's 404 page / a `410` with this service's message |
| `401` | a misconfigured token: logged on the Next.js side, surfaced as "backend unavailable" |
| `5xx`, invalid JSON, or a timeout | "backend unavailable" |

"Backend unavailable" becomes: a `503` from the JSON API; a "waking up, try again" message on the
form (which keeps what you typed); a `503` with `Retry-After: 30` on a short link; and "live
updates paused" on the dashboard, which keeps polling and recovers by itself.

## Free-tier cold starts (Render)

On Render's free plan a service sleeps after 15 minutes without traffic and takes from about ten
seconds to a minute to wake (measured on Render: Go and C# about 12 s, Python about 22 s; the JVM is
the slowest).

**Only a public request wakes it.** A request from the Next.js service to a sleeping backend (both on
Render) gets Render's HTML `502` page straight away and does **not** wake the service, so the UI shows
a "waking up" state and stays there; a request from a browser or `curl` is held until the service is up.
To demo the Python backend on the free tier, wake it first by opening
`https://<this-service>.onrender.com/meta` (or `curl` it), then use the site. Next.js degrades politely
while the backend is asleep (a `503` from its JSON API, an amber "waking up" message on the form and
dashboard) but cannot wake it itself. (An earlier design pinged the backend from a Next.js startup hook
so the two would wake together; that was removed because server-to-server requests don't wake it.)

- **Don't try to keep everything awake.** A free workspace gets about 750 instance-hours a month. One
  always-on service uses about 730; two would run out mid-month.
- `LINK_BACKEND=local` needs no second service at all.

On this side, `/meta` is the health check and doesn't touch the database, so the service reports
healthy the moment uvicorn is up; the first real query then opens a connection (and Neon's own
compute may also be waking, which adds a second or two).

## Startup, shutdown and scaling

- **Start:** the image runs `uvicorn app.main:create_app --factory`. `create_app` reads and
  validates the settings (a bad one stops the process with a clear message), then builds the app
  and a *lazy* database engine: nothing connects until the first query.
- **Port:** Render injects `PORT`; the Dockerfile's `CMD` passes it to uvicorn (default `8080`).
- **Stop:** Render sends `SIGTERM` on a redeploy. Uvicorn stops accepting connections, lets
  in-flight requests finish, then runs the shutdown half of the app's `lifespan`, which closes
  every pooled database connection.
- **One worker, five connections.** A single uvicorn process is one event loop on one thread, and
  the GIL means Python code can't run in parallel within a process; so the pattern is Node's:
  `await` slow I/O and never block the loop. To use more CPU cores you would run more processes
  (`--workers N`), which isn't needed on the free tier. The database pool is `pool_size=5,
  max_overflow=0`, so at most five queries run at once and the rest wait for a free connection
  (the parallel-click contract test fires 12 at once, so seven of them queue for a
  connection, which works because each request's transaction ends before its response is sent).

## The shared database

- **Schema ownership.** Tables are defined by Drizzle in the Next.js repo
  (`src/server/db/schema.ts`; generated SQL in `drizzle/`). The Next.js service
  applies migrations on every deploy; **this service never migrates**. On a new
  database, deploy or migrate Next.js first, or you'll see `relation "links" does
  not exist`.
- **Small differences from the ORM.** Drizzle generates ids and sets `updated_at`
  in application code; here `gen_random_uuid()` builds ids in SQL and every
  `UPDATE` sets `updated_at = now()` by hand. `owner_id` is `NOT NULL` with no
  default, so every insert must supply an owner.
- **Neon and SQLAlchemy.** The URL Neon hands out doesn't work with SQLAlchemy +
  asyncpg as-is, and `app/db.py` adapts it: the scheme becomes
  `postgresql+asyncpg://`; libpq's `sslmode` becomes asyncpg's `ssl` argument; and
  `channel_binding` (which Neon's copy button adds) is dropped, since asyncpg would
  reject it. Because Neon's pooled URL goes through PgBouncer in transaction mode,
  prepared-statement caching is turned off and statements get unique names (the
  Python twin of the Next.js app's `prepare: false`). Pre-ping replaces connections
  that Neon dropped while idle, and the pool is 5 connections because several
  services share one database. `tests/test_db.py` pins all of this down.
- **Changing the schema safely.** Because several backends share the database, a
  migration must leave *older* backends working (add a nullable column, upgrade
  every backend, then tighten it).

## The contract

`docs/openapi.yaml` in the Next.js repo is the source of truth. `.github/workflows/ci.yml`
here pins the version this service implements (`CONTRACT_REF: contract-v1`, a git
tag); CI checks it out, applies its `drizzle/*.sql` to a throwaway Postgres, starts
this service, and runs the shared suite against it. To upgrade, bump the tag, make
the new tests pass, and merge; other backends can stay on the old tag meanwhile, so
prefer *additive* contract changes.

**FastAPI writes its own OpenAPI document** (`/docs`, `/openapi.json`) from the route signatures.
It is a handy live reference, but it is *derived from this code* and is not the contract: the
hand-written spec in the Next.js repo is the source of truth, and the contract tests are what prove
this service matches it.

**Where a framework default disagreed with the contract** (each one is a place the tests would
fail, and each is fixed and commented in the code):

| Default behaviour | What the contract needs | How it is handled |
|---|---|---|
| Validation failures answer `422` with `{"detail": [...]}` | `400` with `{"error", "fieldErrors"}` | `handle_validation_error` in `app/api.py` |
| HTTP errors answer `{"detail": "..."}` | `{"error": "..."}` | `handle_http_exception` |
| An unhandled exception gives a plain-text 500 | a JSON `{"error"}` with nothing internal | `handle_unexpected` |
| pydantic coerces `"yes"` or `1` to `true` | only a real JSON boolean | `StrictBool` in `app/schemas.py` |
| datetime parsing accepts date-only and many other formats | RFC 3339 with an explicit offset | custom validator + regex |
| A `yield` dependency's cleanup runs *after* the response is sent | commit *before* the response | `Depends(..., scope="function")` |
| `BackgroundTasks` are dropped when you return your own `Response` | the click must be logged | `Response(..., background=...)` |

### Four implementations, side by side

| Python (this repo) | Go (`resume_go`) | C# (`resume_csharp`) | Next.js (`resume_nextjs`) | Job |
|---|---|---|---|---|
| `app/store.py`, `app/models.py` | `internal/store` | `Data/EfLinkStore.cs`, `LinksDbContext.cs` | `link-repository.ts`, `schema.ts` | the only code that runs queries; the table definitions |
| `app/service.py` | `internal/service` | `Services/LinkService.cs` | `link-api/local.ts` | limits, retention, codes, 404 vs 410 |
| `app/api.py` | `internal/api` | `Endpoints/` | `src/app/api/**`, `src/app/r/**` | HTTP handlers, auth |
| `app/schemas.py` | `internal/link/validate.go` | `Contracts/` | `link-schema.ts` | input validation, wire format |
| `app/domain.py` | `internal/link/link.go` | `Domain/` | `link-status.ts` | "is this link usable?" |
| `app/config.py`, `app/db.py` | `internal/config` | `Configuration/` | `env.ts`, `db/client.ts` | environment variables; connecting to Postgres |

The trade-offs are visible in numbers: this image is ~270 MB (the Python runtime and its
packages ship with it), against ~380 MB for C# (which carries the .NET runtime) and ~20 MB for
Go's single static binary.

## Environment variables

| Variable | Where | Meaning |
|---|---|---|
| `DATABASE_URL` | this service | Postgres URL. Use Neon's **pooled** URL in production. |
| `LINK_BACKEND_TOKEN` | this service **and** Next.js | Shared secret, 16+ characters. **Must be identical on both.** |
| `PORT` | this service | Render sets it; the Dockerfile defaults to `8080`. |
| `LINK_BACKEND=remote`, `LINK_BACKEND_URL` | Next.js | Point Next.js at this service's public URL. |

## Run it locally

```bash
# 1. Postgres, with the schema applied by the Next.js repo
docker run -d --name links-pg -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=links -p 54329:5432 postgres:17
(cd ../resume_nextjs && DIRECT_URL=postgres://postgres:dev@localhost:54329/links npx drizzle-kit migrate)

# 2. This service, in Docker
docker build -t resume-python .
docker run --rm -p 8080:8080 \
  -e DATABASE_URL="postgres://postgres:dev@host.docker.internal:54329/links" \
  -e LINK_BACKEND_TOKEN="local-dev-token-0123456789" resume-python

#    ...or without Docker (Python 3.13):
#    python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
#    DATABASE_URL=... LINK_BACKEND_TOKEN=... uvicorn app.main:create_app --factory --reload

# 3. Next.js in front of it (from ../resume_nextjs)
LINK_BACKEND=remote LINK_BACKEND_URL=http://localhost:8080 \
LINK_BACKEND_TOKEN=local-dev-token-0123456789 npm run dev
```

**Tests**

```bash
pytest                                              # unit tests: no database needed
ruff check . && ruff format --check .               # lint and formatting (also run in CI)
docker build --target test -t resume-python-test . && docker run --rm resume-python-test

# the shared contract suite against this service directly (from ../resume_nextjs):
CONTRACT_BASE_URL=http://localhost:8080 CONTRACT_IDENTITY=header \
CONTRACT_API_PREFIX="" CONTRACT_TOKEN=local-dev-token-0123456789 npm run test:contract
```

## Deploy on Render

Create a **Web Service** from this repo, runtime **Docker**, and set `DATABASE_URL`
(the Neon **pooled** URL) and `LINK_BACKEND_TOKEN`. Render provides `PORT`. Set the
health check path to `/meta`. To use it, set `LINK_BACKEND=remote`,
`LINK_BACKEND_URL` and the same `LINK_BACKEND_TOKEN` on the Next.js service. Setting
**Auto-Deploy** to "After CI Checks Pass" keeps a broken push out of production.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Next.js logs `Backend … failed (401)` | The two `LINK_BACKEND_TOKEN` values differ. |
| First page load takes a minute or two | Cold start: both services were asleep. |
| Startup error `LINK_BACKEND_TOKEN is required` | Missing or shorter than 16 characters. |
| Startup error about the database URL or SSL | Check `DATABASE_URL` is Neon's **pooled** URL; `app/db.py` already handles `sslmode` and `channel_binding`. |
| `relation "links" does not exist` | The schema hasn't been applied; migrate the Next.js repo first. |
| Clicks never appear in `click_events` | The redirect stopped passing `background=` to its `Response`, or the task is failing (see the logs). |
| Requests hang for ~30 s under concurrent clicks | The session dependency lost `scope="function"`: see "Design decisions". |
| Clicks counted twice | Something else is also logging click events. |
| CI can't check out the contract | The tag isn't pushed, the Next.js repo is private, or `CONTRACT_REPO` is wrong. |

## Layout

```
app/main.py      builds the FastAPI app (factory); disposes the engine at shutdown
app/api.py       routes, dependencies (session, auth), contract-shaped errors
app/service.py   business rules; the Store protocol; result types
app/store.py     the ONLY module that runs queries (SQLAlchemy)
app/models.py    SQLAlchemy table classes: a MIRROR of the schema Drizzle owns
app/db.py        engine + session factory; adapts the Neon URL
app/schemas.py   pydantic request models, validation rules, wire format
app/domain.py    Link model, status rules, short-code generation (no I/O)
app/config.py    pydantic-settings: environment variables, validated at startup
tests/           pytest: domain, schemas, config, db URL, service (fake store), API
Dockerfile       three stages: base, test, runtime
pyproject.toml   pytest and ruff (lint + format) configuration
```

Suggested reading order: `domain.py` → `schemas.py` → `models.py` → `store.py` →
`service.py` → `db.py` → `api.py` → `main.py`.

## Design decisions worth talking about

Each of these is a choice with a reason, and the trade-off is stated so it can be
challenged.

- **A conventional stack, on purpose.** FastAPI, pydantic v2, SQLAlchemy 2.0 async
  over asyncpg, pydantic-settings, pytest and ruff: the mainstream Python service
  stack, so a Python developer can navigate it without learning anything bespoke.
  (An earlier version used raw asyncpg and hand-rolled settings; it worked, but it
  was an unusual pattern for a reader to meet.)
- **SQLAlchemy for everything except the one statement that matters.** Reads,
  inserts and toggles use the ORM's expression language. `claim` (check the rules
  *and* count the click) is deliberately an explicit `UPDATE ... WHERE ...
  RETURNING`, not the usual load-modify-commit sequence, which would let concurrent
  requests overshoot a click limit. The ORM is a convenience; correctness lives in
  the database, and the code says so where it counts.
- **No Alembic.** The usual companion to SQLAlchemy is not used because this
  service doesn't own the schema: the Next.js repo's Drizzle migrations do, and
  `app/models.py` is a hand-kept mirror. If this service owned the tables, Alembic
  would generate and apply the migrations.
- **One session (one transaction) per request, and the teardown must run before the
  response.** The session is a dependency that commits when the route returns and
  rolls back if it raised. It is declared `scope="function"` so the commit happens
  *before* the response is sent. With FastAPI's default the commit is late, and under
  concurrent clicks the service deadlocks (the contract's parallel-click test hangs
  for 30 s: reproduced by removing the argument), because held connections starve
  the click-logging task.
- **The route → service → store split, even though the ORM could do it all in the
  route.** The service holds the rules (limits, retries, 404 vs 410) with no HTTP or
  SQL in it, so it is tested against a hand-written fake. The layering also mirrors
  the Go and Next.js implementations, which makes the three easy to compare.
- **The store is a `Protocol`,** so the service depends on a shape, not on
  SQLAlchemy. Tests pass a small fake; no mocking library.
- **Expected outcomes are values.** `Created | CodeTaken | LimitReached` is a union
  matched with `match`, and only genuine failures are exceptions.
- **Validation is the type.** A `CreateLink` parameter means the body was parsed and
  validated before the route runs. Custom validators (not just `Field` constraints)
  are used so error messages match the other two implementations word for word.
- **Domain objects, not ORM objects, leave the store.** Mapping rows to plain
  dataclasses keeps the ORM from leaking upward (no accidental lazy loading, no
  session held open by a stray object).
- **Settings are validated once, at startup,** and the app is built by a factory, so
  importing it has no side effects and tests build one with no database (using
  `dependency_overrides`).
- **The contract is pinned and tested,** so "same behaviour in three languages" is
  checked on every push, not just claimed.
