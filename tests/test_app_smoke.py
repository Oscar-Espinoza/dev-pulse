"""End-to-end smoke test: real lifespan (engine + scheduler) on a sqlite file."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base


def _prepare_database(tmp_path, monkeypatch, name: str) -> None:
    """Point settings at a fresh sqlite file and create the schema in it."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / name}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()

    async def _create_schema() -> None:
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create_schema())


def test_app_boots_serves_and_shuts_down(tmp_path, monkeypatch) -> None:
    _prepare_database(tmp_path, monkeypatch, "smoke.db")

    from app.main import create_app

    app = create_app()
    try:
        with TestClient(app) as client:  # runs lifespan: scheduler really starts
            assert client.get("/healthz").json() == {"status": "ok"}

            created = client.post("/users", json={"email": "smoke@example.com"})
            assert created.status_code == 201
            headers = {"X-API-Key": created.json()["api_key"]}

            monitor = client.post(
                "/monitors",
                json={"url": "https://example.com", "interval_seconds": 60},
                headers=headers,
            )
            assert monitor.status_code == 201

            listed = client.get("/monitors", headers=headers)
            assert len(listed.json()) == 1

            # The MCP mount is only assembled by create_app(), which the other
            # fixtures bypass; unauthenticated it must challenge, not 404.
            mcp = client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert mcp.status_code == 401
        # exiting the with-block runs shutdown; reaching here means it was clean
    finally:
        get_settings.cache_clear()


def test_mcp_can_be_switched_off_entirely(tmp_path, monkeypatch) -> None:
    """MCP_ENABLED=false must remove the whole surface and leave REST intact."""
    _prepare_database(tmp_path, monkeypatch, "disabled.db")
    monkeypatch.setenv("MCP_ENABLED", "false")
    get_settings.cache_clear()

    from app.main import create_app

    app = create_app()
    try:
        with TestClient(app) as client:
            assert client.post("/users", json={"email": "off@example.com"}).status_code == 201
            for path in (
                "/mcp/",
                "/.well-known/oauth-protected-resource/mcp",
                "/oauth/consent?txn=x",
            ):
                assert client.get(path).status_code == 404, path
    finally:
        get_settings.cache_clear()
