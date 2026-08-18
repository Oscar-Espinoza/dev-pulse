"""DevPulse as an OAuth 2.1 authorization server for its own MCP surface.

Needed because some chat clients — ChatGPT most importantly — will only talk
to a remote MCP server through OAuth. They cannot be handed a static API key
the way Claude Code or Cursor can.

The point of interest is what happens at the consent screen. A conventional
authorization server would authenticate the user with a password, and DevPulse
has none by deliberate choice (``docs/study-guide/10-auth-and-users.md``). So
the credential presented at the consent screen is the API key the user already
has. OAuth here is a *transport* for that credential, not a second identity
system: nothing new can prove who you are, and an access token is only ever a
short-lived stand-in for a key its holder already possessed.

Flow, end to end:

1. The client registers itself (RFC 7591) → ``oauth_clients``.
2. The client sends the user to ``/authorize``. We park the request in
   ``oauth_transactions`` and redirect to our own consent page.
3. The user pastes their API key and approves. We verify it, delete the
   transaction, and mint a single-use code in ``oauth_authorization_codes``.
4. The client redeems the code at ``/token`` for an access and refresh token
   in ``oauth_tokens``, and uses the access token as an MCP bearer token.
"""

import secrets
import time
from collections.abc import Callable

import structlog
from fastmcp.server.auth.auth import (
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.mcp.auth import ALL_SCOPES, READ_SCOPE, WRITE_SCOPE, ApiKeyVerifier
from app.models import (
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthToken as OAuthTokenRow,
    OAuthTransaction,
    User,
)

logger = structlog.stdlib.get_logger(__name__)

AUTH_CODE_TTL_SECONDS = 5 * 60
TRANSACTION_TTL_SECONDS = 10 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60

CONSENT_PATH = "/oauth/consent"

SessionFactoryGetter = Callable[[], async_sessionmaker[AsyncSession]]


class ConsentError(Exception):
    """The consent step could not be completed (bad key, expired request)."""


class DevPulseOAuthProvider(OAuthProvider):
    """Authorization server backed by the DevPulse database.

    Every store the OAuth machinery needs is a table, so tokens survive a
    restart of the single Render process and can genuinely be revoked.
    """

    def __init__(
        self,
        *,
        base_url: str | AnyHttpUrl,
        consent_url: str,
        session_factory_getter: SessionFactoryGetter,
    ) -> None:
        """``base_url`` is where the OAuth endpoints live (the MCP mount);
        ``consent_url`` is the page on the parent app, which sits outside it."""
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=list(ALL_SCOPES),
                default_scopes=list(ALL_SCOPES),
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self._get_session_factory = session_factory_getter
        self._consent_url = consent_url
        # Composed rather than reimplemented: "an API key may act as a bearer
        # token" is stated in exactly one place.
        self._api_keys = ApiKeyVerifier(session_factory_getter)

    def _session(self) -> AsyncSession:
        return self._get_session_factory()()

    # --- client registration ---------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._session() as session:
            row = await session.get(OAuthClient, client_id)
            if row is None:
                return None
            return OAuthClientInformationFull.model_validate(row.data)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")
        async with self._session() as session:
            data = client_info.model_dump(mode="json", exclude_none=True)
            existing = await session.get(OAuthClient, client_info.client_id)
            if existing is None:
                session.add(
                    OAuthClient(
                        client_id=client_info.client_id,
                        client_name=client_info.client_name,
                        data=data,
                    )
                )
            else:
                # RFC 7591 treats a repeat as an update, not a conflict.
                existing.client_name = client_info.client_name
                existing.data = data
            await session.commit()
        logger.info(
            "oauth_client_registered",
            client_id=client_info.client_id,
            client_name=client_info.client_name,
        )

    # --- authorization: park the request, then show consent ---------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Redirect the user to our consent page instead of issuing a code.

        The base class contract is to return a URL to send the user to. The
        request itself is stored server-side so the consent page only has to
        carry an opaque id — a tampered redirect_uri never survives the trip.
        """
        if client.client_id is None:
            raise AuthorizeError(
                error="invalid_client", error_description="Client ID is required"
            )

        scopes = list(params.scopes or ALL_SCOPES)
        if client.scope:
            allowed = set(client.scope.split())
            scopes = [scope for scope in scopes if scope in allowed]

        transaction_id = secrets.token_urlsafe(32)
        async with self._session() as session:
            session.add(
                OAuthTransaction(
                    id=transaction_id,
                    client_id=client.client_id,
                    params={
                        "redirect_uri": str(params.redirect_uri),
                        "redirect_uri_provided_explicitly": (
                            params.redirect_uri_provided_explicitly
                        ),
                        "code_challenge": params.code_challenge,
                        "scopes": scopes,
                        "state": params.state,
                        "resource": params.resource,
                    },
                    expires_at=int(time.time()) + TRANSACTION_TTL_SECONDS,
                )
            )
            await session.commit()

        return f"{self._consent_url}?txn={transaction_id}"

    async def describe_transaction(self, transaction_id: str) -> dict:
        """What the consent page needs to render: client name and scopes."""
        async with self._session() as session:
            transaction = await self._live_transaction(session, transaction_id)
            client = await session.get(OAuthClient, transaction.client_id)
            return {
                "client_name": (client.client_name if client else None)
                or transaction.client_id,
                "scopes": transaction.params.get("scopes", []),
            }

    async def complete_consent(self, transaction_id: str, api_key: str) -> str:
        """Approve a parked request with an API key; return the redirect URL.

        This is the only place a DevPulse credential is exchanged for OAuth
        state, and it is why no password is needed anywhere in this flow.
        """
        async with self._session() as session:
            transaction = await self._live_transaction(session, transaction_id)

            result = await session.execute(
                select(User).where(User.api_key == api_key.strip())
            )
            user = result.scalar_one_or_none()
            if user is None:
                logger.info("oauth_consent_rejected", client_id=transaction.client_id)
                raise ConsentError("That API key is not valid.")

            params = transaction.params
            code = secrets.token_urlsafe(32)
            session.add(
                OAuthAuthorizationCode(
                    code=code,
                    client_id=transaction.client_id,
                    user_id=user.id,
                    redirect_uri=params["redirect_uri"],
                    redirect_uri_provided_explicitly=params[
                        "redirect_uri_provided_explicitly"
                    ],
                    code_challenge=params["code_challenge"],
                    scopes=params.get("scopes", list(ALL_SCOPES)),
                    resource=params.get("resource"),
                    expires_at=int(time.time()) + AUTH_CODE_TTL_SECONDS,
                )
            )
            await session.delete(transaction)
            await session.commit()

        logger.info("oauth_consent_granted", user_id=user.id)
        return construct_redirect_uri(
            params["redirect_uri"], code=code, state=params.get("state")
        )

    async def _live_transaction(
        self, session: AsyncSession, transaction_id: str
    ) -> OAuthTransaction:
        transaction = await session.get(OAuthTransaction, transaction_id)
        if transaction is None or transaction.expires_at < time.time():
            raise ConsentError(
                "This authorization request has expired. Start again from the "
                "application you were connecting."
            )
        return transaction

    # --- authorization codes ---------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with self._session() as session:
            row = await session.get(OAuthAuthorizationCode, authorization_code)
            if row is None or row.client_id != client.client_id:
                return None
            if row.expires_at < time.time():
                await session.delete(row)
                await session.commit()
                return None
            return AuthorizationCode(
                code=row.code,
                scopes=list(row.scopes),
                expires_at=float(row.expires_at),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyHttpUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
                subject=str(row.user_id),
            )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        async with self._session() as session:
            row = await session.get(OAuthAuthorizationCode, authorization_code.code)
            if row is None:
                raise TokenError(
                    "invalid_grant", "Authorization code not found or already used."
                )
            user_id = row.user_id
            # Deleting here is what makes the code single-use; a replay of the
            # same code finds nothing and fails above.
            await session.delete(row)
            token = await self._issue_tokens(
                session,
                client_id=row.client_id,
                user_id=user_id,
                scopes=list(row.scopes),
                resource=row.resource,
            )
            await session.commit()
        logger.info("oauth_code_exchanged", user_id=user_id)
        return token

    # --- tokens ------------------------------------------------------------

    async def _issue_tokens(
        self,
        session: AsyncSession,
        *,
        client_id: str,
        user_id: int,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        session.add(
            OAuthTokenRow(
                token=access,
                kind="access",
                client_id=client_id,
                user_id=user_id,
                scopes=scopes,
                resource=resource,
                expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
            )
        )
        session.add(
            OAuthTokenRow(
                token=refresh,
                kind="refresh",
                client_id=client_id,
                user_id=user_id,
                scopes=scopes,
                resource=resource,
                expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
            )
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        async with self._session() as session:
            row = await self._live_token(session, refresh_token, kind="refresh")
            if row is None or row.client_id != client.client_id:
                return None
            return RefreshToken(
                token=row.token,
                client_id=row.client_id,
                scopes=list(row.scopes),
                expires_at=row.expires_at,
                subject=str(row.user_id),
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        requested = set(scopes) if scopes else set(refresh_token.scopes)
        if not requested.issubset(set(refresh_token.scopes)):
            raise TokenError(
                "invalid_scope",
                "Requested scopes exceed those authorized by the refresh token.",
            )

        async with self._session() as session:
            row = await self._live_token(session, refresh_token.token, kind="refresh")
            if row is None:
                raise TokenError("invalid_grant", "Refresh token is no longer valid.")
            user_id = row.user_id
            client_id = row.client_id
            resource = row.resource
            # Rotate: the presented refresh token dies with the tokens it minted.
            await session.delete(row)
            token = await self._issue_tokens(
                session,
                client_id=client_id,
                user_id=user_id,
                scopes=sorted(requested),
                resource=resource,
            )
            await session.commit()
        logger.info("oauth_token_refreshed", user_id=user_id)
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self._session() as session:
            row = await self._live_token(session, token, kind="access")
            if row is None:
                return None
            user = await session.get(User, row.user_id)
            if user is None:
                return None
            return AccessToken(
                token=row.token,
                client_id=row.client_id,
                scopes=list(row.scopes),
                expires_at=row.expires_at,
                resource=row.resource,
                subject=str(row.user_id),
                # Same claim shape the API-key verifier emits, so the tools do
                # not care which way the caller authenticated.
                claims={"user_id": row.user_id, "email": user.email},
            )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Accept an OAuth access token, or a raw API key as a bearer token.

        The fallback is what lets Claude Code and Cursor connect with a single
        header while ChatGPT goes through the full OAuth dance, against one
        server and one set of tools.
        """
        access_token = await self.load_access_token(token)
        if access_token is not None:
            return access_token
        return await self._api_keys.verify_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with self._session() as session:
            await session.execute(
                delete(OAuthTokenRow).where(OAuthTokenRow.token == token.token)
            )
            await session.commit()

    async def _live_token(
        self, session: AsyncSession, token: str, *, kind: str
    ) -> OAuthTokenRow | None:
        """Fetch an unexpired token row of the given kind, pruning it if stale."""
        row = await session.get(OAuthTokenRow, token)
        if row is None or row.kind != kind:
            return None
        if row.expires_at is not None and row.expires_at < time.time():
            await session.delete(row)
            await session.commit()
            return None
        return row


__all__ = [
    "CONSENT_PATH",
    "ConsentError",
    "DevPulseOAuthProvider",
    "READ_SCOPE",
    "WRITE_SCOPE",
]
