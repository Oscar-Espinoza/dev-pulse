# DevPulse

Uptime & latency monitoring API. Register a monitor (URL + method + expected
status + interval) and an in-process async scheduler checks it on schedule,
recording every outcome — including timeouts and connection failures — as
queryable results.

**Stack:** FastAPI · SQLAlchemy 2.0 (async) · Postgres (Neon-ready) ·
APScheduler 3.x · httpx · Alembic · structlog

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

Per-user limits (defaults): max 20 active monitors, minimum interval 60s.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # point DATABASE_URL at your Postgres

# local Postgres, if you don't have one:
docker run -d --name devpulse-pg \
  -e POSTGRES_USER=devpulse -e POSTGRES_PASSWORD=devpulse -e POSTGRES_DB=devpulse \
  -p 5432:5432 postgres:16

alembic upgrade head
uvicorn app.main:app --reload
```

### Try it

```bash
# register and grab your API key
curl -s -X POST localhost:8000/users -H 'content-type: application/json' \
  -d '{"email":"me@example.com"}'

export KEY=...  # api_key from the response

# create a monitor checked every 60s
curl -s -X POST localhost:8000/monitors -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com","interval_seconds":60}'

# watch results accumulate
curl -s "localhost:8000/monitors/1/results?limit=5" -H "X-API-Key: $KEY"
```

Interactive docs: http://localhost:8000/docs

## Tests

```bash
pytest
```

No network, no Postgres needed: tests run on in-memory sqlite with mocked HTTP.

## Deploying (Render + Neon)

- **Env var** `DATABASE_URL`: your Neon connection string. The pooled
  (`-pooler`) URL works for the app — prepared-statement caching is disabled
  automatically for it. Use the **direct** URL when running migrations.
- **Build command:** `pip install .`
- **Pre-deploy command:** `alembic upgrade head`
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Health check path:** `/healthz`

Note: the scheduler lives inside the web process (deliberate, to stay on free
tiers). Run **one** instance; Render's free tier sleeps idle services, which
pauses checks until the next request wakes it.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | local dev URL | Postgres DSN (Neon URLs work as-is) |
| `MAX_ACTIVE_MONITORS_PER_USER` | `20` | active-monitor quota per user |
| `MIN_INTERVAL_SECONDS` | `60` | minimum allowed check interval |
| `HTTP_TIMEOUT_SECONDS` | `10.0` | outbound probe timeout |
| `ECHO_SQL` | `false` | log SQL statements |
| `JSON_LOGS` | `false` | JSON log lines instead of pretty console |

## Known limitations

- **SSRF exposure:** monitors can target any URL, including `localhost`,
  RFC1918 private ranges, and cloud metadata endpoints (e.g.
  `169.254.169.254`), and can attach arbitrary headers to the outbound probe.
  This API has no target-URL allowlist/blocklist, and check results
  (status, timing, error text) are readable back through
  `GET /monitors/{id}/results` — effectively a probe oracle into whatever
  network the app runs on. Do not expose this API to untrusted registrants
  in a real deployment without adding one.
- **Per-user quota is advisory, not a global cap:** `POST /users` performs no
  verification, so a malicious actor can register unlimited accounts and
  multiply their effective check volume well past the per-user
  `MAX_ACTIVE_MONITORS_PER_USER` limit. Acceptable for a portfolio project;
  production use would need registration gating (email verification, admin
  approval, or similar) to make the quota meaningful.
