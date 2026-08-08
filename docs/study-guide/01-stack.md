# 01 — The stack: why each library

Open `pyproject.toml`. Nine runtime dependencies, three dev dependencies. Every one is
there for a reason someone wrote down, and two of them carry version constraints that exist
to prevent specific disasters.

The full list lives at `pyproject.toml:6-24`.

## The dependency list, annotated

**`fastapi`** — the web framework. Chosen for three things that show up constantly in this
codebase: dependency injection (chapter 10), lifespan management for startup/shutdown
resources (chapter 12), and automatic OpenAPI docs generated from type hints. That last one
means `/docs` in a browser gives you a working API explorer that nobody had to write or
maintain.

**`uvicorn[standard]`** — the server that actually runs FastAPI. FastAPI is a framework;
it doesn't listen on a port by itself. `[standard]` pulls in the faster optional pieces.

**`sqlalchemy[asyncio]>=2.0.51`** — the ORM (object-relational mapper): Python classes that
map to database tables, so you write `session.get(Monitor, 5)` instead of raw SQL. The
`[asyncio]` extra is required because this whole app is async (chapter 02 explains what
that means).

> 🔑 **Decision: SQLAlchemy ORM + separate Pydantic schemas, explicitly *not* SQLModel.**
> SQLModel is a popular library that merges the two — one class is both your database table
> and your API contract. This project rejected it and keeps two independent layers. Chapter
> 09 shows what that buys: the API can expose fields the table doesn't have, hide fields it
> does have, and validate things the database can't express. The cost is writing the field
> list twice.

**`asyncpg`** — the Postgres driver. Fast, async-native. It's also opinionated in ways that
cause three specific workarounds in `app/database.py` — chapter 05 is largely about them.

**`alembic`** — database migrations. Chapter 06.

**`httpx`** — the HTTP client used to probe monitored URLs. Async, and — importantly for
the tests — it has a pluggable transport layer, so the test suite can swap the network for
a Python function. Chapter 13.

**`apscheduler>=3.11,<4.0`** — the scheduler.

> 🔑 **Decision: pinned to APScheduler 3.x, never 4.0.** The build plan states the reason
> flatly: *"4.0 is still pre-release and its docs warn against production use."* This is a
> constraint you should not casually relax — the 4.0 API is different enough that upgrading
> is a rewrite of `app/services/scheduler.py`, not a version bump. The upper bound `<4.0`
> in `pyproject.toml:13` is what stops a routine `pip install --upgrade` from silently
> breaking the app.

> 🔑 **Decision: in-process APScheduler, not Celery.** Celery is the default answer for
> "run background jobs in Python," and it would work here. It also requires a message
> broker (Redis or RabbitMQ), a second process to run workers, and deployment complexity
> that a portfolio project doesn't want to pay for. APScheduler runs *inside* the app's
> event loop — no broker, no second service. What you give up is described in chapter 00:
> you can only run one instance.

**`pydantic[email]`** — data validation. Pydantic is the layer that turns untrusted JSON
into typed Python objects, rejecting anything malformed with a 422 before your code sees
it. The `[email]` extra adds `EmailStr`, which is why `POST /users` can reject
`"not-an-email"` without a line of validation code.

**`pydantic-settings`** — the same validation machinery, applied to environment variables.
Chapter 03.

**`structlog`** — structured logging.

> 🔑 **Decision: structlog only — no `print()`, no bare `logging` calls in application
> code.** The reason is that logs from a running service are only useful if you can search
> them. `logger.info("monitor_checked", monitor_id=5, success=False)` produces a record with
> *fields*, so in production (with `JSON_LOGS=true`) you can query "all failed checks for
> monitor 5" directly. A `print(f"checked monitor 5")` produces a string you have to write a
> regex against. Chapter 03 covers the setup, including the one line that makes APScheduler's
> and uvicorn's own logs flow through the same pipeline instead of a second, differently
> formatted one.

## The dev dependencies

**`pytest`** + **`pytest-asyncio`** — the test runner, plus the plugin that lets tests be
`async def`. Look at `pyproject.toml:34`:

```toml
asyncio_mode = "auto"
```

> 🔑 **Decision: `asyncio_mode = "auto"`.** Without it, every single async test needs a
> `@pytest.mark.asyncio` decorator above it. With it, pytest just notices the test is async
> and does the right thing. It's a small thing that removes ~50 lines of ceremonial noise
> from the test suite — and it's why you'll never see that decorator in `tests/`.

**`aiosqlite`** — an async SQLite driver. This is a dev dependency for one reason: the test
suite runs the *entire application* against an in-memory SQLite database instead of
Postgres. That's why `pytest` needs no infrastructure. It also imposes a constraint that
shows up repeatedly: any SQL the app writes must work on both Postgres and SQLite. Chapters
04 and 14 both hit that wall and show how it was handled.

## The version floor: Python 3.12

`pyproject.toml:5` requires Python ≥3.12. The recorded reason is pragmatic — *"machine has
3.12.3"* — but 3.12 also gives modern type syntax like `int | None` instead of
`Optional[int]`, which this codebase uses everywhere.

Note what *isn't* used: the project installs with plain `venv` + `pip install -e ".[dev]"`,
not `uv` or Poetry. Recorded reason: `uv` wasn't installed on the machine. This is a good
example of a decision that is *not* deep — sometimes the answer really is "that's what was
available," and recording it saves the next person from hunting for a principle that isn't
there.

## Two constraints that apply everywhere

The build plan lists project-wide rules. Two shape almost every file you'll read:

**Full type hints and docstrings on every module, class, and public function.** Open any
file in `app/` and you'll see it. This isn't decoration — FastAPI *reads* the type hints to
generate validation and documentation, and SQLAlchemy 2.0 reads them to define columns. In
this codebase, types are executable.

**One commit per task.** Each of the eight build tasks is exactly one commit. That's why
`git log` reads like a table of contents, and why `git show <sha>` is a useful way to study
a single subsystem in isolation.

### 🔍 Try it

1. **Confirm the APScheduler pin is doing something:**

   ```bash
   pip index versions apscheduler 2>/dev/null | head -3
   pip show apscheduler | grep -i version
   ```

   If a 4.x exists and you're on 3.x, the pin is actively protecting you.

2. **See the OpenAPI docs FastAPI generates for free.** Start the app (it will fail to
   fully start without a database — that's expected and fine for this step; if you want it
   to boot cleanly, come back after chapter 06):

   ```bash
   uvicorn app.main:app --reload
   ```

   Then open http://localhost:8000/docs. Every endpoint, every field, every validation
   rule on that page was generated from type hints. Nobody wrote it.

3. **Look at the structured-vs-string logging difference.** Search for how the app logs:

   ```bash
   grep -rn "logger.info" app/ | head -5
   ```

   Every one passes *fields*, not a formatted sentence. Compare `monitor_checked` in
   `app/services/checker.py:96-103` — six named fields — to what a `print()` of the same
   information would look like.

---

Previous: [00 — Orientation](00-orientation.md) · Next: [02 — Python survival kit](02-python-survival-kit.md)
