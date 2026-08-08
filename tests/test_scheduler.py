"""MonitorScheduler job management: load, add, reschedule, remove, resilience."""

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from app.models import Monitor, User
from app.services.scheduler import MonitorScheduler


def noop_client() -> httpx.AsyncClient:
    """Client that would 200 anything (jobs never actually fire in these tests)."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )


@pytest.fixture
async def scheduler(session_factory) -> AsyncIterator[MonitorScheduler]:
    ms = MonitorScheduler(session_factory, noop_client())
    yield ms
    if ms._scheduler.running:  # cleanup regardless of how the test left it
        await ms.shutdown()


async def seed_monitors(session_factory) -> tuple[int, int, int]:
    """Two active monitors and one inactive; returns their ids."""
    async with session_factory() as session:
        user = User(email="sched@example.com")
        m1 = Monitor(user=user, url="https://a.example.com", interval_seconds=60)
        m2 = Monitor(user=user, url="https://b.example.com", interval_seconds=120)
        m3 = Monitor(
            user=user, url="https://c.example.com",
            interval_seconds=60, is_active=False,
        )
        session.add_all([m1, m2, m3])
        await session.commit()
        return m1.id, m2.id, m3.id


async def test_start_schedules_only_active_monitors(session_factory, scheduler) -> None:
    m1_id, m2_id, m3_id = await seed_monitors(session_factory)
    await scheduler.start()
    assert scheduler.get_job(m1_id) is not None
    assert scheduler.get_job(m2_id) is not None
    assert scheduler.get_job(m3_id) is None
    assert scheduler.get_job(m2_id).trigger.interval == timedelta(seconds=120)


async def test_jobs_have_overlap_protection(session_factory, scheduler) -> None:
    m1_id, _, _ = await seed_monitors(session_factory)
    await scheduler.start()
    job = scheduler.get_job(m1_id)
    assert job.max_instances == 1
    assert job.coalesce is True


async def test_schedule_monitor_replaces_existing_job(session_factory, scheduler) -> None:
    await scheduler.start()
    scheduler.schedule_monitor(42, 60)
    assert scheduler.get_job(42).trigger.interval == timedelta(seconds=60)
    scheduler.schedule_monitor(42, 300)  # reschedule, not duplicate
    assert scheduler.get_job(42).trigger.interval == timedelta(seconds=300)


async def test_remove_monitor_is_idempotent(session_factory, scheduler) -> None:
    await scheduler.start()
    scheduler.schedule_monitor(7, 60)
    scheduler.remove_monitor(7)
    assert scheduler.get_job(7) is None
    scheduler.remove_monitor(7)  # absent — must not raise


async def test_sync_monitor_adds_and_removes(session_factory, scheduler) -> None:
    await scheduler.start()
    scheduler.sync_monitor(9, is_active=True, interval_seconds=90)
    assert scheduler.get_job(9).trigger.interval == timedelta(seconds=90)
    scheduler.sync_monitor(9, is_active=False, interval_seconds=90)
    assert scheduler.get_job(9) is None


async def test_start_survives_poison_interval_row(session_factory, scheduler) -> None:
    """A pre-existing row with an out-of-range interval_seconds (e.g. from
    before the schema validation existed) must not abort scheduler startup;
    the other active monitors must still get scheduled."""
    async with session_factory() as session:
        user = User(email="poison@example.com")
        good = Monitor(user=user, url="https://good.example.com", interval_seconds=60)
        poison = Monitor(
            user=user, url="https://poison.example.com", interval_seconds=10**14
        )
        session.add_all([good, poison])
        await session.commit()
        good_id, poison_id = good.id, poison.id

    await scheduler.start()  # must not raise

    assert scheduler.get_job(good_id) is not None
    assert scheduler.get_job(poison_id) is None


async def test_run_check_survives_unexpected_errors(session_factory) -> None:
    """Even a broken session factory must not propagate out of the job wrapper."""
    def broken_factory() -> None:
        raise RuntimeError("database exploded")

    ms = MonitorScheduler(broken_factory, noop_client())  # type: ignore[arg-type]
    await ms._run_check(1)  # must not raise
