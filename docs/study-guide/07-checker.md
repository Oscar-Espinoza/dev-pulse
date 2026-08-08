# 07 — The checker

File: `app/services/checker.py`, 104 lines. This is the part that does the actual product:
make an HTTP request, decide whether it counted, write it down.

It contains the single most important decision in the codebase.

## The decision: failures are data, not exceptions

```python
async def perform_check(...) -> CheckOutcome:
    """Probe ``url`` and classify the outcome.

    Never raises for HTTP-level problems: timeouts, connection failures and
    unexpected status codes all come back as a failed ``CheckOutcome``.
    """
```
`app/services/checker.py:30-42`

> 🔑 **Decision: `perform_check` never raises for anything that could happen to a monitored
> website.** A timeout is not an error. A refused connection is not an error. A 503 is not
> an error. They are all *results* — the exact results this product exists to record.

This inverts normal instincts. In most code, a request that times out is an exception you
propagate or handle. Here, an outage is the **payload**. If `perform_check` raised on
timeout, then the moment a monitored site went down — the one moment the product is for —
the check would blow up and record nothing.

Everything else in the file follows from this.

## The four outcomes

Read `app/services/checker.py:43-60` and notice it's a classification, not a happy path
with error handling bolted on:

| Situation | `success` | `status_code` | `error_message` |
|-----------|-----------|---------------|-----------------|
| Got the expected status | `True` | the code | `None` |
| Got a different status | `False` | the code | `"Expected status 200, got 503"` |
| Timed out | `False` | `None` | `"Request timed out"` |
| Connection/DNS/TLS failure | `False` | `None` | `"ConnectError: ..."` |

The two `None` status codes carry real information (chapter 04, decision 1): they're how a
query later distinguishes "the server answered, badly" from "there was no server."

The ordering of the `except` clauses matters:

```python
except httpx.TimeoutException:
    ...
except httpx.HTTPError as exc:
    ...
```
`:46-51`

`TimeoutException` is a *subclass* of `HTTPError`. Python matches `except` clauses top to
bottom, so the specific one must come first — flip them and timeouts get swallowed by the
general handler and lose their dedicated message. A small thing that's easy to break during
a careless refactor.

## Timing is recorded even when it fails

```python
def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
```
`:25-27`

Look where `_elapsed_ms(start)` is called: in *every* return path, including both failure
branches. A timeout still records how long you waited before giving up — roughly the
configured timeout, ~10,000 ms.

> 🐍 **Python note:** `time.perf_counter()` is a monotonic clock meant for measuring
> durations. Unlike wall-clock time, it can't jump backwards when the system clock is
> adjusted or NTP corrects a drift, so a duration computed from it is never negative or
> wildly wrong.

That ~10,000 ms figure on failed checks comes back to bite in chapter 14, and produces one
of the stats endpoint's product decisions. Remember it.

## `CheckOutcome`: separating "what happened" from "storing it"

```python
@dataclass(slots=True)
class CheckOutcome:
    """Result of a single HTTP probe, independent of persistence."""
    status_code: int | None
    response_time_ms: float
    success: bool
    error_message: str | None
```
`:15-22`

> 🐍 **Python note:** `@dataclass` generates the boilerplate (constructor, equality,
> repr) for a class that just holds fields. `slots=True` makes instances slightly smaller
> and faster by skipping the per-instance dictionary.

> 🔑 **Decision: the probe returns a plain object, not a database row.** The docstring says
> it: *"independent of persistence."*

This is what makes the probe logic testable without a database. Look at
`tests/test_checker.py` — most of it calls `perform_check` with a fake transport and asserts
on the returned `CheckOutcome`. No session, no tables, no fixtures beyond an HTTP client.

Compare with the alternative, where `perform_check` writes its own `CheckResult` row: now
every test of "does a 503 count as failure?" needs a database. The seam between *deciding*
and *storing* is one line of design that pays for itself in every test.

## The job body

```python
async def run_monitor_check(monitor_id, session_factory, client) -> None:
    """Scheduler job body: load the monitor, probe it, persist a CheckResult.

    Reads the monitor fresh each run so config edits apply immediately, and
    silently no-ops if the monitor was deleted or deactivated after the job
    fired (delete/reschedule races).
    """
    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        if monitor is None or not monitor.is_active:
            logger.warning("monitor_check_skipped", monitor_id=monitor_id)
            return
```
`:63-78`

Three decisions in six lines.

**It takes a `monitor_id`, not a `Monitor`.** The scheduler stores only an integer with
each job. If it stored the whole object, that object would be a snapshot from whenever the
job was created — and a monitor edited an hour ago would keep being checked with its old
URL.

**It opens its own session.** Chapter 05 established one session per HTTP *request*, but a
scheduled job has no request. So it takes the factory and manages its own, with `async
with` guaranteeing the close. This is exactly why the checker and scheduler live in
`app/services/` and not `app/routers/` — they must work with no request in flight.

**It re-reads the row every run, and no-ops if things changed.** This is race protection.
Consider the sequence: the job for monitor 5 fires; a user deletes monitor 5; the job's
`session.get` returns `None`. Without the guard you'd get an `AttributeError` inside a
background job. With it, a warning is logged and the run ends quietly.

The same guard covers deactivation — and note the belt-and-braces: chapter 08's
`sync_monitor` already removes the job when a monitor is deactivated, so this check is the
*second* line of defense against a job that fires in the gap. There's a test for it
specifically: *"A job may fire after its monitor was deleted — must be a silent no-op."*

## One shared HTTP client

`run_monitor_check` receives a `client` rather than creating one. That client is built once
per process, in the lifespan (`app/main.py:34-36`):

```python
http_client = httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=True)
```

> 🔑 **Decision: one `httpx.AsyncClient` for the entire process.** Creating a client per
> check would throw away connection pooling and TLS session reuse, so every probe would pay
> a fresh TCP and TLS handshake. It would also mean the timeout setting is applied in a
> dozen places instead of one.

`follow_redirects=True` is a product decision hiding in a keyword argument: if a monitored
URL 301s to HTTPS, DevPulse follows it and reports on the destination, rather than
recording a "failure" that isn't one.

## What gets logged

```python
logger.info("monitor_checked", monitor_id=..., url=..., success=..., status_code=..., response_time_ms=...)
```
`:96-103`

Chapter 03's principle in practice. Every check emits a structured record with five fields.
In production these are JSON lines you can aggregate — which means you can answer "what's
the p99 latency for monitor 5" from your logs even though the app has no metrics system.

Note also `logger.warning("monitor_check_skipped", ...)` at `:77`. A skip is unusual enough
to be worth noticing but not an error — warning is exactly the right level, and it's the
breadcrumb you'd follow if a monitor mysteriously stopped producing results.

### 🔍 Try it

1. **Read the tests as a specification of the four outcomes:**

   ```bash
   pytest tests/test_checker.py -v
   ```

   The test names enumerate the table above. Then look at how they fake the network —
   `httpx.MockTransport` takes a plain Python function that returns a response, so a "server
   that times out" is one `raise httpx.TimeoutException(...)`. Chapter 13 goes deeper.

2. **Break the exception ordering and watch a test catch it.** In
   `app/services/checker.py:46-51`, swap the two `except` blocks so `httpx.HTTPError` comes
   first. Run `pytest tests/test_checker.py -v`. Then revert with `git checkout
   app/services/checker.py`. This is the fastest way to convince yourself the ordering
   isn't stylistic.

3. **Follow the data.** Trace one field — `response_time_ms` — from `_elapsed_ms` at `:27`,
   through `CheckOutcome` at `:20`, into the `CheckResult` row at `:90`, and finally into
   the average computed at `app/routers/monitors.py:229`. That's the whole product in one
   value.

---

Previous: [06 — Migrations](06-migrations.md) · Next: [08 — The scheduler](08-scheduler.md)
