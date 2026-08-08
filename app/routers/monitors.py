"""CRUD for monitors, with per-user limits and live scheduler synchronization."""

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

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(prefix="/monitors", tags=["monitors"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _validate_interval(interval_seconds: int, settings: Settings) -> None:
    """422 if the requested interval undercuts the configured minimum."""
    if interval_seconds < settings.min_interval_seconds:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"interval_seconds must be at least {settings.min_interval_seconds}"
            ),
        )


async def _count_active_monitors(
    session: AsyncSession, user_id: int, *, exclude_id: int | None = None
) -> int:
    """Active monitors owned by ``user_id`` (optionally excluding one)."""
    stmt = (
        select(func.count())
        .select_from(Monitor)
        .where(Monitor.user_id == user_id, Monitor.is_active.is_(True))
    )
    if exclude_id is not None:
        stmt = stmt.where(Monitor.id != exclude_id)
    return (await session.execute(stmt)).scalar_one()


async def _get_owned_monitor(
    session: AsyncSession, monitor_id: int, user_id: int
) -> Monitor:
    """Load a monitor owned by the user, or 404 (never leaks others' ids)."""
    monitor = await session.get(Monitor, monitor_id)
    if monitor is None or monitor.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Monitor not found"
        )
    return monitor


@router.post("", response_model=MonitorRead, status_code=status.HTTP_201_CREATED)
async def create_monitor(
    payload: MonitorCreate,
    user: CurrentUser,
    session: DbSession,
    scheduler: SchedulerDep,
    settings: SettingsDep,
) -> Monitor:
    """Create a monitor and (if active) schedule its recurring check."""
    _validate_interval(payload.interval_seconds, settings)
    if payload.is_active:
        active = await _count_active_monitors(session, user.id)
        if active >= settings.max_active_monitors_per_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Active monitor limit reached "
                    f"({settings.max_active_monitors_per_user})"
                ),
            )
    monitor = Monitor(
        user_id=user.id,
        url=str(payload.url),
        method=payload.method,
        expected_status=payload.expected_status,
        interval_seconds=payload.interval_seconds,
        headers=payload.headers,
        is_active=payload.is_active,
    )
    session.add(monitor)
    await session.commit()
    await session.refresh(monitor)
    scheduler.sync_monitor(
        monitor.id,
        is_active=monitor.is_active,
        interval_seconds=monitor.interval_seconds,
    )
    logger.info("monitor_created", monitor_id=monitor.id, user_id=user.id)
    return monitor


@router.get("", response_model=list[MonitorRead])
async def list_monitors(user: CurrentUser, session: DbSession) -> list[Monitor]:
    """List the requesting user's monitors."""
    result = await session.execute(
        select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.id)
    )
    return list(result.scalars().all())


@router.get("/{monitor_id}", response_model=MonitorRead)
async def get_monitor(
    monitor_id: int, user: CurrentUser, session: DbSession
) -> Monitor:
    """Fetch one of the requesting user's monitors."""
    return await _get_owned_monitor(session, monitor_id, user.id)


@router.patch("/{monitor_id}", response_model=MonitorRead)
async def update_monitor(
    monitor_id: int,
    payload: MonitorUpdate,
    user: CurrentUser,
    session: DbSession,
    scheduler: SchedulerDep,
    settings: SettingsDep,
) -> Monitor:
    """Partially update a monitor and converge its scheduled job."""
    monitor = await _get_owned_monitor(session, monitor_id, user.id)
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "url" in updates:
        updates["url"] = str(updates["url"])

    new_interval = updates.get("interval_seconds", monitor.interval_seconds)
    _validate_interval(new_interval, settings)

    will_be_active = updates.get("is_active", monitor.is_active)
    if will_be_active:
        others = await _count_active_monitors(
            session, user.id, exclude_id=monitor.id
        )
        if others >= settings.max_active_monitors_per_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Active monitor limit reached "
                    f"({settings.max_active_monitors_per_user})"
                ),
            )

    for field, value in updates.items():
        setattr(monitor, field, value)
    await session.commit()
    await session.refresh(monitor)
    scheduler.sync_monitor(
        monitor.id,
        is_active=monitor.is_active,
        interval_seconds=monitor.interval_seconds,
    )
    logger.info("monitor_updated", monitor_id=monitor.id, changed=sorted(updates))
    return monitor


@router.delete("/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_monitor(
    monitor_id: int,
    user: CurrentUser,
    session: DbSession,
    scheduler: SchedulerDep,
) -> None:
    """Delete a monitor (and, via cascade, its results); unschedule its job."""
    monitor = await _get_owned_monitor(session, monitor_id, user.id)
    await session.delete(monitor)
    await session.commit()
    scheduler.remove_monitor(monitor_id)
    logger.info("monitor_deleted", monitor_id=monitor_id, user_id=user.id)


@router.get("/{monitor_id}/results", response_model=list[CheckResultRead])
async def list_results(
    monitor_id: int,
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[CheckResult]:
    """Most recent check results for a monitor, newest first."""
    await _get_owned_monitor(session, monitor_id, user.id)
    result = await session.execute(
        select(CheckResult)
        .where(CheckResult.monitor_id == monitor_id)
        .order_by(CheckResult.checked_at.desc(), CheckResult.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


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
