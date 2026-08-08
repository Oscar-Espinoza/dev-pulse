"""Application configuration loaded from environment variables and .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """DevPulse runtime configuration.

    Every field can be overridden via an environment variable of the same
    name (case-insensitive), e.g. ``DATABASE_URL``, ``MIN_INTERVAL_SECONDS``.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql://devpulse:devpulse@localhost:5432/devpulse"
    echo_sql: bool = False
    json_logs: bool = False
    max_active_monitors_per_user: int = 20
    min_interval_seconds: int = 60
    http_timeout_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
