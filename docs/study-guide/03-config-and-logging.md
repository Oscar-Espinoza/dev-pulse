# 03 — Config & logging

Files: `app/config.py` (31 lines), `app/logging.py` (34 lines). The two smallest modules in
the app, and the first ones built — everything else depends on them.

## Configuration is policy, not constants

Open `app/config.py:8-24`. The entire configuration is six fields on one class:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://devpulse:devpulse@localhost:5432/devpulse"
    echo_sql: bool = False
    json_logs: bool = False
    max_active_monitors_per_user: int = 20
    min_interval_seconds: int = 60
    http_timeout_seconds: float = 10.0
```

`BaseSettings` comes from pydantic-settings. Inheriting from it means every field is
automatically populated from an environment variable of the same name, case-insensitively —
`MAX_ACTIVE_MONITORS_PER_USER=5` in the environment overrides the default of 20. The values
written in the class are *defaults*, not values.

> 🔑 **Decision: the business limits are settings, not constants.** "Max 20 monitors per
> user" and "minimum 60-second interval" could each have been a number typed directly into
> `app/routers/monitors.py`. Making them settings means the rules can differ between
> development and production without a code change or redeploy, and — as chapter 13 shows —
> means tests can set the quota to 2 and verify the limit logic in three requests instead
> of twenty-one.

Notice what these numbers are *protecting*. `min_interval_seconds: 60` stops a user from
pointing DevPulse at someone else's website with a 1-second interval and turning it into a
denial-of-service tool. `max_active_monitors_per_user: 20` caps how much outbound traffic
one account can generate. `http_timeout_seconds: 10.0` stops one hanging URL from tying up
a check slot forever. They're a small rate-limiting policy expressed as configuration.

Chapter 15 covers the hole in that policy that the project knows about and documented.

## `extra="ignore"` and why the API surface is forgiving

`app/config.py:16` sets `extra="ignore"`. Without it, an environment variable in `.env`
that doesn't match a field would raise an error at startup. With it, unknown variables are
silently skipped.

That's the right call for a deployed service: hosting platforms inject dozens of their own
environment variables (`PORT`, `RENDER_*`, `PATH`), and an app that refuses to boot because
it doesn't recognize one of them is an app that doesn't boot.

## `@lru_cache`: one settings object, forever

```python
@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`app/config.py:27-30`.

> 🐍 **Python note:** `@lru_cache` memoizes a function — the body runs on the first call,
> and every later call returns the same stored object without re-executing. On a
> zero-argument function it's effectively "run once, ever."

Two reasons this matters:

1. **Efficiency.** Constructing `Settings()` reads and parses the `.env` file. Without the
   cache, every request that needs a setting would re-read it from disk.
2. **Identity.** Because every caller gets *the same object*, FastAPI's dependency system
   can treat `get_settings` as a single swappable thing.

That second point is what makes this test possible (`tests/test_monitors_api.py:72`):

```python
app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, max_active_monitors_per_user=2)
```

The test says "for this app, `get_settings` now returns a quota of 2" — no environment
variables, no monkeypatching, no cleanup. Chapter 13 returns to this pattern.

The cache has one sharp edge: it persists across tests in the same process. That's why
`tests/test_app_smoke.py:15` and `:47` call `get_settings.cache_clear()` before and after
changing `DATABASE_URL` via the environment. If you ever find a test mysteriously seeing
another test's configuration, this is the reason.

## Logging: fields, not sentences

`app/logging.py:9-33` configures structlog once, at startup. The important part is what
comes *out*.

Compare two ways of recording the same event:

```python
print(f"Checked monitor {id}: success={ok} in {ms}ms")     # a sentence
logger.info("monitor_checked", monitor_id=id, success=ok, response_time_ms=ms)   # fields
```

The real call is `app/services/checker.py:96-103`. In development it prints readable
colored output; in production, with `JSON_LOGS=true`, the same call emits a JSON object.
The difference matters the day something breaks at 2am: with JSON logs you can ask your log
tool for "every record where `success=false` and `monitor_id=5`." With printed sentences
you're writing regexes against prose.

> 🔑 **Decision: `json_logs` is a deployment switch, not a code change.** Same log
> statements, different renderer, chosen by an environment variable
> (`app/logging.py:17-19`). Development gets human-readable output; production gets machine
> parseable output. Neither requires touching the code that does the logging.

## The one line that unifies three log streams

```python
logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
```

`app/logging.py:16`, with the docstring at `:14` recording exactly why:
*"Root stdlib logging level; APScheduler/uvicorn logs flow through it."*

Here's the problem it solves. structlog is what *your* code uses. But APScheduler and
uvicorn are third-party libraries that use Python's built-in `logging` module, and they'll
happily emit their own differently-formatted messages. Without this line you'd get two
interleaved log formats in one stream — and when APScheduler complains that a job
misfired, it would be in the format you *didn't* configure.

Wiring structlog through the standard library's machinery
(`structlog.stdlib.LoggerFactory()` at `app/logging.py:31`) and setting the root level
means everything lands in one pipeline with one format. It's four lines of setup that make
the production logs actually usable.

## Reading the processor chain

`app/logging.py:21-28` lists what happens to every log record, in order: merge any
context variables, add the logger's name, add the level, add an ISO-8601 UTC timestamp,
render stack info and exceptions if present, then hand off to the renderer.

The one worth noting is `TimeStamper(fmt="iso", utc=True)`. **Everything in this codebase
is UTC** — logs here, database timestamps in chapter 04, the scheduler's timezone in
chapter 08. That consistency is not decorative: chapter 14 has an entire section on what
goes wrong at the boundary between timezone-aware and timezone-naive times.

## `.env` and `.env.example`

`.env` is gitignored — it holds real credentials. `.env.example` is committed and
documents what's expected, with comments that record intent:

```
# Postgres connection string. Neon URLs work as-is (pooled or direct);
# plain postgresql:// is normalized to postgresql+asyncpg:// automatically.
# Log SQL statements (dev only)
# Emit JSON logs (enable in production)
```

That first comment is a pointer to chapter 05, which is about the surprisingly involved
machinery hiding behind "work as-is."

### 🔍 Try it

1. **Override a setting from the environment and watch it take effect:**

   ```bash
   python -c "
   from app.config import Settings
   print(Settings(_env_file=None).max_active_monitors_per_user)
   "
   MAX_ACTIVE_MONITORS_PER_USER=3 python -c "
   from app.config import Settings
   print(Settings(_env_file=None).max_active_monitors_per_user)
   "
   ```

   20, then 3. No code changed.

2. **See both log renderers:**

   ```bash
   python -c "
   from app.logging import configure_logging
   import structlog
   configure_logging(json_logs=False)
   structlog.stdlib.get_logger('demo').info('monitor_checked', monitor_id=5, success=False)
   "
   ```

   Then run it again with `json_logs=True` and compare. Same call, two shapes — one for
   your eyes, one for a log aggregator.

3. **Read the test that pins the defaults** — `tests/test_config.py`. Note the
   `_env_file=None` argument: it tells pydantic-settings to ignore any `.env` on disk, so
   the test asserts the *code's* defaults rather than whatever your local file happens to
   say. That's a small, easy-to-miss piece of test hygiene worth copying.

---

Previous: [02 — Python survival kit](02-python-survival-kit.md) · Next: [04 — The data model](04-data-model.md)
