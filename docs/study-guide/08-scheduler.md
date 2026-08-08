# 08 — The scheduler

File: `app/services/scheduler.py`, 123 lines, one class. Every method is short and almost
every one contains a guard against a specific failure. This chapter is a tour of those
guards, because they're what separates a scheduler that survives production from one that
quietly dies on a Tuesday.

## The core idea: one job per monitor, addressable by id

```python
@staticmethod
def job_id(monitor_id: int) -> str:
    return f"monitor:{monitor_id}"
```
`app/services/scheduler.py:41-44`

> 🔑 **Decision: job ids are a deterministic function of the monitor id.**

This tiny method is the hinge the whole design turns on. Because the id for monitor 5 is
always `"monitor:5"`, any part of the app can add, replace, remove, or inspect that one
job without keeping a registry of what it scheduled.

The alternative — letting APScheduler assign random job ids and storing a mapping — would
need that mapping kept in sync across creates, updates, deletes and restarts. Deriving the
id instead means there's nothing to keep in sync, and nothing to get out of sync.

The payoff shows up in chapter 11: when a user changes a monitor's interval, the route
adjusts *exactly that job*, rather than tearing down and rebuilding the whole schedule.
With hundreds of monitors, "reload everything on every edit" is the difference between a
change costing microseconds and costing a full scan plus hundreds of re-registrations.

## Three flags that prevent three pileups

```python
self._scheduler.add_job(
    self._run_check,
    trigger=IntervalTrigger(seconds=interval_seconds),
    args=[monitor_id],
    id=self.job_id(monitor_id),
    replace_existing=True,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=MISFIRE_GRACE_SECONDS,
)
```
`app/services/scheduler.py:73-82`

**`max_instances=1`** — a monitor on a 30-second interval pointing at a site that takes 45
seconds to time out would, by default, start a second check while the first is still
running, then a third. Within minutes you'd have dozens of overlapping requests to a site
that's already struggling. This caps it at one run of any given job at a time. The class
docstring states the intent: *"a slow check can never overlap with itself."*

**`coalesce=True`** — if the app was paused (host suspended, event loop blocked) and a job
missed twelve firings, APScheduler's default is to run it twelve times immediately.
`coalesce` collapses that backlog into a single run. The result is that recovering from a
stall doesn't produce a burst of traffic — *"a backlog collapses into a single run."*

**`misfire_grace_time=30`** — a job that fires late is normally discarded. Thirty seconds
of grace means a slightly-late check still runs. The constant is defined at module level
with its reasoning attached (`:19-21`): *"Late checks are still run if within this many
seconds of their slot, instead of being discarded (e.g. after event-loop stalls)."*

Together these encode a coherent stance: **prefer one correct check to zero checks or ten
checks.**

**`replace_existing=True`** deserves its own note. It makes `schedule_monitor` an
*upsert* — add if absent, replace if present. That's why the docstring reads *"Add — or
replace, if it already exists"* (`:72`), and why there's no separate `reschedule_monitor`
method. Create and update are the same code path, so they can't drift apart.

## `sync_monitor`: converge, don't command

```python
def sync_monitor(self, monitor_id: int, *, is_active: bool, interval_seconds: int) -> None:
    """Converge one monitor's job to its desired state (API create/update)."""
    if is_active:
        self.schedule_monitor(monitor_id, interval_seconds)
    else:
        self.remove_monitor(monitor_id)
```
`:97-104`

> 🔑 **Decision: the API describes the desired end state; the scheduler figures out the
> steps.**

Callers don't say "add this job" or "remove that job." They say "monitor 5 should be active
at 300 seconds" and this method makes reality match. That's why `app/routers/monitors.py`
calls `sync_monitor` identically after create *and* after update (`:92-96` and `:154-158`)
— the route doesn't have to reason about whether a job already exists, whether the monitor
was just deactivated, or whether the interval changed.

Note the keyword-only `*` (chapter 02): you cannot call this with the booleans in the wrong
order, because you must name them.

## Guard: removing a job that isn't there is not an error

```python
def remove_monitor(self, monitor_id: int) -> None:
    """Remove a monitor's job; a missing job is not an error."""
    try:
        self._scheduler.remove_job(self.job_id(monitor_id))
        logger.info("monitor_job_removed", monitor_id=monitor_id)
    except JobLookupError:
        logger.debug("monitor_job_already_absent", monitor_id=monitor_id)
```
`:89-95`

APScheduler raises `JobLookupError` when asked to remove a job it doesn't have. That's a
perfectly normal situation here: deleting an *inactive* monitor means removing a job that
was never scheduled.

Swallowing it makes the operation **idempotent** — calling it twice, or on something
already gone, is harmless. The alternative would push a `try/except` into every caller, or
worse, a "does this job exist?" check that's racy by construction.

Note the log levels: a real removal is `info`, a no-op removal is `debug`. The unusual case
is still recorded, just below the normal noise floor.

## Guard: one broken check can't kill the loop

```python
async def _run_check(self, monitor_id: int) -> None:
    """Job entrypoint: one check run, guaranteed never to raise.

    HTTP-level failures are already recorded by ``run_monitor_check``;
    this guard catches infrastructure errors (e.g. DB unreachable) so an
    individual failure can never kill the scheduler loop.
    """
    try:
        await run_monitor_check(monitor_id, self._session_factory, self._http_client)
    except Exception:
        logger.exception("monitor_check_errored", monitor_id=monitor_id)
```
`:110-122`

This is the most important guard in the file, and the docstring explains the division of
labor precisely.

Chapter 07 established that `perform_check` never raises for *HTTP* problems. But other
things can still fail: the database can be unreachable, a disk can fill, a bug can throw.
Without this wrapper, an exception escaping into APScheduler's job runner could take down
the job — and with it, that monitor stops being checked, silently, until someone restarts
the app.

A blanket `except Exception` is usually a smell. Here it's correct, and it's correct
because of what it does with the exception: `logger.exception` records the full traceback,
so nothing is hidden. The rule it embodies is **one monitor's failure must never affect
another monitor's checks.**

There's a test that deliberately hands the scheduler a broken session factory and asserts
the job wrapper doesn't propagate — *"Even a broken session factory must not propagate out
of the job wrapper."*

## Startup, and the poison-row guard

```python
async def start(self) -> None:
    async with self._session_factory() as session:
        result = await session.execute(select(Monitor).where(Monitor.is_active.is_(True)))
        monitors = result.scalars().all()
    for monitor in monitors:
        try:
            self.schedule_monitor(monitor.id, monitor.interval_seconds)
        except Exception:
            logger.exception("monitor_schedule_failed_on_startup", monitor_id=monitor.id)
    self._scheduler.start()
```
`:46-64`

On boot, load every active monitor and schedule it. Simple — with one addition that wasn't
in the original design.

Notice the `try/except` **inside the loop**. That is not decorative; it's the scar from a
real bug. If scheduling one monitor throws, that monitor is logged and skipped, and every
*other* monitor still gets scheduled and the app still starts.

The full story is war story C1 in [chapter 11](11-monitors-crud.md#war-story-c1) — a single
bad row could otherwise make the application unbootable, forever. Read the guard now, and
the reason for it there.

Also note the session is opened, read, and closed *before* the loop runs. Holding a
database session open across the scheduling loop would be pointless connection occupancy.

`start()` must be called with a running event loop, which is why it lives in the lifespan
(chapter 12) rather than at import time.

## Shutdown

```python
async def shutdown(self) -> None:
    """Stop the scheduler without waiting for in-flight jobs."""
    self._scheduler.shutdown(wait=False)
```
`:66-69`

`wait=False` means: stop scheduling and return immediately, don't block until running
checks finish. Since a check can take up to the configured 10-second timeout, waiting could
stall shutdown long enough for a deployment platform to send SIGKILL. Any interrupted check
simply doesn't write a row, and the next scheduled run picks things up. Nothing is
corrupted by abandoning a probe.

## UTC, everywhere

```python
self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
```
`:39`

The scheduler's own timezone, matching the timestamps in chapter 04 and the log timestamps
in chapter 03. `AsyncIOScheduler` specifically (not `BackgroundScheduler`) because jobs run
as coroutines on the app's existing event loop — no threads, no separate loop, and
`await` works inside a job exactly as it does inside a route.

### 🔍 Try it

1. **Watch job management without any job ever firing:**

   ```bash
   pytest tests/test_scheduler.py -v
   ```

   These tests run in milliseconds even though the shortest interval is 60 seconds. They
   assert on *registration* — is a job present, what's its interval — never on waiting for
   one to fire. Time-dependent tests are slow and flaky; this suite sidesteps both by
   testing the thing it actually controls.

2. **Prove the upsert is really an upsert:**

   ```bash
   pytest tests/test_scheduler.py -v -k reschedul
   ```

   Then read `schedule_monitor`. Same method, called twice, one job — that's
   `replace_existing=True`.

3. **Find the guard that's a scar.** Run `git log --oneline -- app/services/scheduler.py`
   and then `git show 7283069 -- app/services/scheduler.py`. That diff is the `try/except`
   inside `start()` being added, and its commit message says why. Chapter 11 tells the
   story.

---

Previous: [07 — The checker](07-checker.md) · Next: [09 — Schemas: the API boundary](09-schemas-api-boundary.md)
