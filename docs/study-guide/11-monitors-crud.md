# 11 — Monitors CRUD

File: `app/routers/monitors.py`. The biggest file in the app, and the one where policy
lives. It also contains the scars from two real bugs — the most instructive pages in this
guide.

## The ownership helper

```python
async def _get_owned_monitor(session, monitor_id: int, user_id: int) -> Monitor:
    """Load a monitor owned by the user, or 404 (never leaks others' ids)."""
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return monitor
```
`app/routers/monitors.py:55-64`

> 🔑 **Decision: a monitor you don't own returns 404, not 403.**

403 Forbidden means "this exists and you may not have it." That is an information leak: an
attacker walking `/monitors/1`, `/monitors/2`, `/monitors/3` learns exactly which IDs exist
and how many monitors the system holds. The 404 makes "doesn't exist" and "isn't yours"
indistinguishable.

Notice the single condition doing both jobs: `monitor is None or monitor.user_id !=
user_id`. Two very different situations, one identical response — that's what makes them
indistinguishable from outside.

Every single-monitor endpoint calls this first (`:122`, `:135`, `:178`, `:193`, `:213`).
Ownership isn't re-implemented per route, so it can't be forgotten in one.

## Quota rules and their three subtleties

```python
async def _count_active_monitors(session, user_id, *, exclude_id=None) -> int:
    stmt = select(func.count()).select_from(Monitor).where(
        Monitor.user_id == user_id, Monitor.is_active.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(Monitor.id != exclude_id)
    return (await session.execute(stmt)).scalar_one()
```
`app/routers/monitors.py:41-52`

**It counts in SQL.** `select(func.count())` returns a number from the database. Loading
the user's monitors and calling `len()` would work identically at 20 rows and badly at
20,000. Same principle as chapter 04's cascade and chapter 14's aggregation.

**Only active monitors count.** A paused monitor generates no traffic, so it shouldn't
consume a slot. The quota exists to cap outbound load, and the rule matches that intent.

**`exclude_id` exists because of a subtle bug class.** When you PATCH an already-active
monitor, it's *already* in the count. Without excluding it, a user at exactly the limit
could never edit any monitor again — every PATCH would 409 against their own existing
monitor. The test name says it: *"An active monitor holds a quota slot; re-PATCHing it must
not 409"* (`tests/test_monitors_api.py:177-189`).

The status codes: quota exceeded is **409 Conflict** (`:81`) — a state conflict, retry
later after deleting something. An interval below the configured floor is **422** (`:34`) —
malformed input, retrying won't help. The distinction tells a client whether to change the
request or change the world.

## Every write syncs exactly one job

```python
scheduler.sync_monitor(monitor.id, is_active=monitor.is_active, interval_seconds=monitor.interval_seconds)
```

Appears after create (`:99-103`), after update (`:161-165`), and as `remove_monitor` after
delete (`:181`). Chapter 08 explained the machinery; here's the discipline: **the database
and the scheduler are updated in the same request, for the same monitor.** No reconciliation
loop, no "reload all jobs," no eventual consistency.

The ordering is deliberate — commit first, then sync. If the commit fails, no job is
touched. The reverse order could leave a job scheduled for a monitor that was never saved.

## <a id="war-story-c1"></a>💥 War story C1: the interval that bricked startup

**Commit `7283069`.**

**What broke.** `interval_seconds` had a lower bound but no upper bound. A single request —

```json
POST /monitors  {"url": "https://example.com", "interval_seconds": 100000000000000}
```

— passed validation and was written to the database. Then APScheduler's `IntervalTrigger`
tried to turn that into a timedelta and raised `OverflowError`.

**Why it was catastrophic rather than annoying.** The failing request was the small part.
Chapter 08 showed that `MonitorScheduler.start()` loads *every active monitor* on boot. So
that one row now crashed startup. Not the request — **startup**. The application could not
boot again, and every other monitor stopped being checked, permanently, until someone
manually found and deleted the row.

**Why the gap existed.** Validation was designed against the danger everyone anticipated:
intervals that were too *small*, hammering a target site. There was a policy floor
(`min_interval_seconds`) and a schema floor (`ge=1`). Nobody asked what an absurdly *large*
value would do, because a slow check sounds harmless. The failure wasn't in the check — it
was in arithmetic on the way to scheduling it.

**The fix, at two layers.**

```python
interval_seconds: int = Field(ge=1, le=31_536_000, ...)
```
`app/schemas.py:17-21`, and the same bound on `MonitorUpdate` at `:36`. One year maximum.
This stops the bad value at the door — on POST *and* PATCH, because fixing only one leaves
the hole open.

```python
for monitor in monitors:
    try:
        self.schedule_monitor(monitor.id, monitor.interval_seconds)
    except Exception:
        logger.exception("monitor_schedule_failed_on_startup", monitor_id=monitor.id)
```
`app/services/scheduler.py:56-62`. Now a poison row is logged and skipped, and every other
monitor still gets scheduled.

> **The lesson: input validation is not the only defense.** The second layer assumes the
> first one failed. Bad data can arrive from a migration, a manual `UPDATE`, an older
> version of the code, or a bug you haven't found yet — and when it does, the system must
> degrade to "one monitor is broken," never to "nothing boots."
>
> The general form: **a single bad row must never be able to take down the whole system.**
> Ask it of any startup path that loads everything.

Two regression tests were added: one rejects the huge interval with a 422 before it can
reach the database (`tests/test_monitors_api.py:60-69`), and one inserts a poison row
*directly* and asserts startup survives it and still schedules the healthy monitors. That
second test is the interesting one — it tests the recovery path, which only exists because
someone assumed the first defense would fail.

## <a id="war-story-c2"></a>💥 War story C2: the null that corrupted a row

**Same commit, `7283069`.**

**What broke.** `PATCH /monitors/5` with body `{"url": null}` permanently corrupted monitor
5. Every subsequent `GET` of it returned a 500. The monitor became unreadable and unfixable
through the API.

**The mechanism**, in three innocent steps:

1. `MonitorUpdate.url` is `HttpUrl | None = None` — every field optional, because PATCH is
   partial (chapter 09).
2. The route dumped changes with `model_dump(exclude_unset=True)`. That flag drops fields
   the client *never mentioned*. But `{"url": null}` **was** mentioned — the client
   explicitly set it — so `url=None` survived into the update dictionary as a real value.
3. The route then coerced it for the string column:
   ```python
   updates["url"] = str(updates["url"])
   ```
   And `str(None)` in Python is the four-character string `"None"`.

So `monitors.url` became the literal text `"None"`. On the next read, `MonitorRead`
validated it as an `HttpUrl`, failed, and returned a 500. Data written by a *successful*
200 response made the resource permanently unreadable.

**The fix.**

```python
updates = payload.model_dump(exclude_unset=True, exclude_none=True)
```
`app/routers/monitors.py:136`

`exclude_none=True` drops explicit nulls too, so `{"url": null}` is treated exactly like
omitting the field — which is what a client sending it almost certainly meant.

The regression test spells out the semantics, and it checks the thing that actually hurt —
that the monitor is still *readable* afterward:

```python
async def test_patch_explicit_null_is_ignored_not_applied(client, auth_headers) -> None:
    """Explicit JSON null must be treated like an omitted field, not a value
    to set — str(None) previously corrupted the url column and broke every
    subsequent read of the monitor."""
```
`tests/test_monitors_api.py:159-174`

> **The lessons.** First: `exclude_unset` and `exclude_none` sound like synonyms and aren't
> — "absent," "null," and "a value" are three states, and PATCH semantics live or die on
> telling them apart.
>
> Second, and more general: **a type coercion at a layer boundary is a place where `None`
> goes to hide.** `str(x)` never fails. It happily converts `None`, `[]`, or an arbitrary
> object into text your database will accept and your reader will choke on. Anywhere you
> convert types between layers — here, `HttpUrl` → `str` — check what happens when the
> value is missing.
>
> Third: the damage was *asymmetric*. The write succeeded with a 200. The failure surfaced
> later, on an unrelated read, in a different endpoint. Bugs that corrupt state are worse
> than bugs that crash, because the crash tells you where to look.

## Results: bounded by construction

```python
async def list_results(monitor_id, user: CurrentUser, session: DbSession,
                       limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[CheckResult]:
    await _get_owned_monitor(session, monitor_id, user.id)
    result = await session.execute(
        select(CheckResult).where(CheckResult.monitor_id == monitor_id)
        .order_by(CheckResult.checked_at.desc(), CheckResult.id.desc()).limit(limit))
```
`app/routers/monitors.py:185-200`

Three things worth copying. The `limit` is **capped at 500 in the type** — a client cannot
ask for a million rows, so there's no request that can exhaust memory. The default of 50 is
what you want when you didn't think about it. And the sort is on `(checked_at DESC, id
DESC)`: the second key breaks ties, so two checks recorded in the same instant come back in
a stable, repeatable order rather than whatever the database felt like.

That tie-breaker reappears in chapter 14, where a stable order isn't just tidy — it changes
the answer.

### 🔍 Try it

1. **Read both war stories in the record:**

   ```bash
   git show 7283069
   ```

   One commit, both bugs, and a message that explains them. Notice the diff touches
   `app/schemas.py`, `app/routers/monitors.py`, `app/services/scheduler.py`, and the tests —
   four files for two bugs, because each fix has two layers.

2. **Reproduce C2 in 30 seconds:**

   ```bash
   pytest tests/test_monitors_api.py -k explicit_null -v      # passes
   ```
   Now remove `exclude_none=True` from `app/routers/monitors.py:136` and run it again. Read
   the failure carefully — it fails on the *re-fetch*, not the PATCH. Restore with
   `git checkout app/routers/monitors.py`.

3. **Confirm the enumeration defense.** Run
   `pytest tests/test_monitors_api.py -k other_users -v`, then ask yourself what an attacker
   learns from the response. Nothing — which is the point.

4. **Find the quota subtlety yourself.** Comment out the `exclude_id` argument at
   `app/routers/monitors.py:145-146` and run
   `pytest tests/test_monitors_api.py -k reactivating -v`.

---

Previous: [10 — Auth & users](10-auth-and-users.md) · Next: [12 — Lifespan: assembly](12-lifespan-assembly.md)
