"""User registration."""

import structlog
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.database import DbSession
from app.models import User
from app.schemas import UserCreate, UserRead

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, session: DbSession) -> User:
    """Register a user and return their generated API key.

    The key is only shown here — store it; all monitor endpoints require it
    in the ``X-API-Key`` header.
    """
    existing = await session.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )
    user = User(email=payload.email)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    logger.info("user_created", user_id=user.id)
    return user
