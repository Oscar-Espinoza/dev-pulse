# 14 — Case study: the stats endpoint

`GET /monitors/{id}/stats?hours=N` — uptime percentage, average response time, incident
count. Code at `app/routers/monitors.py:203-261`; tests at
`tests/test_monitors_api.py:255-412`.

This is the newest feature and the one worth studying hardest, because it's small enough to
hold in your head and every part of it was an argued decision. We'll go decisions first,
then the SQL, then the traps.

## Four product decisions before a line of code

The interesting fights in this feature were never about SQL. They were about what the
numbers *mean*.

### 1. An incident is an outage, not a failed check

If a site is down for ten minutes and you check every minute, you record ten failures. Is
that ten incidents or one?

> 🔑 **Decision: one.** An incident is a *run of consecutive failures*. Ten failures in a
> row is one outage. The recorded phrasing: *"A run of consecutive `success=false` checks
> counts once."*

"Failed check count" is a number you already have — it's `total_checks × (1 - uptime)`.
Reporting it as "incidents" would just be uptime in a costume. What an operator actually
wants to know is *how many times did this thing break*, because ten separate one-minute
outages and one ten-minute outage are very different problems with the same uptime
percentage.

This is the decision that makes the query non-trivial, and everything below follows from
it.

### 2. Average latency counts successful checks only

> 🔑 **Decision: exclude failures from the average.** Recorded reason: *"timeouts record
> ~10,000 ms and would skew it."*

Chapter 07 established that a timed-out check still records how long it waited — about the
full 10-second timeout. Include those in the mean and a monitor with 95% uptime reports an
"average response time" of ~700 ms when the site is actually serving in 200 ms. The number
becomes a blend of two unrelated things: how fast the site is, and how often it's down.
You already have a metric for the second one.

So the average answers "when it works, how fast is it?" — and is `null` when there were no
successes at all, because the honest answer is "we don't know."

### 3. Empty windows are `null`, not zero

> 🔑 **Decision: `uptime_percent` is `null` when the window contains no checks.**

A brand-new monitor has no data. Reporting `0.0` would say "it was down the whole time,"
which is false and alarming. Reporting `100.0` would say "perfect," equally false.
`null` means "no data," and a UI can render that as "—" instead of a red zero.

This is one of the few places where "no answer" is the right answer, and returning it
honestly is worth the extra branch in the code (`app/routers/monitors.py:254`).

### 4. An outage that started before the window still counts once

The subtle one. You ask for the last 24 hours; the site went down 30 hours ago and is still
down. Do the failures inside your window count as an incident?

> 🔑 **Decision: yes, once.** A run cut by the window's left edge counts as one incident
> inside the window.

There's no perfect answer, which is exactly why it needed to be decided explicitly rather
than falling out of whatever the code happened to do. It's documented in the route's
docstring (`:210-212`) and pinned by a test named
`test_stats_episode_straddling_window_edge_counts_once` (`tests/test_monitors_api.py:361`).

**That pattern is the transferable lesson.** When behavior at an edge is arguable, pick
deliberately, write it in the docstring, and pin it with a test whose name states the rule.
Otherwise the next person "fixes" it into the other behavior and nobody knows which was
intended.

## The engineering constraint: one SQL statement

> 🔑 **Decision: all three metrics come from a single aggregation query. No rows are loaded
> into Python.** Stated as a hard constraint: *"computed efficiently with a single SQL
> aggregation query (not loading all rows into Python)."*

The obvious implementation is a loop:

```python
results = await session.execute(select(CheckResult).where(...))   # DON'T
rows = results.scalars().all()
uptime = sum(r.success for r in rows) / len(rows) * 100
```

Correct, readable, and wrong. A monitor checked every 60 seconds produces 43,200 rows a
month. That code ships all of them over the network, allocates 43,200 Python objects, and
throws them away to produce three numbers — per request. The database can produce those
three numbers by scanning an index and returning **one row**.

Same principle as chapter 04's `passive_deletes` and chapter 11's `func.count()`. It shows
up a third time here because it's the single most common performance mistake with an ORM:
using it as a row-fetcher when you should be asking the database a question.

## The SQL, in two steps

If you're new to SQL, read this section slowly; it's the densest page in the guide.

### Step 1: a CTE that adds "what happened last time"

> A **CTE** (Common Table Expression, the `WITH` clause) is a named temporary result you
> can query as if it were a table. Think of it as a local variable for a query.

```python
windowed = (
    select(
        CheckResult.success,
        CheckResult.response_time_ms,
        func.lag(CheckResult.success)
        .over(order_by=(CheckResult.checked_at, CheckResult.id))
        .label("prev_success"),
    )
    .where(CheckResult.monitor_id == monitor_id, CheckResult.checked_at >= cutoff)
    .cte("windowed")
)
```
`app/routers/monitors.py:215-225`

This produces a temporary table of the checks in your window, with **one extra column**.

`func.lag(X).over(order_by=...)` is a **window function**. It computes, for each row, the
value of `X` in the *previous* row according to that ordering. So `prev_success` on any row
tells you whether the check before it succeeded:

| checked_at | success | prev_success |
|-----------|---------|--------------|
| 10:00 | true | `NULL` ← no previous row |
| 10:01 | false | true |
| 10:02 | false | false |
| 10:03 | true | false |
| 10:04 | false | true |

Why this shape? Because now **"an incident started here" is a property of a single row**:
this check failed, and the one before it didn't. No loop, no state carried between rows —
which is precisely what lets the whole thing be one aggregate query.

Two details that look small and aren't:

- **`order_by=(checked_at, id)`** — the `id` is a tie-breaker. Two checks recorded in the
  same instant would otherwise have an undefined order, and "which one came first" changes
  whether a row starts an incident. Same tie-breaker as chapter 11's `list_results`, but
  here it can change the *answer*, not just the presentation.
- **The `WHERE` is inside the CTE.** So `LAG` only ever sees rows within the window — which
  is exactly what implements product decision 4. The first in-window row has
  `prev_success = NULL` regardless of what happened before the cutoff, so a straddling
  outage registers as one incident. That behavior isn't a happy accident; it's why the
  filter sits where it does.

### Step 2: four aggregates over that table

```python
stmt = select(
    func.count().label("total_checks"),
    func.sum(case((windowed.c.success, 1), else_=0)).label("successful_checks"),
    func.avg(case((windowed.c.success, windowed.c.response_time_ms))).label("avg_response_time_ms"),
    func.sum(case((and_(~windowed.c.success,
                        or_(windowed.c.prev_success.is_(None),
                            windowed.c.prev_success.is_(True))),
                   1), else_=0)).label("incident_count"),
).select_from(windowed)
row = (await session.execute(stmt)).one()
```
`app/routers/monitors.py:226-248`

No `GROUP BY`, so this collapses the whole CTE to exactly one row. Four numbers:

**`total_checks`** — `COUNT(*)`.

**`successful_checks`** — `SUM(CASE WHEN success THEN 1 ELSE 0 END)`. Counting by summing
ones is the standard portable trick for "count rows matching a condition."

**`avg_response_time_ms`** — the elegant one:

```python
func.avg(case((windowed.c.success, windowed.c.response_time_ms)))
```

`case` with no `else_` yields `NULL` for failures, and **SQL's `AVG` skips `NULL`s**. So
this is the mean over successful checks only — product decision 2 — expressed as an absence
rather than a filter. The plan notes it explicitly: *"`case((cond, value))` with no `else_`
yields NULL, which `avg()` skips — that's how 'successful checks only' works."*

**`incident_count`** — count the rows that *start* an outage:

```
this check failed          →  ~windowed.c.success
   AND
( no previous check         →  prev_success IS NULL
  OR previous check passed ) →  prev_success IS true
```

That's exactly the "an incident started here" property from step 1. Walk it against the
table above: 10:01 (failed, previous passed) ✓, 10:02 (failed, previous also failed) ✗ —
same outage, 10:04 (failed, previous passed) ✓. Two incidents. That's the
`S F F S F → 2` case, pinned at `tests/test_monitors_api.py:289`.

> 🐍 **Python note:** `~` is "not" and `&`/`|` are "and"/"or" in SQLAlchemy expressions —
> Python's `not`/`and`/`or` keywords can't be overloaded, so the operators do the work.
> `and_()` and `or_()` are the readable equivalents used here.

### Step 3: the Python that assembles the answer

```python
total = row.total_checks
return MonitorStats(
    ...,
    uptime_percent=round(row.successful_checks / total * 100, 2) if total else None,
    avg_response_time_ms=(round(row.avg_response_time_ms, 2)
                          if row.avg_response_time_ms is not None else None),
    incident_count=row.incident_count or 0,
)
```
`app/routers/monitors.py:249-261`

Three guards, each for a real case:

- `if total` — an empty window: avoids dividing by zero, and returns `null` per decision 3.
- `is not None` on the average — no successes in the window, so `AVG` returned `NULL`.
- `or 0` on incidents — `SUM` over **zero rows returns `NULL`, not 0**. This surprises
  nearly everyone. Without the `or 0` an empty window would report `incident_count: null`.

Note the deliberate difference between `if total` (truthiness) and `is not None` — chapter
02's point about `None` versus falsy, in the wild. `0` is a legitimate average; `None`
isn't a legitimate anything.

## The cross-backend traps

Production is Postgres; the tests are SQLite (chapter 13). One statement must be correct on
both.

**Booleans.** SQLite has no boolean type — it stores 0 and 1. The same SQLAlchemy
expression compiles differently for each dialect: `NOT windowed.success` on Postgres,
`windowed.success = 0` on SQLite; `prev_success IS true` versus `prev_success IS 1`. Both
correct. This is why the code says `.is_(True)` rather than a bare Python `if` — it lets
SQLAlchemy emit the right thing per dialect.

**`LAG` availability.** Window functions need SQLite ≥ 3.25 (2018). Fine everywhere modern,
but worth knowing that this query has a floor which plain aggregates don't.

**Timezones — the real trap.** Chapter 04 made every timestamp timezone-aware. SQLite has
no timezone-aware type; it stores a formatted string. The `cutoff` here is an aware UTC
datetime, and it's compared against those stored strings.

It works — because both sides serialize through the same SQLAlchemy formatting, so the
comparison is lexicographic on identically-shaped strings. But it works *conditionally*,
which is why the test-seeding rule is written down as a constraint: *"always build
`checked_at` values from `utcnow()` minus `timedelta`s — never naive/local datetimes."*

Seed one naive datetime in a test and you'd get a differently-formatted string, a comparison
that silently misbehaves on SQLite but not Postgres, and a test that's either flaky or
wrong. `_seed_checks` at `tests/test_monitors_api.py:235` follows the rule throughout.

## How the tests pin each decision

Nine tests (`tests/test_monitors_api.py:255-412`), and the mapping is one-to-one with the
decisions:

| Test | Pins |
|------|------|
| `..._requires_api_key` (`:255`) | Auth applies here too |
| `..._other_users_monitor_is_404` (`:260`) | Ownership, chapter 11's 404 rule |
| `..._empty_window` (`:275`) | Decision 3 — asserts the whole body, `null`s and all |
| `..._counts_incident_episodes_not_failed_checks` (`:289`) | Decision 1 — `S F F S F → 2` |
| `..._excludes_checks_outside_window` (`:315`) | The cutoff, *and* the SQLite timezone trap |
| `..._all_failures_is_one_incident` (`:339`) | Decision 1 + `null` average |
| `..._episode_straddling_window_edge_counts_once` (`:361`) | Decision 4 |
| `..._ignores_other_monitors_results` (`:386`) | The CTE's `monitor_id` filter |
| `..._hours_bounds_validated` (`:406`) | `Annotated[int, Query(ge=1, le=8760)]` |

Notice there is no test for "the SQL is one statement." That's a design property, not a
behavior, and it's enforced by review rather than by assertion. Worth knowing what your
tests can and can't protect.

## The workflow that produced it

Two process details are as instructive as the code.

**The design was validated before the plan was finished.** The exact statement was executed
against in-memory SQLite during planning to answer questions the documentation didn't:
does an aggregate over an *empty* CTE return one row or zero rows? (One — so `.one()` is
safe.) Does `SUM` over zero rows return 0 or NULL? (NULL — hence `or 0`.) Ten minutes of
experiment removed two guesses from the implementation.

**Route ordering was checked, not assumed.** A reasonable worry: could `/monitors/{id}`
swallow `/monitors/5/stats`? The plan records the answer — path parameters never span `/`,
so no. The worry was legitimate; resolving it in writing meant nobody had to re-derive it.

## What was deliberately left out

No caching. No percentiles (p95/p99 would need `percentile_cont`, which SQLite lacks —
it would break the test strategy). No time-bucketed series for charts. No composite index,
even though this query would benefit from one — chapter 15 explains why that was left as a
follow-up rather than smuggled into a feature branch.

Each of those is a real thing someone might want. None of them was needed to answer the
question asked, and every one would have been permanent surface area.

### 🔍 Try it

1. **See the SQL for both backends:**

   ```bash
   python -c "
   from sqlalchemy import select, func, case, and_, or_
   from sqlalchemy.dialects import postgresql, sqlite
   from app.models import CheckResult
   w = (select(CheckResult.success, CheckResult.response_time_ms,
               func.lag(CheckResult.success).over(
                   order_by=(CheckResult.checked_at, CheckResult.id)).label('prev_success'))
        .where(CheckResult.monitor_id == 1).cte('windowed'))
   s = select(func.count(), func.avg(case((w.c.success, w.c.response_time_ms)))).select_from(w)
   print(s.compile(dialect=postgresql.dialect()))
   print(s.compile(dialect=sqlite.dialect()))
   "
   ```

   Compare the two. Same Python, different SQL — that's the portability layer earning its
   keep.

2. **Break the incident rule and watch the right test fail.** In
   `app/routers/monitors.py:232-246`, delete the `and_(...)` wrapper so every failed check
   counts. Run `pytest tests/test_monitors_api.py -k stats -v` — only the two
   episode-counting tests should fail. Restore with `git checkout app/routers/monitors.py`.

3. **Prove `SUM` over nothing is `NULL`:**

   ```bash
   python -c "
   import sqlite3
   c = sqlite3.connect(':memory:'); c.execute('create table t(x int)')
   print(c.execute('select sum(x), count(*) from t').fetchone())
   "
   ```

   `(None, 0)`. That's the `or 0` at `app/routers/monitors.py:260`, justified.

4. **Write the tenth test.** What happens with a single check in the window? Predict the
   four numbers first, then write the test and check yourself.

---

Previous: [13 — Testing philosophy](13-testing-philosophy.md) · Next: [15 — What wasn't built](15-what-was-not-built.md)
