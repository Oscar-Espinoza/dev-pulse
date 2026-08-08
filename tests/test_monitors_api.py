"""Monitors CRUD: auth, limits, scheduler synchronization, ownership."""

from datetime import datetime, timedelta

from app.config import Settings, get_settings
from app.models import CheckResult, Monitor, User, utcnow

VALID_MONITOR = {
    "url": "https://example.com/health",
    "method": "GET",
    "expected_status": 200,
    "interval_seconds": 300,
    "headers": {"X-Token": "abc"},
}


# --- auth -----------------------------------------------------------------

async def test_missing_api_key_is_401(client) -> None:
    response = await client.get("/monitors")
    assert response.status_code == 401


async def test_unknown_api_key_is_401(client) -> None:
    response = await client.get("/monitors", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


# --- create ---------------------------------------------------------------

async def test_create_monitor_schedules_job(client, auth_headers, scheduler_spy) -> None:
    response = await client.post("/monitors", json=VALID_MONITOR, headers=auth_headers)
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == "https://example.com/health"
    assert body["is_active"] is True
    assert scheduler_spy.synced == [(body["id"], True, 300)]


async def test_create_inactive_monitor_syncs_inactive(
    client, auth_headers, scheduler_spy
) -> None:
    response = await client.post(
        "/monitors",
        json={**VALID_MONITOR, "is_active": False},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert scheduler_spy.synced == [(response.json()["id"], False, 300)]


async def test_interval_below_minimum_rejected(client, auth_headers) -> None:
    response = await client.post(
        "/monitors",
        json={**VALID_MONITOR, "interval_seconds": 30},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "60" in response.json()["detail"]


async def test_interval_far_above_ceiling_rejected(client, auth_headers) -> None:
    """A poison interval (would overflow APScheduler's IntervalTrigger) must
    be rejected at the schema layer, never persisted."""
    response = await client.post(
        "/monitors",
        json={**VALID_MONITOR, "interval_seconds": 10**14},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_active_monitor_quota_enforced(app, client, auth_headers) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, max_active_monitors_per_user=2
    )
    for i in range(2):
        r = await client.post(
            "/monitors",
            json={**VALID_MONITOR, "url": f"https://example.com/{i}"},
            headers=auth_headers,
        )
        assert r.status_code == 201
    over = await client.post("/monitors", json=VALID_MONITOR, headers=auth_headers)
    assert over.status_code == 409
    # inactive monitors don't count against the quota
    inactive = await client.post(
        "/monitors", json={**VALID_MONITOR, "is_active": False}, headers=auth_headers
    )
    assert inactive.status_code == 201


# --- read / ownership -----------------------------------------------------

async def _create(client, auth_headers, **overrides) -> dict:
    r = await client.post(
        "/monitors", json={**VALID_MONITOR, **overrides}, headers=auth_headers
    )
    assert r.status_code == 201
    return r.json()


async def test_list_returns_only_own_monitors(
    client, auth_headers, session_factory
) -> None:
    mine = await _create(client, auth_headers)
    async with session_factory() as session:
        other = User(email="other@example.com")
        session.add(Monitor(user=other, url="https://other.example.com", interval_seconds=60))
        await session.commit()
    response = await client.get("/monitors", headers=auth_headers)
    assert [m["id"] for m in response.json()] == [mine["id"]]


async def test_get_other_users_monitor_is_404(client, auth_headers, session_factory) -> None:
    async with session_factory() as session:
        other = User(email="other2@example.com")
        monitor = Monitor(user=other, url="https://other.example.com", interval_seconds=60)
        session.add(monitor)
        await session.commit()
        foreign_id = monitor.id
    response = await client.get(f"/monitors/{foreign_id}", headers=auth_headers)
    assert response.status_code == 404


# --- update ---------------------------------------------------------------

async def test_patch_interval_reschedules(client, auth_headers, scheduler_spy) -> None:
    created = await _create(client, auth_headers)
    scheduler_spy.synced.clear()
    response = await client.patch(
        f"/monitors/{created['id']}",
        json={"interval_seconds": 600},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["interval_seconds"] == 600
    assert scheduler_spy.synced == [(created["id"], True, 600)]


async def test_patch_deactivate_syncs_inactive(client, auth_headers, scheduler_spy) -> None:
    created = await _create(client, auth_headers)
    scheduler_spy.synced.clear()
    response = await client.patch(
        f"/monitors/{created['id']}", json={"is_active": False}, headers=auth_headers
    )
    assert response.status_code == 200
    assert scheduler_spy.synced == [(created["id"], False, 300)]


async def test_patch_interval_below_minimum_rejected(client, auth_headers) -> None:
    created = await _create(client, auth_headers)
    response = await client.patch(
        f"/monitors/{created['id']}",
        json={"interval_seconds": 10},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_patch_explicit_null_is_ignored_not_applied(
    client, auth_headers
) -> None:
    """Explicit JSON null must be treated like an omitted field, not a value
    to set — str(None) previously corrupted the url column and broke every
    subsequent read of the monitor."""
    created = await _create(client, auth_headers)
    response = await client.patch(
        f"/monitors/{created['id']}", json={"url": None}, headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["url"] == created["url"]

    refetched = await client.get(f"/monitors/{created['id']}", headers=auth_headers)
    assert refetched.status_code == 200
    assert refetched.json()["url"] == created["url"]


async def test_reactivating_already_active_monitor_not_blocked_by_quota(
    app, client, auth_headers
) -> None:
    """An active monitor holds a quota slot; re-PATCHing it must not 409."""
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, max_active_monitors_per_user=1
    )
    created = await _create(client, auth_headers)
    response = await client.patch(
        f"/monitors/{created['id']}", json={"is_active": True}, headers=auth_headers
    )
    assert response.status_code == 200


# --- delete ---------------------------------------------------------------

async def test_delete_removes_job_and_row(
    client, auth_headers, scheduler_spy, session_factory
) -> None:
    created = await _create(client, auth_headers)
    response = await client.delete(f"/monitors/{created['id']}", headers=auth_headers)
    assert response.status_code == 204
    assert scheduler_spy.removed == [created["id"]]
    assert (
        await client.get(f"/monitors/{created['id']}", headers=auth_headers)
    ).status_code == 404


# --- results --------------------------------------------------------------

async def test_results_returned_newest_first(
    client, auth_headers, session_factory
) -> None:
    created = await _create(client, auth_headers)
    async with session_factory() as session:
        session.add_all(
            [
                CheckResult(
                    monitor_id=created["id"], status_code=200,
                    response_time_ms=float(i), success=True,
                )
                for i in range(3)
            ]
        )
        await session.commit()
    response = await client.get(
        f"/monitors/{created['id']}/results?limit=2", headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["id"] > body[1]["id"]


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
