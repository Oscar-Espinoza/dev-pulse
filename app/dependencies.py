"""Request-level dependencies: API-key auth and scheduler access."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select

from app.database import DbSession
from app.models import User
from app.services.scheduler import MonitorScheduler

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="API key issued by POST /users",
)


async def get_current_user(
    session: DbSession,
    api_key: Annotated[str | None, Security(api_key_header)],
) -> User:
    """Resolve the requesting user from the X-API-Key header, or 401."""
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    result = await session.execute(select(User).where(User.api_key == api_key))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return user


def get_scheduler(request: Request) -> MonitorScheduler:
    """The process-wide MonitorScheduler created in the lifespan context."""
    return request.app.state.scheduler


CurrentUser = Annotated[User, Depends(get_current_user)]
SchedulerDep = Annotated[MonitorScheduler, Depends(get_scheduler)]
