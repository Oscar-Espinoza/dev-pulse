"""Application configuration loaded from environment variables and .env."""

from functools import lru_cache

from pydantic import field_validator
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
    mcp_enabled: bool = True
    # Public origin of this service, e.g. https://dev-pulse.onrender.com.
    # OAuth metadata advertises absolute URLs built from this, so if it is
    # wrong every client fails discovery. It MUST be set in production.
    mcp_base_url: str = "http://localhost:8000"

    @field_validator("mcp_base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        """Accept a bare hostname and a trailing slash.

        Render's ``fromService``/``property: host`` supplies a hostname with no
        scheme, and everything downstream needs an absolute origin, so fill in
        https when the scheme is missing.
        """
        value = value.strip().rstrip("/")
        if "://" not in value:
            value = f"https://{value}"
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
