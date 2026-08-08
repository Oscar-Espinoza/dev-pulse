# Monitor Stats Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Context

The original request was JWT auth + an analytics endpoint. During planning the user answered **"Actually leave the authentication as it is"** — so the JWT/OAuth2/passlib work is **dropped entirely**. Auth stays the existing `X-API-Key` scheme (`CurrentUser` dependency in `app/dependencies.py`). This plan covers only the analytics endpoint:

`GET /monitors/{monitor_id}/stats?hours=N` → uptime %, average response time, incident count over the last N hours, computed in **a single SQL aggregation statement** (no loading rows into Python).

User-confirmed product decisions:
- **Incident** = a downtime *episode*: a run of consecutive `success=false` checks counts once (computed with a `LAG` window function). A run that started before the window's left edge still counts once inside the window.
- **Window** = `?hours=N` int query param, default `24`, bounds `1..8760`.
- **Avg response time** = over **successful checks only** (timeouts record ~10,000 ms and would skew it); `null` when there are no successes in the window.

**Goal:** Add an ownership-scoped `GET /monitors/{monitor_id}/stats` endpoint backed by one CTE+LAG aggregation query.

**Architecture:** New `MonitorStats` response schema in `app/schemas.py`; new route in `app/routers/monitors.py` reusing the existing `_get_owned_monitor` 404-on-not-owned helper and `CurrentUser` auth. The query is a CTE over `check_results` filtered by monitor + cutoff, with `LAG(success)` ordered by `(checked_at, id)`, aggregated in an outer SELECT (COUNT / SUM-CASE / AVG-CASE). No model or migration changes.

**Tech Stack:** FastAPI 0.141, SQLAlchemy 2.0.51 async (prod Postgres/asyncpg, tests in-memory aiosqlite), pytest 9 + pytest-asyncio (`asyncio_mode="auto"`, no marks needed). Run tests with `pytest` from repo root using `.venv`.

## Global Constraints

- **No new dependencies.** Everything uses stdlib + already-installed packages. Do not touch `pyproject.toml`.
- **Auth unchanged.** Do not modify `app/dependencies.py`, the `X-API-Key` scheme, or any existing auth test.
- **Single SQL statement** for the stats computation — one `session.execute()` for all aggregates (the ownership check's `session.get` is separate and fine).
- **No schema/migration changes** — no Alembic revision, no model edits.
- Design was validated by executing the exact statement on aiosqlite (SQLite 3.45.1 ≥ 3.25 needed for LAG; Postgres supports it natively). An empty CTE still yields exactly one aggregate row, so `.one()` is safe; `SUM` over zero rows is `NULL` (handled with `or 0` / the `if total` guard).
- **Test seeding rule:** always build `checked_at` values from `utcnow()` (from `app.models`) minus `timedelta`s — never naive/local datetimes. SQLite stores these as naive-UTC strings and the aware cutoff binds to the identical format, so comparisons stay correct on both backends.
- The conftest `app` fixture registers routers manually — but it uses the same `monitors.router` object, so a route added to `app/routers/monitors.py` is automatically live in tests. Nothing to change in `tests/conftest.py` or `app/main.py`.

---

### Task 1: Stats endpoint (`GET /monitors/{monitor_id}/stats`)

**Files:**
- Modify: `app/schemas.py` (add `MonitorStats` after `CheckResultRead`, ~line 64)
- Modify: `app/routers/monitors.py` (imports at top; new route after `list_results`, end of file)
- Test: `tests/test_monitors_api.py` (new `# --- stats ---` section at end of file)

**Interfaces:**
- Consumes (all existing): `_get_owned_monitor(session: AsyncSession, monitor_id: int, user_id: int) -> Monitor` and `_create(client, auth_headers, **overrides) -> dict` (module-level helper in `tests/test_monitors_api.py`); fixtures `client`, `auth_headers`, `session_factory`; `CheckResult` columns `monitor_id`, `status_code: int | None`, `response_time_ms: float`, `success: bool`, `checked_at: datetime`; `utcnow() -> datetime` from `app.models`.
- Produces: `MonitorStats` Pydantic schema with fields `monitor_id: int`, `window_hours: int`, `total_checks: int`, `uptime_percent: float | None`, `avg_response_time_ms: float | None`, `incident_count: int`; route `monitor_stats(monitor_id, user, session, hours)` at `GET /monitors/{monitor_id}/stats`.

- [ ] **Step 1: Add the response schema**

In `app/schemas.py`, after `CheckResultRead`, add (no `from_attributes` — this schema is constructed directly, never from an ORM object):

```python
class MonitorStats(BaseModel):
    """Aggregated check statistics for one monitor over a time window."""

    monitor_id: int
    window_hours: int
    total_checks: int
    uptime_percent: float | None
    avg_response_time_ms: float | None
    incident_count: int
```

- [ ] **Step 2: Write the failing tests**

In `tests/test_monitors_api.py`, change the imports at the top:

```python
from datetime import datetime, timedelta

from app.config import Settings, get_settings
from app.models import CheckResult, Monitor, User, utcnow
```

Then append this section at the end of the file:

```python
# --- stats ----------------------------------------------------------------

async def _seed_checks(
    session_factory, monitor_id: int, checks: list[tuple[bool, float, datetime]]
) -> None:
    """Insert CheckResult rows given as (success, response_time_ms, checked_at)."""
    async with session_factory() as session:
        session.add_all(
            [
                CheckResult(
                    monitor_id=monitor_id,
                    status_code=200 if success else None,
                    response_time_ms=response_time_ms,
                    success=success,
                    checked_at=checked_at,
                )
                for success, response_time_ms, checked_at in checks
            ]
        )
        await session.commit()


async def test_stats_requires_api_key(client) -> None:
    response = await client.get("/monitors/1/stats")
    assert response.status_code == 401


async def test_stats_other_users_monitor_is_404(
    client, auth_headers, session_factory
) -> None:
    async with session_factory() as session:
        other = User(email="stats-other@example.com")
        monitor = Monitor(
            user=other, url="https://other.example.com", interval_seconds=60
        )
        session.add(monitor)
        await session.commit()
        foreign_id = monitor.id
    response = await client.get(f"/monitors/{foreign_id}/stats", headers=auth_headers)
    assert response.status_code == 404


async def test_stats_empty_window(client, auth_headers) -> None:
    created = await _create(client, auth_headers)
    response = await client.get(f"/monitors/{created['id']}/stats", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "monitor_id": created["id"],
        "window_hours": 24,
        "total_checks": 0,
        "uptime_percent": None,
        "avg_response_time_ms": None,
        "incident_count": 0,
    }


async def test_stats_counts_incident_episodes_not_failed_checks(
    client, auth_headers, session_factory
) -> None:
    """S F F S F -> 2 incidents; avg latency over the successes only."""
    created = await _create(client, auth_headers)
    now = utcnow()
    await _seed_checks(
        session_factory,
        created["id"],
        [
            (True, 100.0, now - timedelta(minutes=5)),
            (False, 10_000.0, now - timedelta(minutes=4)),
            (False, 10_000.0, now - timedelta(minutes=3)),
            (True, 200.0, now - timedelta(minutes=2)),
            (False, 10_000.0, now - timedelta(minutes=1)),
        ],
    )
    response = await client.get(f"/monitors/{created['id']}/stats", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total_checks"] == 5
    assert body["uptime_percent"] == 40.0
    assert body["avg_response_time_ms"] == 150.0
    assert body["incident_count"] == 2


async def test_stats_excludes_checks_outside_window(
    client, auth_headers, session_factory
) -> None:
    created = await _create(client, auth_headers)
    now = utcnow()
    await _seed_checks(
        session_factory,
        created["id"],
        [
            (False, 10_000.0, now - timedelta(hours=48)),
            (True, 100.0, now - timedelta(minutes=2)),
            (True, 300.0, now - timedelta(minutes=1)),
        ],
    )
    response = await client.get(
        f"/monitors/{created['id']}/stats?hours=24", headers=auth_headers
    )
    body = response.json()
    assert body["total_checks"] == 2
    assert body["uptime_percent"] == 100.0
    assert body["avg_response_time_ms"] == 200.0
    assert body["incident_count"] == 0


async def test_stats_all_failures_is_one_incident(
    client, auth_headers, session_factory
) -> None:
    created = await _create(client, auth_headers)
    now = utcnow()
    await _seed_checks(
        session_factory,
        created["id"],
        [
            (False, 10_000.0, now - timedelta(minutes=3)),
            (False, 10_000.0, now - timedelta(minutes=2)),
            (False, 10_000.0, now - timedelta(minutes=1)),
        ],
    )
    body = (
        await client.get(f"/monitors/{created['id']}/stats", headers=auth_headers)
    ).json()
    assert body["uptime_percent"] == 0.0
    assert body["avg_response_time_ms"] is None
    assert body["incident_count"] == 1


async def test_stats_episode_straddling_window_edge_counts_once(
    client, auth_headers, session_factory
) -> None:
    """A failure run that began before the cutoff still counts as exactly one
    incident inside the window (its first in-window row has no predecessor)."""
    created = await _create(client, auth_headers)
    now = utcnow()
    await _seed_checks(
        session_factory,
        created["id"],
        [
            (False, 10_000.0, now - timedelta(hours=30)),
            (False, 10_000.0, now - timedelta(minutes=2)),
            (False, 10_000.0, now - timedelta(minutes=1)),
        ],
    )
    body = (
        await client.get(
            f"/monitors/{created['id']}/stats?hours=24", headers=auth_headers
        )
    ).json()
    assert body["total_checks"] == 2
    assert body["incident_count"] == 1


async def test_stats_ignores_other_monitors_results(
    client, auth_headers, session_factory
) -> None:
    mine = await _create(client, auth_headers)
    noisy = await _create(client, auth_headers, url="https://example.com/b")
    now = utcnow()
    await _seed_checks(
        session_factory, noisy["id"], [(False, 10_000.0, now - timedelta(minutes=1))]
    )
    await _seed_checks(
        session_factory, mine["id"], [(True, 100.0, now - timedelta(minutes=1))]
    )
    body = (
        await client.get(f"/monitors/{mine['id']}/stats", headers=auth_headers)
    ).json()
    assert body["total_checks"] == 1
    assert body["uptime_percent"] == 100.0
    assert body["incident_count"] == 0


async def test_stats_hours_bounds_validated(client, auth_headers) -> None:
    created = await _create(client, auth_headers)
    for bad in (0, 8761):
        response = await client.get(
            f"/monitors/{created['id']}/stats?hours={bad}", headers=auth_headers
        )
        assert response.status_code == 422
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pytest tests/test_monitors_api.py -v -k stats`
Expected: ALL 9 stats tests FAIL. The route doesn't exist yet, so every request to `/monitors/.../stats` returns 404 — even `test_stats_requires_api_key` (routing happens before auth, so it gets 404, not 401) and the assertion-based tests (404 body has no stats keys). The 44 pre-existing tests must still pass. If tests instead error at collection time, fix the import block from Step 2.

- [ ] **Step 4: Implement the endpoint**

In `app/routers/monitors.py`, update the imports:

```python
from datetime import timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import DbSession
from app.dependencies import CurrentUser, SchedulerDep
from app.models import CheckResult, Monitor, utcnow
from app.schemas import (
    CheckResultRead,
    MonitorCreate,
    MonitorRead,
    MonitorStats,
    MonitorUpdate,
)
```

Then append after `list_results` (end of file):

```python
@router.get("/{monitor_id}/stats", response_model=MonitorStats)
async def monitor_stats(
    monitor_id: int,
    user: CurrentUser,
    session: DbSession,
    hours: Annotated[int, Query(ge=1, le=8760)] = 24,
) -> MonitorStats:
    """Uptime %, avg latency (successful checks only), and incident count over
    the last ``hours`` hours, in one aggregation query. An incident is a run of
    consecutive failures; a run cut by the window's left edge counts once."""
    await _get_owned_monitor(session, monitor_id, user.id)
    cutoff = utcnow() - timedelta(hours=hours)
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
    stmt = select(
        func.count().label("total_checks"),
        func.sum(case((windowed.c.success, 1), else_=0)).label("successful_checks"),
        func.avg(case((windowed.c.success, windowed.c.response_time_ms))).label(
            "avg_response_time_ms"
        ),
        func.sum(
            case(
                (
                    and_(
                        ~windowed.c.success,
                        or_(
                            windowed.c.prev_success.is_(None),
                            windowed.c.prev_success.is_(True),
                        ),
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("incident_count"),
    ).select_from(windowed)
    row = (await session.execute(stmt)).one()
    total = row.total_checks
    return MonitorStats(
        monitor_id=monitor_id,
        window_hours=hours,
        total_checks=total,
        uptime_percent=round(row.successful_checks / total * 100, 2) if total else None,
        avg_response_time_ms=(
            round(row.avg_response_time_ms, 2)
            if row.avg_response_time_ms is not None
            else None
        ),
        incident_count=row.incident_count or 0,
    )
```

Notes for the implementer:
- `case((cond, value))` with no `else_` yields NULL, which `avg()` skips — that's how "successful checks only" works.
- Aggregates over the empty CTE return one row of `(0, NULL, NULL, NULL)` — already handled by `if total` and `or 0`.
- Route ordering doesn't matter: path params never span `/`, so `/{monitor_id}` cannot capture `/5/stats`.

- [ ] **Step 5: Run the stats tests to verify they pass**

Run: `pytest tests/test_monitors_api.py -v -k stats`
Expected: all 9 stats tests PASS.

- [ ] **Step 6: Run the full suite**

Run: `pytest`
Expected: all tests pass (44 pre-existing + 9 new), no warnings introduced.

- [ ] **Step 7: Commit**

```bash
git add app/schemas.py app/routers/monitors.py tests/test_monitors_api.py
git commit -m "feat: add GET /monitors/{id}/stats aggregation endpoint"
```

---

### Task 2: Documentation

**Files:**
- Modify: `README.md` (quickstart curl block, after the create-monitor curl at ~line 57)
- Create: `docs/superpowers/plans/2026-08-07-monitor-stats-endpoint.md` (copy of this plan)

**Interfaces:**
- Consumes: the `GET /monitors/{monitor_id}/stats?hours=N` route from Task 1.
- Produces: nothing consumed by later tasks (final task).

- [ ] **Step 1: Add a stats example to the README quickstart**

In `README.md`, append to the second ```bash block (after the create-monitor curl):

```bash
# uptime %, avg latency, and incident count over the last 24h
curl -s "localhost:8000/monitors/<monitor id>/stats?hours=24" -H "X-API-Key: <your key>"
```

- [ ] **Step 2: Save this plan into the repo's plans directory**

Copy this plan file verbatim to `docs/superpowers/plans/2026-08-07-monitor-stats-endpoint.md` (the repo keeps executed plans there; see the existing `2026-08-07-devpulse-monitoring-engine.md`).

- [ ] **Step 3: Verify docs render and tests still pass**

Run: `pytest`
Expected: all green (docs-only change; this is a guard against accidental code edits).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/plans/2026-08-07-monitor-stats-endpoint.md
git commit -m "docs: document monitor stats endpoint and record its plan"
```

---

## Verification (end-to-end)

1. `pytest` — full suite green (53 tests).
2. Optional manual check against a real Postgres (validates LAG/CTE on the prod dialect):
   ```bash
   uvicorn app.main:app --reload
   # register, create a monitor with interval_seconds=60, wait a few checks, then:
   curl -s "localhost:8000/monitors/1/stats?hours=1" -H "X-API-Key: <key>"
   ```
   Expect JSON like `{"monitor_id": 1, "window_hours": 1, "total_checks": 3, "uptime_percent": 100.0, "avg_response_time_ms": 182.4, "incident_count": 0}`.
3. `/docs` (Swagger) shows the new endpoint with the `hours` param bounds and `MonitorStats` response schema — generated automatically, nothing to write.

## Self-Review Notes

- Spec coverage: uptime % ✓ (uptime_percent), avg response time ✓ (successful-only per user decision), incident count ✓ (episodes via LAG), single SQL aggregation ✓ (one CTE statement, one execute), ownership scoping ✓ (`_get_owned_monitor` + `CurrentUser`). JWT scope intentionally dropped per the user's "leave the authentication as it is".
- Type consistency: `MonitorStats` field names in Task 1 Step 1 match the endpoint's constructor kwargs (Step 4) and every test's asserted JSON keys (Step 2).
- The design (including empty-window one-row behavior and window-edge incident semantics) was validated by executing the exact statement against the app's models on aiosqlite during planning.
