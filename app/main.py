"""DevPulse application entrypoint: app factory and lifespan wiring."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.database import build_engine
from app.logging import configure_logging
from app.routers import monitors, users
from app.services.scheduler import MonitorScheduler

logger = structlog.stdlib.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build shared resources on startup; tear them down on shutdown.

    Startup: engine -> session factory -> shared HTTP client -> scheduler
    (which loads all active monitors and begins checking). Everything is
    stored on ``app.state`` for dependencies to reach.
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

    await scheduler.start()
    logger.info("app_started")
    try:
        yield
    finally:
        await scheduler.shutdown()
        await http_client.aclose()
        await engine.dispose()
        logger.info("app_stopped")


def create_app() -> FastAPI:
    """Application factory."""
    configure_logging(json_logs=get_settings().json_logs)
    app = FastAPI(
        title="DevPulse",
        description="Uptime & latency monitoring with an in-process async scheduler.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(users.router)
    app.include_router(monitors.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        """Liveness probe (used by Render health checks)."""
        return {"status": "ok"}

    return app


app = create_app()
