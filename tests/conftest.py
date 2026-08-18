"""Shared test fixtures. Grown across tasks (DB here; app/client in Tasks 6-7)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import get_db
from app.dependencies import get_scheduler
from app.mcp.oauth import DevPulseOAuthProvider
from app.main import build_mcp_components, mount_mcp
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


# --- MCP surface ----------------------------------------------------------
#
# The MCP tools live outside the request cycle, so FastAPI's
# ``dependency_overrides`` cannot reach them. They take their resources from
# getters instead, which is what these fixtures substitute.


@pytest.fixture
def mcp_settings() -> Settings:
    """Settings for MCP tests, isolated from any developer .env.

    Mutable on purpose: the server reads it through a getter, so a test can
    tighten a limit before making a call. The base URL must be localhost —
    RFC 8414 requires an HTTPS issuer and the SDK exempts only localhost.
    """
    return Settings(_env_file=None, mcp_base_url="http://localhost")


@pytest.fixture
def mcp_parts(session_factory, scheduler_spy: SchedulerSpy, mcp_settings):
    """The MCP sub-app and its OAuth provider, built by the production code."""
    return build_mcp_components(
        settings=mcp_settings,
        session_factory_getter=lambda: session_factory,
        scheduler_getter=lambda: scheduler_spy,
    )


@pytest.fixture
def oauth_provider(mcp_parts) -> DevPulseOAuthProvider:
    return mcp_parts[1]


@pytest.fixture
def mcp_app(mcp_parts) -> FastAPI:
    """A host app carrying the MCP mount, consent page and discovery routes.

    Assembled through the same ``build_mcp_components``/``mount_mcp`` pair that
    ``create_app`` uses, so the wiring under test is the wiring that ships.
    """
    mcp_asgi, provider = mcp_parts
    application = FastAPI(lifespan=mcp_asgi.lifespan)
    mount_mcp(application, mcp_asgi, provider)
    return application


@pytest.fixture
def mcp_client_factory(mcp_app: FastAPI):
    """Build an MCP client authenticating as whoever owns ``api_key``.

    Requests are routed straight into the ASGI app, so no socket is opened.
    """

    def build(api_key: str) -> Client:
        def httpx_client_factory(**kwargs) -> AsyncClient:
            kwargs.pop("transport", None)
            kwargs.pop("base_url", None)
            return AsyncClient(
                transport=ASGITransport(app=mcp_app),
                base_url="http://test",
                **kwargs,
            )

        return Client(
            StreamableHttpTransport(
                url="http://test/mcp/",
                headers={"Authorization": f"Bearer {api_key}"},
                httpx_client_factory=httpx_client_factory,
            )
        )

    return build


@pytest.fixture
def mcp_http(mcp_app: FastAPI):
    """Plain HTTP against the MCP host app: ``async with mcp_http() as http``.

    For the OAuth endpoints, which are ordinary HTTP rather than MCP calls.
    Runs inside the lifespan so an MCP client can be opened alongside it in the
    same task — see ``mcp_session`` for why that matters.
    """

    @asynccontextmanager
    async def open_client() -> AsyncIterator[AsyncClient]:
        async with mcp_app.router.lifespan_context(mcp_app):
            async with AsyncClient(
                transport=ASGITransport(app=mcp_app), base_url="http://localhost"
            ) as client:
                yield client

    return open_client


@pytest.fixture
def mcp_session(mcp_app: FastAPI, mcp_client_factory, user: User):
    """Open an authenticated MCP session: ``async with mcp_session() as mcp``.

    Deliberately a *sync* fixture handing back a context manager rather than an
    async fixture that yields a live client. The MCP sub-application's lifespan
    owns an anyio task group, and anyio requires a task group to be exited by
    the same task that entered it. pytest-asyncio runs async-generator fixture
    setup and teardown in separate tasks, so holding the lifespan open across
    the yield fails on teardown. Entering it inside the test body keeps both
    ends in one task.
    """

    @asynccontextmanager
    async def open_session(api_key: str | None = None) -> AsyncIterator[Client]:
        async with mcp_app.router.lifespan_context(mcp_app):
            async with mcp_client_factory(api_key or user.api_key) as client:
                yield client

    return open_session
