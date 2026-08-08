"""Shared test fixtures. Grown across tasks (DB here; app/client in Tasks 6-7)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base


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
