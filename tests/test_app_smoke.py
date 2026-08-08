"""End-to-end smoke test: real lifespan (engine + scheduler) on a sqlite file."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base


def test_app_boots_serves_and_shuts_down(tmp_path, monkeypatch) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'smoke.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()

    async def _create_schema() -> None:
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create_schema())

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
        # exiting the with-block runs shutdown; reaching here means it was clean
    finally:
        get_settings.cache_clear()
