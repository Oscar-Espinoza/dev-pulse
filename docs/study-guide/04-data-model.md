# 04 — The data model

File: `app/models.py`. 112 lines that define the entire persistent state of the
application. This chapter is mostly about six decisions hiding in those lines.

## How a SQLAlchemy 2.0 model reads

```python
class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    is_active: Mapped[bool] = mapped_column(default=True)
```

`app/models.py:66-80`. The Python type on the left defines the column's type and
nullability; `mapped_column(...)` on the right adds database-level details. `Mapped[int]`
is a non-null integer; `Mapped[int | None]` (as at `:103`) is a nullable one. The type hint
isn't a comment — it *is* the schema.

## The three tables

**`User`** (`:47-63`) — email, API key, creation time. Notice what's missing: no password,
no name, no verification status. Chapter 10 covers the auth decision that makes this
enough.

**`Monitor`** (`:66-91`) — the check definition. Owned by a user via `user_id`.

**`CheckResult`** (`:94-111`) — one row per probe. This is the append-only fact table, and
it's the one that grows: a single monitor on a 60-second interval writes 1,440 rows a day.
Everything in chapter 14 is about querying this table without drowning.

## Decision 1: an outage is a *row*, not a missing row

Look carefully at `CheckResult`:

```python
status_code: Mapped[int | None] = mapped_column(default=None)
response_time_ms: Mapped[float] = mapped_column()
success: Mapped[bool] = mapped_column()
error_message: Mapped[str | None] = mapped_column(Text(), default=None)
```

`app/models.py:103-109`.

> 🔑 **Decision: failures are recorded as data, with the same shape as successes.** A
> timeout produces a row with `success=False`, `status_code=None`, `error_message="Request
> timed out"`, and — importantly — a real `response_time_ms` measuring how long you waited
> before giving up.

Two columns are nullable, and each nullability is a considered choice. `status_code` is
`None` when there *was* no HTTP response at all (timeout, DNS failure, connection refused)
— the distinction between "the server said 503" and "there was no server" is preserved.
`error_message` is `None` on success because there's nothing to say.

The alternative design — logging an error and writing nothing — would make the outage
invisible to every query. You could never compute uptime, because downtime would be a gap
in the data rather than data. Chapter 07 shows the code that enforces this, and chapter 14
shows the payoff.

## Decision 2: cascade deletes happen in the database

```python
monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
...
check_results: Mapped[list[CheckResult]] = relationship(
    back_populates="monitor", cascade="all, delete-orphan", passive_deletes=True
)
```

`app/models.py:100-101` and `:89-91`.

There are three things there and they work together:

- `ondelete="CASCADE"` is a *database* instruction: when a monitor row is deleted, Postgres
  deletes its check results itself.
- `cascade="all, delete-orphan"` is the *ORM* instruction for the same relationship.
- `passive_deletes=True` tells the ORM: **don't do it yourself, the database will handle
  it.**

> 🔑 **Decision: `passive_deletes=True`.** Without it, deleting a monitor with 50,000 check
> results makes SQLAlchemy load all 50,000 rows into Python memory just to issue 50,000
> individual DELETE statements. With it, one `DELETE FROM monitors WHERE id = 5` goes to
> the database and the cascade happens there.

This is the same principle as chapter 14's stats query, showing up in a different place:
**work that the database can do should happen in the database.** Ten thousand rows crossing
the network to be counted or deleted in Python is almost always a design mistake.

## Decision 3: `JSONVariant` — one line that enables the whole test suite

```python
JSONVariant = JSON().with_variant(JSONB(), "postgresql")
```

`app/models.py:28`, with the comment above it: *"JSONB on Postgres, plain JSON elsewhere
(sqlite in tests)."*

`Monitor.headers` stores a dictionary of custom HTTP headers. Postgres has a purpose-built
binary JSON type, `JSONB`, which is what you want in production. SQLite has no such thing.

If the model said `JSONB` outright, the models would be unusable on SQLite — and the entire
test suite runs on SQLite. If it said `JSON` outright, production would give up JSONB's
indexing and efficiency.

`with_variant` says "use JSONB when the dialect is postgresql, plain JSON otherwise." One
line, and the *exact same model classes* work against both backends. Every test in `tests/`
depends on this line existing.

Hold onto this idea — the tension between "best type on Postgres" and "must also run on
SQLite" is a recurring theme, and chapter 14 hits it much harder.

## Decision 4: timestamps are timezone-aware, always

```python
def utcnow() -> datetime:
    """Timezone-aware current UTC time (SQLAlchemy python-side default)."""
    return datetime.now(timezone.utc)
```

`app/models.py:31-33`, used as `default=utcnow` on every timestamp column.

Two details:

**Aware, not naive.** Python lets you have a `datetime` with no timezone attached — a
"naive" datetime. They are a persistent source of bugs, because two naive datetimes from
different sources may be in different timezones with nothing recording that fact. Every
timestamp created here carries `timezone.utc` explicitly, and every column is
`DateTime(timezone=True)`.

**Python-side, not database-side.** The default could have been the database's `now()`.
Generating it in Python means the value exists on the object *before* the insert, so code
and tests can control it — which is exactly what chapter 14's tests do when they seed check
results at specific times in the past.

There's a subtlety this creates on SQLite (which has no real timezone-aware type), and it's
one of the traps chapter 14 has to navigate. Note the choice now; the consequence lands
there.

## Decision 5: deterministic constraint names

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    ...
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

`app/models.py:19-25` and `:41-44`.

Left alone, databases auto-generate names for indexes, unique constraints and foreign keys
— and different databases generate different ones. That's fine until you need a migration
that *drops* a constraint, at which point you need to know its name, and the name in
production may not match the name in your development database.

This convention makes every constraint name a pure function of the table and column. It
costs six lines at the top of the file and it makes chapter 06's migrations predictable and
reversible. It's the kind of decision that looks like fussiness right up until the first
time you need it.

## Decision 6: indexes on exactly the columns that get queried

```python
monitor_id: ... index=True     # :101
checked_at: ... index=True     # :107
```

An index makes lookups fast and writes slightly slower, so you index what you actually
query. Both of these are the columns every history and statistics query filters or sorts
by — `WHERE monitor_id = ?` and `ORDER BY checked_at`, at `app/routers/monitors.py:196-197`
and `:223`.

Note that these are two *separate* single-column indexes, not one composite
`(monitor_id, checked_at)` index. For the stats query, which filters on both at once, a
composite index would be better. That's a known, recorded gap — chapter 15 lists it as the
first thing to build next, and explains why it wasn't done as part of the stats work.

## `back_populates`: relationships in both directions

```python
monitors: Mapped[list[Monitor]] = relationship(back_populates="user", ...)   # on User
user: Mapped[User] = relationship(back_populates="monitors")                 # on Monitor
```

`relationship()` is the ORM convenience that lets you write `monitor.user` instead of a
manual join. `back_populates` names the matching attribute on the other class so the two
stay in sync in memory — assigning `monitor.user = someone` also appends to
`someone.monitors`.

`tests/test_monitors_api.py:107` uses this to create a monitor for a brand-new user in one
expression: `Monitor(user=other, url=..., interval_seconds=60)` — no manual `user_id`
juggling.

### 🔍 Try it

1. **Watch the cascade actually cascade:**

   ```bash
   pytest tests/test_models.py -v
   ```

   Read `tests/test_models.py` alongside the output. One of these tests only passes because
   the SQLite fixture explicitly turns foreign keys on — chapter 13 explains that trap.

2. **See both JSON variants compile.** This prints the DDL that each backend would get:

   ```bash
   python -c "
   from sqlalchemy.schema import CreateTable
   from sqlalchemy.dialects import postgresql, sqlite
   from app.models import Monitor
   print(CreateTable(Monitor.__table__).compile(dialect=postgresql.dialect()))
   print(CreateTable(Monitor.__table__).compile(dialect=sqlite.dialect()))
   "
   ```

   Find the `headers` column in each. JSONB in one, JSON in the other — same model class.

3. **Ask yourself the design question:** if `CheckResult.success` were removed and you had
   to infer success from `status_code == expected_status`, what would break? (Hint: look at
   what `status_code` is when a request times out, and then try to write the uptime query.)

---

Previous: [03 — Config & logging](03-config-and-logging.md) · Next: [05 — Database plumbing](05-database-plumbing.md)
