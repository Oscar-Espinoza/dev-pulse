"""Tests for Neon-aware engine argument preparation."""

from app.database import prepare_engine_args

NEON_POOLED = (
    "postgresql://user:pass@ep-cool-cloud-123456-pooler.us-east-2.aws.neon.tech"
    "/devpulse?sslmode=require&channel_binding=require"
)
NEON_DIRECT = (
    "postgresql://user:pass@ep-cool-cloud-123456.us-east-2.aws.neon.tech"
    "/devpulse?sslmode=require"
)


def test_plain_postgres_scheme_upgraded_to_asyncpg() -> None:
    url, _ = prepare_engine_args("postgresql://u:p@localhost:5432/db")
    assert url.drivername == "postgresql+asyncpg"


def test_asyncpg_scheme_kept() -> None:
    url, _ = prepare_engine_args("postgresql+asyncpg://u:p@localhost:5432/db")
    assert url.drivername == "postgresql+asyncpg"


def test_sslmode_and_channel_binding_stripped_and_ssl_enabled() -> None:
    """asyncpg rejects sslmode/channel_binding query params; use ssl=True instead."""
    url, connect_args = prepare_engine_args(NEON_DIRECT)
    assert "sslmode" not in url.query
    assert "channel_binding" not in url.query
    assert connect_args["ssl"] is True


def test_pooler_host_disables_prepared_statement_cache() -> None:
    """Neon's pgbouncer pooler breaks asyncpg's prepared-statement cache."""
    _, connect_args = prepare_engine_args(NEON_POOLED)
    assert connect_args["statement_cache_size"] == 0


def test_direct_host_keeps_statement_cache() -> None:
    _, connect_args = prepare_engine_args(NEON_DIRECT)
    assert "statement_cache_size" not in connect_args


def test_sqlite_url_untouched() -> None:
    """Test/smoke databases pass through without Postgres-specific args."""
    url, connect_args = prepare_engine_args("sqlite+aiosqlite:///./x.db")
    assert url.drivername == "sqlite+aiosqlite"
    assert connect_args == {}
