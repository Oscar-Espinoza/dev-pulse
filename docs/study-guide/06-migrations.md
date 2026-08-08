# 06 — Migrations

Directory: `alembic/`. The subject is a single question — *how does the production database
get the right tables?* — and the answer chosen here rules out the obvious shortcut.

## The shortcut, and why it's not used

SQLAlchemy can create your whole schema in one line:

```python
Base.metadata.create_all(engine)
```

Put that in startup and the tables appear. It's tempting, and it's wrong for anything that
outlives its first deploy, because it only ever *creates missing tables*. It will not add a
column to an existing table, change a type, add an index, or backfill data. The day your
model changes, `create_all` does nothing at all and your app breaks against the old schema.

> 🔑 **Decision: Alembic for all schema management; no `create_all` in production.**
> Recorded in the build plan as a decision made before any code was written. Schema changes
> become *files* — reviewable in a pull request, applied in order, and reversible.

Search the app and you'll find `create_all` exactly twice, both in tests
(`tests/conftest.py:37`, `tests/test_app_smoke.py:20`). Chapter 13 explains why tests are
allowed the shortcut: their schema is thrown away microseconds later, so "migrate from the
old shape" is a question that never arises.

## How a migration works

A migration is a Python file with `upgrade()` and `downgrade()` functions and a pointer to
the migration before it. `alembic/versions/06802f2d9c71_initial_schema.py` is this project's
only one so far; its `down_revision` is `None` because it's the first.

That chain of pointers is the schema's version history. `alembic upgrade head` walks from
wherever your database currently is to the newest revision, running each `upgrade()` in
order. The database itself stores its current revision in a table Alembic manages, so the
command is safe to run repeatedly — already-applied migrations are skipped.

## The interesting file: `alembic/env.py`

This is the file Alembic runs to figure out *what* and *where*. The template Alembic
generates reads a connection string from `alembic.ini` and builds its own engine. This
project replaced that:

```python
from app.config import get_settings
from app.database import build_engine
from app.models import Base

target_metadata = Base.metadata
```
`alembic/env.py:9-17`

```python
async def run_async_migrations() -> None:
    """Connect with the app's Neon-aware engine builder and run migrations."""
    engine = build_engine(get_settings().database_url)
```
`alembic/env.py:38-40`

> 🔑 **Decision: migrations import the application's own settings, models, and engine
> builder rather than duplicating them.**

This matters more than it looks. Chapter 05 described three non-obvious fixes needed to
connect to Neon: driver rewriting, stripping `sslmode`, disabling the statement cache
behind a pooler. If `env.py` built its own engine, all three would have to be reimplemented
here — and would drift out of sync the first time one was fixed in only one place.

By importing `build_engine`, migrations inherit every one of those fixes automatically, for
free, forever. The same applies to `target_metadata = Base.metadata`: the migration's idea
of what the schema *should* look like is literally the same object the application uses.

A consequence worth noting: `alembic.ini` has a `sqlalchemy.url` line that is deliberately
unused — `env.py` injects the real URL. The build plan flags this explicitly so nobody
wastes time wondering why editing it has no effect.

## Async migrations

```python
def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())
```
`alembic/env.py:46-47`

Alembic's core is synchronous, but this app's engine is async (chapter 02, idea 3). The
bridge is `asyncio.run(...)` to enter the event loop, then
`await connection.run_sync(do_run_migrations)` at `:42` to hand a synchronous-looking
connection back to Alembic's machinery. This is the standard async template; you don't have
to love it, you just have to recognize it.

`run_migrations_offline` at `:20` is the `--sql` mode: instead of connecting, it prints the
SQL it *would* run. Useful when a DBA has to review or apply changes by hand.

## Autogenerate — and the trap that was avoided

Alembic can compare your models to a live database and write the migration for you:

```bash
alembic revision --autogenerate -m "add something"
```

> 🔑 **Decision: autogenerate was run against a real Postgres 16 container, not SQLite.**

Here's why that's not pedantry. Chapter 04 introduced `JSONVariant`, which renders as
`JSONB` on Postgres and plain `JSON` elsewhere. Autogenerate writes down what it sees — so
generating against SQLite would produce a migration that creates a plain `JSON` column,
and production would silently get the wrong type. The same applies to constraint and index
details.

The rule generalizes: **generate migrations against the database engine you actually
deploy to.**

The build plan then required two more steps, both worth adopting as habits:

1. **Read the generated file before committing it.** Autogenerate is a good first draft,
   not an oracle. It misses renames (it sees a drop and an add), and it can't know about
   data you need to preserve.
2. **Test the round-trip** — `alembic upgrade head`, then `alembic downgrade base`, then
   upgrade again. A `downgrade()` that doesn't work is discovered at the worst possible
   moment otherwise.

## Deployment: two URLs, one database

The build plan's deployment notes specify the pooled `-pooler` URL for the running app and
the **direct** URL for migrations.

The reason connects back to chapter 05. Migrations issue DDL — `CREATE TABLE`, `ALTER
TABLE` — often inside one long transaction, and they're exactly the workload that a
transaction-mode pooler handles worst. The app, with many short queries, is what pooling is
*for*. Different workloads, different endpoints.

## Adding a column: the whole loop

For when you want to extend this app (chapter 15 suggests exactly this as an exercise):

1. Add the field to the model in `app/models.py`.
2. Add it to the relevant schema in `app/schemas.py` if the API should expose it (chapter
   09 — these are deliberately separate steps).
3. Generate: `alembic revision --autogenerate -m "add monitor description"` against
   Postgres.
4. **Read the generated file.**
5. Apply: `alembic upgrade head`.
6. Write a test. The test suite uses `create_all`, so it picks up the model change
   automatically — which means your tests can pass while the migration is broken. Step 5
   against a real database is the only thing that proves the migration works.

That last warning is the one to remember: **a green test suite does not mean your migration
is correct**, because the tests never run it.

### 🔍 Try it

1. **Read the one migration that exists:**

   ```bash
   ls alembic/versions/
   cat alembic/versions/*_initial_schema.py
   ```

   Match its `op.create_table` calls against `app/models.py`. Find the constraint names —
   they follow chapter 04's naming convention, which is what makes them predictable enough
   to write a `downgrade()` against.

2. **See the SQL without a database:**

   ```bash
   alembic upgrade head --sql
   ```

   This is `run_migrations_offline` at work — the exact DDL, no connection needed.

3. **Prove `env.py` reuses the app's config.** Point `DATABASE_URL` at something obviously
   fake and watch the error mention *asyncpg*:

   ```bash
   DATABASE_URL=postgresql://nobody@127.0.0.1:1/none alembic upgrade head
   ```

   The failure comes from the driver `build_engine` selected — proof the rewrite in chapter
   05 is running inside the migration path too.

---

Previous: [05 — Database plumbing](05-database-plumbing.md) · Next: [07 — The checker](07-checker.md)
