"""SQLAlchemy 2.0 ORM models: User, Monitor, CheckResult."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB on Postgres, plain JSON elsewhere (sqlite in tests).
JSONVariant = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    """Timezone-aware current UTC time (SQLAlchemy python-side default)."""
    return datetime.now(timezone.utc)


def generate_api_key() -> str:
    """Return a URL-safe random API key."""
    return secrets.token_urlsafe(32)


class Base(DeclarativeBase):
    """Declarative base with a deterministic constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    """An account that owns monitors, identified by an API key."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    api_key: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=generate_api_key
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    monitors: Mapped[list[Monitor]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Monitor(Base):
    """A recurring HTTP check definition owned by a user."""

    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    method: Mapped[str] = mapped_column(String(10), default="GET")
    expected_status: Mapped[int] = mapped_column(default=200)
    interval_seconds: Mapped[int] = mapped_column()
    headers: Mapped[dict[str, str]] = mapped_column(JSONVariant, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="monitors")
    check_results: Mapped[list[CheckResult]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan", passive_deletes=True
    )


class CheckResult(Base):
    """Outcome of one HTTP check run against a monitor."""

    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    status_code: Mapped[int | None] = mapped_column(default=None)
    response_time_ms: Mapped[float] = mapped_column()
    success: Mapped[bool] = mapped_column()
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text(), default=None)

    monitor: Mapped[Monitor] = relationship(back_populates="check_results")
