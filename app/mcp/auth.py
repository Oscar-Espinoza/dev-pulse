"""Bearer-token authentication for the MCP surface.

MCP's authorization model is an OAuth 2.1 ``Authorization: Bearer <token>``
header, whereas the REST API uses ``X-API-Key`` (``app/dependencies.py``). The
two are reconciled here without inventing a second credential: the bearer token
*is* the DevPulse API key. A client configured with
``Authorization: Bearer <api-key>`` authenticates immediately, which is all
Claude Code, Cursor and VS Code need.

Chat clients that can only speak full OAuth (notably ChatGPT) need an
authorization server on top of this; that arrives in a later phase and slots in
by replacing the verifier, not by changing what a credential means.
"""

from collections.abc import Callable

import structlog
from fastmcp.server.auth import AccessToken, TokenVerifier
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import User

logger = structlog.stdlib.get_logger(__name__)

READ_SCOPE = "devpulse:read"
WRITE_SCOPE = "devpulse:write"
ALL_SCOPES = [READ_SCOPE, WRITE_SCOPE]

SessionFactoryGetter = Callable[[], async_sessionmaker[AsyncSession]]


class ApiKeyVerifier(TokenVerifier):
    """Resolves a bearer token by looking it up as a DevPulse API key.

    Takes a *getter* rather than a session factory because the factory is built
    in the application lifespan, long after this object is constructed in
    ``create_app()``.
    """

    def __init__(self, session_factory_getter: SessionFactoryGetter) -> None:
        super().__init__()
        self._get_session_factory = session_factory_getter

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an AccessToken for a known API key, else None (→ 401)."""
        session_factory = self._get_session_factory()
        async with session_factory() as session:
            result = await session.execute(select(User).where(User.api_key == token))
            user = result.scalar_one_or_none()

        if user is None:
            logger.info("mcp_auth_rejected")
            return None

        # An API key carries the user's full authority, so it gets every scope.
        # Per-tool scope checks still run, and start discriminating once tokens
        # can be minted with a narrower grant.
        return AccessToken(
            token=token,
            client_id=f"api-key:{user.id}",
            scopes=list(ALL_SCOPES),
            expires_at=None,
            subject=str(user.id),
            claims={"user_id": user.id, "email": user.email},
        )
