"""Async engine construction (Neon-aware) and the request-scoped DB session."""

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def prepare_engine_args(database_url: str) -> tuple[URL, dict[str, Any]]:
    """Normalize a raw DATABASE_URL into (SQLAlchemy URL, asyncpg connect_args).

    Handles Neon-style connection strings:
    - ``postgresql://`` is rewritten to ``postgresql+asyncpg://``.
    - ``sslmode``/``channel_binding`` query params (rejected by asyncpg) are
      stripped; ``ssl=True`` is passed via connect_args instead.
    - Hosts containing ``-pooler`` (Neon's pgbouncer endpoint) get
      ``statement_cache_size=0`` because transaction-mode pgbouncer breaks
      asyncpg's prepared-statement cache.

    Non-Postgres URLs (e.g. sqlite for tests) pass through untouched.
    """
    url = make_url(database_url)
    connect_args: dict[str, Any] = {}
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    if url.drivername == "postgresql+asyncpg":
        query = dict(url.query)
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        url = url.set(query=query)
        if sslmode not in (None, "disable"):
            connect_args["ssl"] = True
        if url.host is not None and "-pooler" in url.host:
            connect_args["statement_cache_size"] = 0
    return url, connect_args


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the application's async engine.

    ``pool_pre_ping`` revalidates pooled connections, which matters for Neon's
    scale-to-zero behavior (idle connections get dropped server-side).
    """
    url, connect_args = prepare_engine_args(database_url)
    return create_async_engine(
        url, echo=echo, connect_args=connect_args, pool_pre_ping=True
    )


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped ``AsyncSession``.

    The session factory is created in the lifespan context and stored on
    ``app.state.session_factory``.
    """
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
