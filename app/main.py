"""DevPulse application entrypoint: app factory and lifespan wiring."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

from app.config import Settings, get_settings
from app.database import build_engine
from app.logging import configure_logging
from app.mcp.oauth import CONSENT_PATH, DevPulseOAuthProvider
from app.mcp.server import build_mcp_server
from app.routers import monitors, oauth, users
from app.services.scheduler import MonitorScheduler

logger = structlog.stdlib.get_logger(__name__)

MCP_MOUNT_PATH = "/mcp"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build shared resources on startup; tear them down on shutdown.

    Startup: engine -> session factory -> shared HTTP client -> scheduler
    (which loads all active monitors and begins checking). Everything is
    stored on ``app.state`` for dependencies to reach. ``scheduler.start()``
    runs inside the ``try`` so that a startup failure (e.g. an unreachable
    database) still closes the HTTP client and disposes the engine instead
    of leaking their connection pools.
    """
    settings = get_settings()
    engine = build_engine(settings.database_url, echo=settings.echo_sql)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    http_client = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, follow_redirects=True
    )
    scheduler = MonitorScheduler(session_factory, http_client)

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.scheduler = scheduler

    started = False
    try:
        await scheduler.start()
        started = True
        logger.info("app_started")
        yield
    finally:
        if started:
            await scheduler.shutdown()
        await http_client.aclose()
        await engine.dispose()
        logger.info("app_stopped")


def build_mcp_components(
    *,
    settings: Settings,
    session_factory_getter: Callable[[], async_sessionmaker[AsyncSession]],
    scheduler_getter: Callable[[], MonitorScheduler],
) -> tuple[Starlette, DevPulseOAuthProvider]:
    """Build the MCP sub-application and its authorization server.

    Split out of ``create_app`` so the tests assemble the MCP surface through
    exactly the same code the deployed app uses, rather than a copy of it that
    can drift.
    """
    # The OAuth endpoints live inside the MCP mount, so the provider's base_url
    # carries the /mcp prefix; FastMCP's invariant is that base_url + mcp_path
    # equals the externally reachable MCP URL. The consent page is on the
    # parent app and so is addressed separately.
    origin = settings.mcp_base_url.rstrip("/")
    provider = DevPulseOAuthProvider(
        base_url=f"{origin}{MCP_MOUNT_PATH}",
        consent_url=f"{origin}{CONSENT_PATH}",
        session_factory_getter=session_factory_getter,
    )
    mcp = build_mcp_server(
        session_factory_getter=session_factory_getter,
        scheduler_getter=scheduler_getter,
        settings_getter=lambda: settings,
        auth=provider,
    )
    # Stateless: no per-connection session state, so the single Render process
    # can sleep and wake without stranding a client mid-session.
    return mcp.http_app(path="/", stateless_http=True), provider


def mount_mcp(
    app: FastAPI, mcp_app: Starlette, provider: DevPulseOAuthProvider
) -> None:
    """Attach the consent page, discovery metadata and the MCP mount."""
    app.include_router(oauth.build_router(provider))
    # Discovery metadata must answer at the site root even though the MCP
    # endpoint is mounted deeper: clients look for
    # /.well-known/oauth-protected-resource/mcp before they know anything else.
    # Left inside the mount, every client fails discovery.
    #
    # No mcp_path is passed: the provider's base_url already ends in /mcp, and
    # supplying it a second time yields /oauth-protected-resource/mcp/mcp.
    for route in provider.get_well_known_routes():
        app.router.routes.append(route)
    app.mount(MCP_MOUNT_PATH, mcp_app)


def create_app() -> FastAPI:
    """Application factory.

    The MCP server is assembled before the FastAPI app because its ASGI
    sub-application supplies a lifespan that must be merged into the one
    FastAPI is constructed with. The resource getters below close over ``app``
    and are therefore written before it exists; that is safe because nothing
    calls them until a request arrives, long after the name is bound.
    """
    settings = get_settings()
    configure_logging(json_logs=settings.json_logs)

    app_lifespan = lifespan
    mcp_app = None
    oauth_provider = None

    if settings.mcp_enabled:

        def session_factory_getter() -> async_sessionmaker[AsyncSession]:
            return app.state.session_factory

        def scheduler_getter() -> MonitorScheduler:
            return app.state.scheduler

        mcp_app, oauth_provider = build_mcp_components(
            settings=settings,
            session_factory_getter=session_factory_getter,
            scheduler_getter=scheduler_getter,
        )
        app_lifespan = combine_lifespans(lifespan, mcp_app.lifespan)

    app = FastAPI(
        title="DevPulse",
        description="Uptime & latency monitoring with an in-process async scheduler.",
        version="0.1.0",
        lifespan=app_lifespan,
    )
    app.include_router(users.router)
    app.include_router(monitors.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        """Liveness probe (used by Render health checks)."""
        return {"status": "ok"}

    if mcp_app is not None and oauth_provider is not None:
        mount_mcp(app, mcp_app, oauth_provider)

    return app


app = create_app()
