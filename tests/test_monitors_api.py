"""Monitors CRUD: auth, limits, scheduler synchronization, ownership."""

from app.config import Settings, get_settings
from app.models import CheckResult, Monitor, User

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
