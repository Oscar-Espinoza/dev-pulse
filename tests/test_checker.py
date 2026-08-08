"""Checker behavior: success, wrong status, timeout, connection error, races."""

import httpx
from sqlalchemy import select

from app.models import CheckResult, Monitor, User
from app.services.checker import CheckOutcome, perform_check, run_monitor_check


def make_client(handler) -> httpx.AsyncClient:
    """httpx client whose transport is a local handler — no real network."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_perform_check_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-token"] == "abc"
        assert request.method == "GET"
        return httpx.Response(200)

    async with make_client(handler) as client:
        outcome = await perform_check(
            client, url="https://example.com/health", method="GET",
            expected_status=200, headers={"X-Token": "abc"},
        )
    assert outcome == CheckOutcome(
        status_code=200,
        response_time_ms=outcome.response_time_ms,  # timing not asserted exactly
        success=True,
        error_message=None,
    )
    assert outcome.response_time_ms >= 0


async def test_perform_check_unexpected_status_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with make_client(handler) as client:
        outcome = await perform_check(
            client, url="https://example.com", method="GET",
            expected_status=200, headers={},
        )
    assert outcome.success is False
    assert outcome.status_code == 503
    assert "Expected status 200, got 503" in (outcome.error_message or "")


async def test_perform_check_timeout_is_failure_not_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with make_client(handler) as client:
        outcome = await perform_check(
            client, url="https://slow.example.com", method="GET",
            expected_status=200, headers={},
        )
    assert outcome.success is False
    assert outcome.status_code is None
    assert "timed out" in (outcome.error_message or "").lower()


async def test_perform_check_connection_error_is_failure_not_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with make_client(handler) as client:
        outcome = await perform_check(
            client, url="https://down.example.com", method="GET",
            expected_status=200, headers={},
        )
    assert outcome.success is False
    assert outcome.status_code is None
    assert "ConnectError" in (outcome.error_message or "")


async def _seed_monitor(session_factory, **overrides) -> Monitor:
    async with session_factory() as session:
        user = User(email=f"checker-{overrides.get('url', 'x')}@example.com")
        monitor = Monitor(
            user=user,
            url=overrides.get("url", "https://example.com"),
            interval_seconds=overrides.get("interval_seconds", 60),
            is_active=overrides.get("is_active", True),
        )
        session.add(monitor)
        await session.commit()
        await session.refresh(monitor)
        return monitor


async def test_run_monitor_check_records_success(session_factory) -> None:
    monitor = await _seed_monitor(session_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with make_client(handler) as client:
        await run_monitor_check(monitor.id, session_factory, client)

    async with session_factory() as session:
        rows = (await session.execute(select(CheckResult))).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is True
    assert rows[0].status_code == 200
    assert rows[0].checked_at is not None


async def test_run_monitor_check_records_failure_row(session_factory) -> None:
    monitor = await _seed_monitor(session_factory, url="https://down.example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with make_client(handler) as client:
        await run_monitor_check(monitor.id, session_factory, client)

    async with session_factory() as session:
        rows = (await session.execute(select(CheckResult))).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].status_code is None
    assert rows[0].error_message


async def test_run_monitor_check_skips_missing_monitor(session_factory) -> None:
    """A job may fire after its monitor was deleted — must be a silent no-op."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with make_client(handler) as client:
        await run_monitor_check(99999, session_factory, client)  # must not raise

    async with session_factory() as session:
        rows = (await session.execute(select(CheckResult))).scalars().all()
    assert rows == []
