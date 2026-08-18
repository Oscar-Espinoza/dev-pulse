"""The DevPulse MCP server: the seven monitor operations, as LLM tools.

Each tool delegates to the matching function in ``app/routers/monitors.py``
rather than reimplementing it. Those functions are plain async callables — the
``Annotated[..., Depends(...)]`` hints only mean something to FastAPI's router,
so passing real objects positionally works — and going through them is what
keeps the per-user quota, the minimum-interval rule and the 404-not-403
ownership behaviour identical on both surfaces. The MCP layer adds exactly two
things of its own: the SSRF guard (see ``app/services/url_guard.py``) and the
translation of ``HTTPException`` into ``ToolError``.

Tool docstrings are not comments — they are the descriptions the model reads
when deciding which tool to call, so they describe the *question each tool
answers* rather than the endpoint it wraps.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider, require_scopes
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.mcp.auth import READ_SCOPE, WRITE_SCOPE
from app.models import User
from app.routers import monitors as monitor_routes
from app.schemas import (
    CheckResultRead,
    HttpMethod,
    MonitorCreate,
    MonitorRead,
    MonitorStats,
    MonitorUpdate,
)
from app.services.scheduler import MonitorScheduler
from app.services.url_guard import UnsafeUrlError, assert_public_url

logger = structlog.stdlib.get_logger(__name__)

SessionFactoryGetter = Callable[[], async_sessionmaker[AsyncSession]]
SchedulerGetter = Callable[[], MonitorScheduler]
SettingsGetter = Callable[[], Settings]

INSTRUCTIONS = """\
DevPulse watches URLs on a schedule and records every check.

A *monitor* is a URL plus the method, expected status code and interval to
check it on. A *check result* is one recorded outcome of that check.

To answer "is it up?" or "how fast is it?", prefer get_monitor_stats — it
aggregates a whole time window server-side. Reach for get_monitor_results only
when the individual failures matter, such as reading error messages.
"""


def build_mcp_server(
    *,
    session_factory_getter: SessionFactoryGetter,
    scheduler_getter: SchedulerGetter,
    settings_getter: SettingsGetter = get_settings,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Assemble the MCP server.

    Takes getters rather than the resources themselves: the session factory and
    scheduler are built in the application lifespan, which runs after
    ``create_app()`` has already constructed this server. Tests supply their own
    getters, which is also why settings are reached through one — MCP tools sit
    outside the request cycle, so FastAPI's ``dependency_overrides`` cannot
    reach them.
    """
    mcp = FastMCP(
        name="DevPulse",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        auth=auth,
    )

    @asynccontextmanager
    async def acting_user() -> AsyncIterator[tuple[AsyncSession, User]]:
        """Open a session, resolve the authenticated caller, surface errors.

        The verifier already proved the token; this reloads the User so it is
        attached to the session the route functions will use.
        """
        token = get_access_token()
        if token is None:
            raise ToolError("Not authenticated.")
        user_id = token.claims.get("user_id")
        if user_id is None:
            raise ToolError("Token is missing a user identity.")

        session_factory = session_factory_getter()
        async with session_factory() as session:
            user = await session.get(User, int(user_id))
            if user is None:
                raise ToolError("The account for this token no longer exists.")
            try:
                yield session, user
            except HTTPException as exc:
                # The route functions speak HTTP; tools speak ToolError. Detail
                # strings are already written for humans, so they pass through.
                raise ToolError(str(exc.detail)) from exc

    async def guard_url(url: str) -> None:
        """Refuse non-public targets, as a ToolError the model can act on."""
        try:
            await assert_public_url(url)
        except UnsafeUrlError as exc:
            raise ToolError(str(exc)) from exc

    # --- read tools ---

    @mcp.tool(
        auth=require_scopes(READ_SCOPE),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def list_monitors() -> list[MonitorRead]:
        """List every monitor belonging to you, with its full configuration.

        Start here when you do not already know a monitor's id.
        """
        async with acting_user() as (session, user):
            monitors = await monitor_routes.list_monitors(user, session)
            return [MonitorRead.model_validate(m) for m in monitors]

    @mcp.tool(
        auth=require_scopes(READ_SCOPE),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def get_monitor(monitor_id: int) -> MonitorRead:
        """Fetch one monitor's configuration by id."""
        async with acting_user() as (session, user):
            monitor = await monitor_routes.get_monitor(monitor_id, user, session)
            return MonitorRead.model_validate(monitor)

    @mcp.tool(
        auth=require_scopes(READ_SCOPE),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def get_monitor_stats(
        monitor_id: int,
        hours: Annotated[
            int,
            Field(ge=1, le=8760, description="Size of the window, ending now."),
        ] = 24,
    ) -> MonitorStats:
        """Summarise a monitor's health over a time window.

        Returns uptime percentage, average response time across successful
        checks, and the number of incidents (runs of consecutive failures).
        This is the right tool for "is it up?", "how fast is it?" and "how
        often did it break?" — it aggregates in the database rather than
        returning raw checks.
        """
        async with acting_user() as (session, user):
            return await monitor_routes.monitor_stats(
                monitor_id, user, session, hours
            )

    @mcp.tool(
        auth=require_scopes(READ_SCOPE),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def get_monitor_results(
        monitor_id: int,
        limit: Annotated[
            int, Field(ge=1, le=500, description="How many checks to return.")
        ] = 50,
    ) -> list[CheckResultRead]:
        """Return a monitor's most recent individual check results, newest first.

        Use this when the specific failures matter — error messages, exact
        timings, when a blip started. For an overall verdict, prefer
        get_monitor_stats.
        """
        async with acting_user() as (session, user):
            results = await monitor_routes.list_results(
                monitor_id, user, session, limit
            )
            return [CheckResultRead.model_validate(r) for r in results]

    # --- write tools ---

    @mcp.tool(
        auth=require_scopes(WRITE_SCOPE),
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False),
    )
    async def create_monitor(
        url: Annotated[str, Field(description="Public http(s) URL to check.")],
        interval_seconds: Annotated[
            int, Field(description="Seconds between checks.")
        ],
        method: HttpMethod = "GET",
        expected_status: Annotated[int, Field(ge=100, le=599)] = 200,
        headers: Annotated[
            dict[str, str] | None,
            Field(description="Extra request headers, e.g. auth for a private API."),
        ] = None,
        is_active: Annotated[
            bool, Field(description="Whether to start checking immediately.")
        ] = True,
    ) -> MonitorRead:
        """Create a monitor and begin checking the URL on the given interval.

        The target must be publicly reachable; private, loopback and
        cloud-metadata addresses are refused. Both the interval floor and the
        cap on simultaneously active monitors are enforced, and exceeding
        either returns an error explaining the limit.
        """
        await guard_url(url)
        payload = MonitorCreate(
            url=url,
            method=method,
            expected_status=expected_status,
            interval_seconds=interval_seconds,
            headers=headers or {},
            is_active=is_active,
        )
        async with acting_user() as (session, user):
            monitor = await monitor_routes.create_monitor(
                payload, user, session, scheduler_getter(), settings_getter()
            )
            return MonitorRead.model_validate(monitor)

    @mcp.tool(
        auth=require_scopes(WRITE_SCOPE),
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
    )
    async def update_monitor(
        monitor_id: int,
        url: str | None = None,
        method: HttpMethod | None = None,
        expected_status: Annotated[int | None, Field(ge=100, le=599)] = None,
        interval_seconds: int | None = None,
        headers: dict[str, str] | None = None,
        is_active: Annotated[
            bool | None,
            Field(description="Set false to pause checking without deleting."),
        ] = None,
    ) -> MonitorRead:
        """Change part of a monitor's configuration; omitted fields stay as they are.

        The scheduled job converges immediately — a new interval takes effect
        without a restart, and setting is_active false stops checks while
        keeping the monitor and its history.
        """
        if url is not None:
            await guard_url(url)
        payload = MonitorUpdate.model_validate(
            {
                key: value
                for key, value in {
                    "url": url,
                    "method": method,
                    "expected_status": expected_status,
                    "interval_seconds": interval_seconds,
                    "headers": headers,
                    "is_active": is_active,
                }.items()
                if value is not None
            }
        )
        async with acting_user() as (session, user):
            monitor = await monitor_routes.update_monitor(
                monitor_id,
                payload,
                user,
                session,
                scheduler_getter(),
                settings_getter(),
            )
            return MonitorRead.model_validate(monitor)

    @mcp.tool(
        auth=require_scopes(WRITE_SCOPE),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False
        ),
    )
    async def delete_monitor(monitor_id: int) -> str:
        """Permanently delete a monitor and its entire check history.

        This cannot be undone. To stop checks while keeping the history, call
        update_monitor with is_active false instead.
        """
        async with acting_user() as (session, user):
            await monitor_routes.delete_monitor(
                monitor_id, user, session, scheduler_getter()
            )
            return f"Monitor {monitor_id} deleted."

    return mcp
