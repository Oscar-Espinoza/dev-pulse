"""In-process scheduling of recurring monitor checks via APScheduler 3.x."""

from datetime import timezone

import httpx
import structlog
from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Monitor
from app.services.checker import run_monitor_check

logger = structlog.stdlib.get_logger(__name__)

#: Late checks are still run if within this many seconds of their slot,
#: instead of being discarded (e.g. after event-loop stalls).
MISFIRE_GRACE_SECONDS = 30


class MonitorScheduler:
    """Owns the AsyncIOScheduler and maps monitors to interval jobs.

    One job per active monitor, keyed ``monitor:{id}``. Jobs run with
    ``max_instances=1`` and ``coalesce=True`` so a slow check can never
    overlap with itself, and a backlog collapses into a single run.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        http_client: httpx.AsyncClient,
    ) -> None:
        self._session_factory = session_factory
        self._http_client = http_client
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)

    @staticmethod
    def job_id(monitor_id: int) -> str:
        """APScheduler job id for a monitor."""
        return f"monitor:{monitor_id}"

    async def start(self) -> None:
        """Schedule all active monitors from the DB, then start the scheduler.

        Must be called with a running event loop (FastAPI lifespan).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(Monitor).where(Monitor.is_active.is_(True))
            )
            monitors = result.scalars().all()
        for monitor in monitors:
            try:
                self.schedule_monitor(monitor.id, monitor.interval_seconds)
            except Exception:
                logger.exception(
                    "monitor_schedule_failed_on_startup", monitor_id=monitor.id
                )
        self._scheduler.start()
        logger.info("scheduler_started", scheduled_jobs=len(monitors))

    async def shutdown(self) -> None:
        """Stop the scheduler without waiting for in-flight jobs."""
        self._scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")

    def schedule_monitor(self, monitor_id: int, interval_seconds: int) -> None:
        """Add — or replace, if it already exists — the job for one monitor."""
        self._scheduler.add_job(
            self._run_check,
            trigger=IntervalTrigger(seconds=interval_seconds),
            args=[monitor_id],
            id=self.job_id(monitor_id),
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
        logger.info(
            "monitor_job_scheduled",
            monitor_id=monitor_id,
            interval_seconds=interval_seconds,
        )

    def remove_monitor(self, monitor_id: int) -> None:
        """Remove a monitor's job; a missing job is not an error."""
        try:
            self._scheduler.remove_job(self.job_id(monitor_id))
            logger.info("monitor_job_removed", monitor_id=monitor_id)
        except JobLookupError:
            logger.debug("monitor_job_already_absent", monitor_id=monitor_id)

    def sync_monitor(
        self, monitor_id: int, *, is_active: bool, interval_seconds: int
    ) -> None:
        """Converge one monitor's job to its desired state (API create/update)."""
        if is_active:
            self.schedule_monitor(monitor_id, interval_seconds)
        else:
            self.remove_monitor(monitor_id)

    def get_job(self, monitor_id: int) -> Job | None:
        """Return the APScheduler job for a monitor, if scheduled."""
        return self._scheduler.get_job(self.job_id(monitor_id))

    async def _run_check(self, monitor_id: int) -> None:
        """Job entrypoint: one check run, guaranteed never to raise.

        HTTP-level failures are already recorded by ``run_monitor_check``;
        this guard catches infrastructure errors (e.g. DB unreachable) so an
        individual failure can never kill the scheduler loop.
        """
        try:
            await run_monitor_check(
                monitor_id, self._session_factory, self._http_client
            )
        except Exception:
            logger.exception("monitor_check_errored", monitor_id=monitor_id)
