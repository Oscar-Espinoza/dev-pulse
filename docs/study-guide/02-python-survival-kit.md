# 02 — Python survival kit

You don't need to learn Python to read this codebase. You need about eight ideas. This
chapter is all of them, each with a pointer to where it first shows up. Treat it as a
reference to come back to, not something to memorize now.

## 1. Indentation is syntax

Python has no braces. A block is defined by being indented under the line that opens it,
and that line ends with a colon.

```python
if api_key is None:
    raise HTTPException(status_code=401, detail="Missing X-API-Key header")
return user
```

The `raise` runs only when the condition holds; the `return` always runs. Getting the
indentation wrong isn't a style error, it's a different program.

## 2. Type hints — and here, they *do* things

```python
def job_id(monitor_id: int) -> str:
    return f"monitor:{monitor_id}"
```

`monitor_id: int` says the parameter is an integer; `-> str` says the function returns a
string. In most Python projects these are documentation that the runtime ignores.

**In this project they are load-bearing.** Three libraries read them:

- FastAPI reads a route's hints to know what to parse from the request, what to validate,
  and what to put in the OpenAPI docs.
- SQLAlchemy 2.0 reads model hints to define database columns.
- Pydantic reads schema hints to decide what JSON is acceptable.

So when you see a type in `app/`, assume it's doing work.

Syntax you'll meet:

- `int | None` — "an integer, or nothing." First appears at `app/models.py:103`
  (`status_code`), and the *reason* it's nullable is a real product decision (chapter 04).
- `list[Monitor]`, `dict[str, str]` — a list of monitors, a dictionary of string→string.
- `Mapped[int]` — SQLAlchemy's wrapper meaning "this is a database column holding an
  int." `app/models.py:52`.
- `Annotated[int, Query(ge=1, le=8760)]` — "an int, *and* here's extra metadata about it."
  `app/routers/monitors.py:208`. The metadata (`ge=1, le=8760`) is what makes FastAPI reject
  `?hours=0` with a 422 automatically.

`Annotated` is the pattern that makes this codebase's dependency injection readable — see
idea 7.

## 3. `async` / `await` and the event loop

This is the big one. Nearly every function in `app/` is `async def`, and understanding why
explains the architecture.

**The problem.** This app spends almost all its time *waiting*: waiting for a monitored
website to respond, waiting for Postgres to return rows. Traditional code blocks during a
wait — while checking one monitor, the process can do nothing else. With hundreds of
monitors and a 10-second timeout each, that's fatal.

**The solution.** An `async def` function can be *paused*. When it hits an `await` on
something slow, it hands control back to a scheduler called the **event loop**, which runs
other work while the wait is outstanding, and resumes the paused function when its answer
arrives. One thread, hundreds of overlapping waits.

```python
response = await client.request(method, url, headers=headers)
```

That's `app/services/checker.py:45`. Read it as: "start this HTTP request; while it's in
flight, let the event loop do something else; when the response arrives, continue here."

Three rules that follow:

- `await` is only legal inside `async def`.
- Calling an `async def` without `await` doesn't run it — you get a coroutine object that
  does nothing. This is the single most common beginner bug.
- One slow *non-async* operation blocks everything. This is why the app uses `asyncpg`
  and `httpx` rather than their blocking equivalents: a blocking database driver would
  freeze the event loop, and the scheduler with it.

This is also why chapter 08's scheduler is an `AsyncIOScheduler` specifically — it runs
jobs *on the same event loop* as the web server, rather than in threads.

## 4. Decorators

A line starting with `@` above a definition wraps it in something else.

```python
@router.get("/{monitor_id}/stats", response_model=MonitorStats)
async def monitor_stats(...):
```

`app/routers/monitors.py:203`. The function is just a function; the decorator registers it
with FastAPI as the handler for that URL. You'll meet four in this codebase:

| Decorator | Where | What it does |
|-----------|-------|--------------|
| `@router.get` / `.post` / `.patch` / `.delete` | `app/routers/*.py` | Registers an HTTP route |
| `@lru_cache` | `app/config.py:27` | Caches the result, so the function body runs once ever |
| `@dataclass(slots=True)` | `app/services/checker.py:15` | Generates a simple data-holding class |
| `@asynccontextmanager` | `app/main.py:20` | Turns a generator into a startup/shutdown resource (idea 6) |

## 5. Context managers: `with` and `async with`

```python
async with session_factory() as session:
    ...
```

`app/database.py:64`. This means: open a database session, run the indented block, and
**close the session no matter how the block ends** — normal return, early return, or
exception. It's Python's answer to `try/finally`, and it's how this codebase guarantees
that database connections and HTTP clients are never leaked.

Chapter 12 contains a war story about resource cleanup that this idea makes readable — a
bug where cleanup was arranged *almost* correctly.

## 6. Generators, `yield`, and why dependencies look strange

Most functions `return` once. A function containing `yield` instead *pauses* there, hands
a value out, and can resume afterward.

FastAPI uses this for resources with a teardown step:

```python
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session
```

`app/database.py:57-65`. Read it as: "make a session, hand it to the route handler
(`yield`), and when the request finishes, resume here" — which exits the `async with`, which
closes the session. Setup and teardown in one function, with the request happening in the
middle.

The same shape appears at `app/main.py:20-54` for the whole application's lifetime, which
is chapter 12.

## 7. Dependency injection: `Depends` and the `Annotated` alias

> ⚙️ **FastAPI note:** This is FastAPI's central idea, so it's worth 60 seconds.

A route declares what it *needs* as a parameter, and FastAPI supplies it:

```python
async def list_monitors(user: CurrentUser, session: DbSession) -> list[Monitor]:
```

`app/routers/monitors.py:109`. The route never fetches the user or opens a session. It
declares two needs and receives them.

`CurrentUser` and `DbSession` are just named aliases:

```python
DbSession = Annotated[AsyncSession, Depends(get_db)]      # app/database.py:68
CurrentUser = Annotated[User, Depends(get_current_user)]  # app/dependencies.py:45
```

Read `Annotated[X, Depends(f)]` as: "the value has type `X`, and FastAPI gets it by calling
`f`." The alias exists so routes stay readable — `user: CurrentUser` instead of
`user: Annotated[User, Depends(get_current_user)]` on every single route.

Two consequences worth holding onto:

- **Auth is not a line of code in any route.** `get_current_user` raises a 401 before the
  route body runs, so a route that declares `user: CurrentUser` is authenticated by
  construction. Chapter 10.
- **Dependencies can be swapped in tests.** Because the route asks for `get_db` rather than
  building its own connection, tests can substitute a SQLite session with one line.
  Chapter 13.

## 8. Small things you'll see constantly

**f-strings** — string interpolation:
```python
f"monitor:{monitor_id}"        # app/services/scheduler.py:44
```

**Keyword-only arguments** — a bare `*` in a signature forces everything after it to be
passed by name:
```python
def sync_monitor(self, monitor_id: int, *, is_active: bool, interval_seconds: int)
```
`app/services/scheduler.py:97`. Callers must write `sync_monitor(5, is_active=True,
interval_seconds=60)`. You cannot accidentally swap two booleans — the compiler makes you
say which is which.

**`None` and truthiness** — `None` is Python's null. Watch for the distinction between
`if x:` (false for `None`, `0`, `""`, and empty collections) and `if x is None:` (only
true for `None`). It matters at `app/routers/monitors.py:249-261`, where `if total` and
`is not None` do deliberately different jobs — chapter 14 explains why.

**`self`** — the explicit first parameter of a method, equivalent to `this` in other
languages. `app/services/scheduler.py` is the only class with real behavior in this app.

**Leading underscore** — `_get_owned_monitor`, `_run_check`. A convention, not enforced:
"internal, don't call from outside this module."

## Where to go when stuck

The fastest way to understand an unfamiliar line here is usually not to read Python docs —
it's to find the test that exercises it. Every behavior in `app/` has a test in `tests/`
that states what it's supposed to do in plain assertions. Chapter 13 shows how to navigate
them.

### 🔍 Try it

1. **Watch the "forgot to await" bug happen.** Open a Python REPL with your venv active:

   ```bash
   python -c "
   import asyncio
   async def f(): return 42
   print(f())          # coroutine object — the function never ran
   print(asyncio.run(f()))  # 42
   "
   ```

2. **Find every dependency alias in the codebase:**

   ```bash
   grep -rn "Annotated\[" app/ | grep Depends
   ```

   Three results. Those three lines are how every route in the app gets its database
   session, its user, and its scheduler.

3. **Prove type hints are load-bearing.** Look at `app/routers/monitors.py:208`, then try
   the endpoint's validation from the tests:

   ```bash
   pytest tests/test_monitors_api.py -k hours_bounds -v
   ```

   That test passes because of `ge=1, le=8760` inside an `Annotated` type. No `if` statement
   anywhere rejects those values.

---

Previous: [01 — The stack](01-stack.md) · Next: [03 — Config & logging](03-config-and-logging.md)
