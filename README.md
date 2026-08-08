# DevPulse

DevPulse is an uptime & latency monitoring API. Register a monitor — a URL,
HTTP method, expected status code, and check interval — and DevPulse checks
it on schedule, recording every result (success, timeout, connection error,
wrong status) so you can query its history.

Portfolio project built to practice a production-shaped FastAPI backend:
typed async ORM models, a dynamically-scheduled background job system, and
per-user rate limiting, all wired through FastAPI's lifespan.

## Tech Stack

- **FastAPI** — API layer, dependency injection, lifespan-managed startup/shutdown
- **SQLAlchemy 2.0** (async, typed `Mapped[]` models) + **Postgres** (Neon-compatible)
- **APScheduler 3.x** (`AsyncIOScheduler`) — one recurring job per active monitor
- **httpx** — async HTTP checks
- **Alembic** — schema migrations
- **structlog** — structured logging
- **pytest** + **pytest-asyncio** — async test suite (in-memory SQLite, mocked HTTP)

## How it works

```
FastAPI lifespan
 ├─ async engine (Neon-aware URL normalization, pooler-safe)
 ├─ shared httpx.AsyncClient
 └─ MonitorScheduler (APScheduler AsyncIOScheduler)
      ├─ on startup: schedules one interval job per active monitor
      ├─ on API create/update/delete: syncs exactly that monitor's job
      └─ each run: HTTP probe -> CheckResult row (never crashes the loop)
```

Monitor CRUD reschedules just the one affected job rather than reloading
everything. Per-user limits (configurable): max 20 active monitors, minimum
60s interval.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # point DATABASE_URL at your Postgres

alembic upgrade head
uvicorn app.main:app --reload
```

```bash
# register and grab your API key
curl -s -X POST localhost:8000/users -H 'content-type: application/json' \
  -d '{"email":"me@example.com"}'

# create a monitor checked every 60s
curl -s -X POST localhost:8000/monitors -H "X-API-Key: <your key>" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com","interval_seconds":60}'

# uptime %, avg latency, and incident count over the last 24h
curl -s "localhost:8000/monitors/<monitor id>/stats?hours=24" -H "X-API-Key: <your key>"
```

Interactive docs: http://localhost:8000/docs

## Tests

```bash
pytest
```

No network and no Postgres required — tests run on in-memory SQLite with
mocked HTTP.

## Known limitations

This is a portfolio project, not a hardened production service: monitors can
target any URL (including internal addresses), and per-user quotas aren't
enforced across accounts since registration has no verification step.
