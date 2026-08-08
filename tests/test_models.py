"""ORM round-trip and cascade behavior."""

from sqlalchemy import select

from app.models import CheckResult, Monitor, User


async def test_user_monitor_checkresult_roundtrip(session_factory) -> None:
    async with session_factory() as session:
        user = User(email="oscar@example.com")
        monitor = Monitor(
            user=user,
            url="https://example.com/health",
            interval_seconds=60,
            headers={"X-Token": "abc"},
        )
        session.add(monitor)
        await session.commit()
        await session.refresh(monitor)

        assert monitor.id is not None
        assert monitor.method == "GET"          # default
        assert monitor.expected_status == 200   # default
        assert monitor.is_active is True        # default
        assert monitor.headers == {"X-Token": "abc"}
        assert len(user.api_key) > 20           # generated

        session.add(
            CheckResult(
                monitor_id=monitor.id,
                status_code=200,
                response_time_ms=12.5,
                success=True,
            )
        )
        await session.commit()

    async with session_factory() as session:
        result = (await session.execute(select(CheckResult))).scalars().all()
        assert len(result) == 1
        assert result[0].monitor_id == monitor.id
        assert result[0].error_message is None


async def test_deleting_monitor_cascades_to_results(session_factory) -> None:
    async with session_factory() as session:
        user = User(email="cascade@example.com")
        monitor = Monitor(user=user, url="https://example.com", interval_seconds=60)
        session.add(monitor)
        await session.commit()
        session.add(
            CheckResult(
                monitor_id=monitor.id, status_code=None,
                response_time_ms=1.0, success=False, error_message="boom",
            )
        )
        await session.commit()
        await session.delete(monitor)
        await session.commit()

    async with session_factory() as session:
        remaining = (await session.execute(select(CheckResult))).scalars().all()
        assert remaining == []
