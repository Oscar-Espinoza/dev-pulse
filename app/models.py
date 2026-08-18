"""SQLAlchemy 2.0 ORM models: User, Monitor, CheckResult, and OAuth state."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
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
    oauth_codes: Mapped[list[OAuthAuthorizationCode]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    oauth_tokens: Mapped[list[OAuthToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
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


# --- OAuth 2.1 authorization server state ---------------------------------
#
# DevPulse is its own authorization server so that chat clients which can only
# speak OAuth (ChatGPT) can reach the MCP surface. These four tables hold the
# whole of that state. They are deliberately separate from ``users``: OAuth
# adds a way to *present* a credential, not a new kind of credential — the
# API key remains the only thing that proves who you are.
#
# Expiries are epoch seconds rather than timestamps. That is what the OAuth
# types use, and it avoids a real trap: sqlite discards timezone information,
# so a DateTime(timezone=True) column round-trips to a naive value that cannot
# be compared against an aware "now" without raising.


class OAuthClient(Base):
    """An MCP client that registered itself via RFC 7591 dynamic registration.

    The registration document is kept verbatim as JSON rather than being spread
    across columns: RFC 7591 lets clients send arbitrary extra metadata, and
    the OAuth library wants the whole object back unchanged.
    """

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    client_name: Mapped[str | None] = mapped_column(String(255), default=None)
    data: Mapped[dict] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class OAuthTransaction(Base):
    """An authorization request parked while the user visits the consent page.

    Created when a client hits ``/authorize`` and consumed when the user
    approves. Holding the parameters server-side means the consent page can
    carry nothing but an opaque id, so a tampered redirect_uri never survives
    the round trip.
    """

    __tablename__ = "oauth_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), index=True
    )
    params: Mapped[dict] = mapped_column(JSONVariant)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class OAuthAuthorizationCode(Base):
    """A single-use code, already bound to the user who approved it.

    Stored rather than signed because single use is the whole point: the row is
    deleted on redemption, which a self-contained token cannot express.
    """

    __tablename__ = "oauth_authorization_codes"

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column()
    code_challenge: Mapped[str] = mapped_column(String(255))
    scopes: Mapped[list[str]] = mapped_column(JSONVariant, default=list)
    resource: Mapped[str | None] = mapped_column(String(2048), default=None)
    expires_at: Mapped[int] = mapped_column(BigInteger)

    user: Mapped[User] = relationship(back_populates="oauth_codes")


class OAuthToken(Base):
    """An issued access or refresh token.

    Kept in the database rather than minted as a self-contained JWT so that a
    token can actually be revoked, and so a restart of the free-tier Render
    instance does not invalidate every connected client.
    """

    __tablename__ = "oauth_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # access | refresh
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[list[str]] = mapped_column(JSONVariant, default=list)
    resource: Mapped[str | None] = mapped_column(String(2048), default=None)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    user: Mapped[User] = relationship(back_populates="oauth_tokens")
