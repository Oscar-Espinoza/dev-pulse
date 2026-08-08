"""Shared test fixtures. Grown across tasks (DB here; app/client in Tasks 6-7)."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.dependencies import get_scheduler
from app.models import Base, User
from app.routers import monitors, users


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """In-memory aiosqlite engine with schema created and FK enforcement on."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fks(dbapi_connection, _record) -> None:  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the test engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


class SchedulerSpy:
    """Records scheduler calls made by API routes (no real APScheduler)."""

    def __init__(self) -> None:
        self.synced: list[tuple[int, bool, int]] = []
        self.removed: list[int] = []

    def sync_monitor(
        self, monitor_id: int, *, is_active: bool, interval_seconds: int
    ) -> None:
        self.synced.append((monitor_id, is_active, interval_seconds))

    def remove_monitor(self, monitor_id: int) -> None:
        self.removed.append(monitor_id)


@pytest.fixture
def scheduler_spy() -> SchedulerSpy:
    return SchedulerSpy()


@pytest.fixture
def app(session_factory, scheduler_spy: SchedulerSpy) -> FastAPI:
    """App with real routers; DB and scheduler dependencies replaced for tests."""
    application = FastAPI()
    application.include_router(users.router)
    application.include_router(monitors.router)

    async def _get_test_db():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = _get_test_db
    application.dependency_overrides[get_scheduler] = lambda: scheduler_spy
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def user(session_factory) -> User:
    """A persisted user whose api_key authenticates requests."""
    async with session_factory() as session:
        u = User(email="owner@example.com")
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u


@pytest.fixture
def auth_headers(user: User) -> dict[str, str]:
    return {"X-API-Key": user.api_key}
