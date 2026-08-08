"""Performs one HTTP check and records its outcome as a CheckResult."""

import time
from dataclasses import dataclass

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CheckResult, Monitor

logger = structlog.stdlib.get_logger(__name__)


@dataclass(slots=True)
class CheckOutcome:
    """Result of a single HTTP probe, independent of persistence."""

    status_code: int | None
    response_time_ms: float
    success: bool
    error_message: str | None


def _elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since ``start`` (a perf_counter timestamp)."""
    return round((time.perf_counter() - start) * 1000, 2)


async def perform_check(
    client: httpx.AsyncClient,
    *,
    url: str,
    method: str,
    expected_status: int,
    headers: dict[str, str],
) -> CheckOutcome:
    """Probe ``url`` and classify the outcome.

    Never raises for HTTP-level problems: timeouts, connection failures and
    unexpected status codes all come back as a failed ``CheckOutcome``.
    """
    start = time.perf_counter()
    try:
        response = await client.request(method, url, headers=headers)
    except httpx.TimeoutException:
        return CheckOutcome(None, _elapsed_ms(start), False, "Request timed out")
    except httpx.HTTPError as exc:
        return CheckOutcome(
            None, _elapsed_ms(start), False, f"{type(exc).__name__}: {exc}"
        )
    elapsed = _elapsed_ms(start)
    if response.status_code == expected_status:
        return CheckOutcome(response.status_code, elapsed, True, None)
    return CheckOutcome(
        response.status_code,
        elapsed,
        False,
        f"Expected status {expected_status}, got {response.status_code}",
    )


async def run_monitor_check(
    monitor_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
) -> None:
    """Scheduler job body: load the monitor, probe it, persist a CheckResult.

    Reads the monitor fresh each run so config edits apply immediately, and
    silently no-ops if the monitor was deleted or deactivated after the job
    fired (delete/reschedule races).
    """
    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        if monitor is None or not monitor.is_active:
            logger.warning("monitor_check_skipped", monitor_id=monitor_id)
            return
        outcome = await perform_check(
            client,
            url=monitor.url,
            method=monitor.method,
            expected_status=monitor.expected_status,
            headers=monitor.headers,
        )
        session.add(
            CheckResult(
                monitor_id=monitor.id,
                status_code=outcome.status_code,
                response_time_ms=outcome.response_time_ms,
                success=outcome.success,
                error_message=outcome.error_message,
            )
        )
        await session.commit()
        logger.info(
            "monitor_checked",
            monitor_id=monitor.id,
            url=monitor.url,
            success=outcome.success,
            status_code=outcome.status_code,
            response_time_ms=outcome.response_time_ms,
        )
