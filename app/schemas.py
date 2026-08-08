"""Pydantic request/response schemas for the API layer."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl

HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


class MonitorBase(BaseModel):
    """Fields shared by monitor create/read schemas."""

    url: HttpUrl
    method: HttpMethod = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    interval_seconds: int = Field(
        ge=1,
        le=31_536_000,
        description="Check period; the API enforces the configured minimum.",
    )
    headers: dict[str, str] = Field(default_factory=dict)
    is_active: bool = True


class MonitorCreate(MonitorBase):
    """Payload for POST /monitors."""


class MonitorUpdate(BaseModel):
    """Partial payload for PATCH /monitors/{id}; omitted fields are unchanged."""

    url: HttpUrl | None = None
    method: HttpMethod | None = None
    expected_status: int | None = Field(default=None, ge=100, le=599)
    interval_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    headers: dict[str, str] | None = None
    is_active: bool | None = None


class MonitorRead(MonitorBase):
    """A monitor as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime


class CheckResultRead(BaseModel):
    """One recorded check outcome."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    monitor_id: int
    status_code: int | None
    response_time_ms: float
    success: bool
    checked_at: datetime
    error_message: str | None


class UserCreate(BaseModel):
    """Payload for POST /users."""

    email: EmailStr


class UserRead(BaseModel):
    """A user including their API key (returned on registration)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    api_key: str
    created_at: datetime
