# 05 — Database plumbing

File: `app/database.py`, 69 lines. Three functions. The middle one is boring and the other
two are where hosting reality shows up in the source code.

## Three layers, in order

**Engine** — the connection pool. One per process, expensive to create, holds open TCP
connections to Postgres. Built by `build_engine` at `app/database.py:45`.

**Session factory** — a thing that makes sessions. Created once in the lifespan
(`app/main.py:33`) from the engine.

**Session** — one unit of work: a transaction, an identity map, a place to stage changes
before committing. Short-lived. In this app, one per HTTP request.

That last rule is the important one.

## One session per request, closed no matter what

```python
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session

DbSession = Annotated[AsyncSession, Depends(get_db)]
```

`app/database.py:57-68`.

This is the `yield` dependency pattern from chapter 02, idea 6: open a session, hand it to
the route (`yield`), and when the response has been sent, resume and let the `async with`
close it. Setup and teardown around the entire request, in five lines.

> 🔑 **Decision: routes never construct their own session.** Every route declares
> `session: DbSession` and receives one. That single indirection is what makes the whole
> test suite possible — chapter 13 swaps `get_db` for a SQLite version with one line, and
> every route in the app follows along without knowing.

Note where the factory comes from: `request.app.state.session_factory`. It's not a module
global. It's an attribute set during startup on the running application object
(`app/main.py:40`). Chapter 12 covers `app.state` as the wiring point; the consequence here
is that a session can only be created while an app is actually running, which is what you
want.

## `expire_on_commit=False`

Set at `app/main.py:33`, but it belongs to this chapter conceptually.

SQLAlchemy's default behavior is: after you commit, every object you loaded is marked
stale, so touching any attribute triggers a fresh SELECT. That's a safe default for
long-lived sessions in a desktop app. In a web request it's a trap — you commit a new
monitor, then return it, and FastAPI's serialization of `monitor.id` fires another query
against a session that's about to close.

Turning it off means committed objects stay usable. It's why `create_monitor` can commit
and then return the object (`app/routers/monitors.py:97-105`) without ceremony.

## The Neon block: five lines that exist because of hosting

Now the interesting part. `prepare_engine_args` (`app/database.py:16-42`) exists to
translate a connection string you paste from a hosting dashboard into something `asyncpg`
will actually accept. Its docstring records all three problems.

### Problem 1: the driver isn't in the URL

```python
if url.drivername in {"postgres", "postgresql"}:
    url = url.set(drivername="postgresql+asyncpg")
```
`app/database.py:31-32`

Every Postgres host on earth hands you a URL starting `postgresql://`. SQLAlchemy needs to
know *which Python driver* to use, spelled `postgresql+asyncpg://`. Rather than making
every developer remember to hand-edit the URL — and rather than having the app crash
confusingly when they forget — the app rewrites it.

Small thing. But it means `.env.example` can honestly say *"Neon URLs work as-is."*

### Problem 2: asyncpg rejects parameters that Postgres URLs normally carry

```python
sslmode = query.pop("sslmode", None)
query.pop("channel_binding", None)
url = url.set(query=query)
if sslmode not in (None, "disable"):
    connect_args["ssl"] = True
```
`app/database.py:35-39`

Hosted Postgres URLs usually arrive with `?sslmode=require&channel_binding=require`. Those
are parameters understood by `libpq`, the standard C client. `asyncpg` is a from-scratch
implementation that doesn't use `libpq`, and it **raises an error** on unknown parameters.

So the code strips them from the URL and passes the *intent* — "use TLS" — through
`connect_args["ssl"] = True` instead. Note the condition: `sslmode` of `"disable"` is
respected as a genuine request for no TLS, rather than being blindly upgraded.

### Problem 3: connection poolers break prepared statements

```python
if url.host is not None and "-pooler" in url.host:
    connect_args["statement_cache_size"] = 0
```
`app/database.py:40-41`

This is the subtlest of the three, and the docstring at `:23-25` states it precisely:
*"Hosts containing `-pooler` (Neon's pgbouncer endpoint) get `statement_cache_size=0`
because transaction-mode pgbouncer breaks asyncpg's prepared-statement cache."*

Unpacked: asyncpg speeds things up by *preparing* a query once on a connection and reusing
it. A transaction-mode connection pooler (pgbouncer) hands your app a *different* physical
connection for each transaction. So asyncpg prepares a statement on connection A, then
tries to reuse it on connection B, where it doesn't exist — and you get intermittent,
maddening `prepared statement "__asyncpg_stmt_1__" does not exist` errors under load.

The fix is to disable the cache when talking to a pooler, detected by the `-pooler` in
Neon's hostname.

> 🔑 **Decision: detect the pooler from the hostname rather than adding a setting.** It
> means the operator can paste either the pooled or the direct URL and get correct behavior
> either way — one less thing to get wrong at 2am. The cost is a heuristic tied to one
> vendor's naming convention. That's a real trade-off, and worth knowing about if this ever
> moves off Neon.

These three fixes are the kind of knowledge that normally lives in one engineer's head and
evaporates when they leave. Here they're in a docstring, and each has a test named after
its reason — look at `tests/test_database.py`.

## `pool_pre_ping=True`

```python
return create_async_engine(url, echo=echo, connect_args=connect_args, pool_pre_ping=True)
```
`app/database.py:52-54`, with the reason in the docstring at `:48-49`: *"revalidates
pooled connections, which matters for Neon's scale-to-zero behavior (idle connections get
dropped server-side)."*

Connection pools keep TCP connections open for reuse. Serverless Postgres like Neon scales
to zero when idle and silently drops those connections. Your pool doesn't notice — it
hands out a dead connection and the next query fails.

`pool_pre_ping` issues a cheap "are you alive?" before handing out any pooled connection,
transparently replacing dead ones. It costs a tiny round-trip per checkout and eliminates a
class of intermittent errors that are miserable to diagnose.

## The escape hatch: non-Postgres URLs pass straight through

Read `prepare_engine_args` once more and notice its structure: everything interesting is
inside `if url.drivername in {"postgres", "postgresql"}` and
`if url.drivername == "postgresql+asyncpg"`. A SQLite URL matches neither, so it returns
untouched with empty `connect_args`.

> 🔑 **Decision: one `build_engine` for production and tests.** The alternative — a
> separate code path for test databases — means the engine construction used by tests isn't
> the engine construction used in production, so the tests can't catch bugs in it. Here,
> `tests/test_database.py` exercises the real function, and `tests/test_app_smoke.py` boots
> the real application through it against a SQLite file.

## What's *not* here

There's no `Base.metadata.create_all()` anywhere in `app/`. The application never creates
its own schema — that's Alembic's job, and it's the subject of chapter 06. `create_all`
appears only in `tests/conftest.py:37` and `tests/test_app_smoke.py:20`.

### 🔍 Try it

1. **Watch the URL normalization happen:**

   ```bash
   python -c "
   from app.database import prepare_engine_args
   for u in [
       'postgresql://u:p@host/db',
       'postgresql://u:p@ep-cool-pooler.neon.tech/db?sslmode=require&channel_binding=require',
       'sqlite+aiosqlite:///./test.db',
   ]:
       print(prepare_engine_args(u))
   "
   ```

   Three inputs, three different treatments. The middle one shows all three fixes firing at
   once; the last shows the pass-through.

2. **Read the tests that name their own reasons:**

   ```bash
   pytest tests/test_database.py -v
   ```

   The test names *are* the documentation. This is a pattern worth stealing.

3. **See `echo_sql` do its thing.** Set `ECHO_SQL=true` in your `.env` and run one test —
   every SQL statement the app emits gets logged. It's noisy, which is why it's off by
   default, and it's the fastest way to answer "what query did that actually run?" when you
   get to chapter 14.

---

Previous: [04 — The data model](04-data-model.md) · Next: [06 — Migrations](06-migrations.md)
