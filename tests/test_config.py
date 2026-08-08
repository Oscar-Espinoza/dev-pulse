"""Tests for application settings."""

from app.config import Settings


def test_settings_defaults() -> None:
    """Limits and timeouts have the documented defaults."""
    settings = Settings(_env_file=None, database_url="postgresql://u:p@localhost/db")
    assert settings.max_active_monitors_per_user == 20
    assert settings.min_interval_seconds == 60
    assert settings.http_timeout_seconds == 10.0
    assert settings.echo_sql is False
    assert settings.json_logs is False


def test_settings_read_from_env(monkeypatch) -> None:
    """Environment variables override defaults (case-insensitive)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@example.com/devpulse")
    monkeypatch.setenv("MAX_ACTIVE_MONITORS_PER_USER", "5")
    monkeypatch.setenv("MIN_INTERVAL_SECONDS", "120")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql://x:y@example.com/devpulse"
    assert settings.max_active_monitors_per_user == 5
    assert settings.min_interval_seconds == 120
