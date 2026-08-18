"""MCP surface: tool exposure, bearer auth, tenant isolation, the SSRF guard.

Drives the real mounted sub-application over ASGI, so a break in the mount or
in bearer authentication fails here rather than in production.
"""

import pytest
from fastmcp.exceptions import ToolError

from app.models import CheckResult, Monitor, User, utcnow

VALID_MONITOR = {"url": "https://example.com/health", "interval_seconds": 300}

EXPECTED_TOOLS = {
    "list_monitors",
    "get_monitor",
    "get_monitor_stats",
    "get_monitor_results",
    "create_monitor",
    "update_monitor",
    "delete_monitor",
}


async def _create(mcp, **overrides):
    result = await mcp.call_tool("create_monitor", {**VALID_MONITOR, **overrides})
    return result.data


async def _add_foreign_monitor(session_factory, email: str) -> int:
    """Persist a monitor owned by somebody else; return its id."""
    async with session_factory() as session:
        other = User(email=email)
        monitor = Monitor(
            user=other, url="https://other.example.com", interval_seconds=60
        )
        session.add(other)
        session.add(monitor)
        await session.commit()
        return monitor.id


# --- discovery & auth -----------------------------------------------------

async def test_lists_every_tool(mcp_session) -> None:
    async with mcp_session() as mcp:
        tools = await mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_tools_describe_themselves(mcp_session) -> None:
    """Descriptions are what the model reads to choose a tool, so they must exist."""
    async with mcp_session() as mcp:
        for tool in await mcp.list_tools():
            assert tool.description, f"{tool.name} has no description"


async def test_destructive_tool_is_annotated(mcp_session) -> None:
    """Clients rely on these hints to decide what needs confirming."""
    async with mcp_session() as mcp:
        tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["delete_monitor"].annotations.destructiveHint is True
    assert tools["list_monitors"].annotations.readOnlyHint is True


async def test_unknown_api_key_is_rejected(mcp_session) -> None:
    """A bearer token that is not a live API key never reaches a tool.

    The failure surfaces as a 401 during connection setup, which anyio wraps in
    an ExceptionGroup — hence matching against ``repr`` rather than ``str``.
    """
    with pytest.raises(Exception) as exc_info:
        async with mcp_session("not-a-real-key"):
            pass
    assert "401" in repr(exc_info.value)


# --- create ---------------------------------------------------------------

async def test_create_monitor_schedules_job(mcp_session, scheduler_spy) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(mcp)
    assert str(monitor.url) == "https://example.com/health"
    assert monitor.is_active is True
    assert scheduler_spy.synced == [(monitor.id, True, 300)]


async def test_create_monitor_accepts_full_configuration(mcp_session) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(
            mcp,
            method="HEAD",
            expected_status=204,
            headers={"X-Token": "abc"},
            is_active=False,
        )
    assert monitor.method == "HEAD"
    assert monitor.expected_status == 204
    assert monitor.headers == {"X-Token": "abc"}
    assert monitor.is_active is False


async def test_interval_below_minimum_is_a_tool_error(mcp_session) -> None:
    """The router's 422 must reach the model as a message it can act on."""
    async with mcp_session() as mcp:
        with pytest.raises(ToolError, match="60"):
            await _create(mcp, interval_seconds=30)


async def test_quota_is_enforced(mcp_session, mcp_settings) -> None:
    mcp_settings.max_active_monitors_per_user = 1
    async with mcp_session() as mcp:
        await _create(mcp)
        with pytest.raises(ToolError, match="limit reached"):
            await _create(mcp, url="https://example.com/second")


# --- SSRF guard -----------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://localhost:8000/",
    ],
)
async def test_create_refuses_non_public_targets(mcp_session, url: str) -> None:
    async with mcp_session() as mcp:
        with pytest.raises(ToolError, match="private or reserved"):
            await _create(mcp, url=url)


async def test_update_refuses_non_public_targets(mcp_session) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(mcp)
        with pytest.raises(ToolError, match="private or reserved"):
            await mcp.call_tool(
                "update_monitor",
                {"monitor_id": monitor.id, "url": "http://169.254.169.254/"},
            )


async def test_create_refuses_non_http_schemes(mcp_session) -> None:
    async with mcp_session() as mcp:
        with pytest.raises(ToolError, match="http or https"):
            await _create(mcp, url="ftp://example.com/file")


# --- read & tenant isolation ----------------------------------------------

async def test_list_returns_only_your_own_monitors(
    mcp_session, session_factory
) -> None:
    await _add_foreign_monitor(session_factory, "mcp-other@example.com")
    async with mcp_session() as mcp:
        await _create(mcp)
        listed = (await mcp.call_tool("list_monitors", {})).data
    assert [str(m.url) for m in listed] == ["https://example.com/health"]


async def test_another_users_monitor_is_not_found(
    mcp_session, session_factory
) -> None:
    """404-not-403: an id you don't own is indistinguishable from a missing one."""
    stranger_id = await _add_foreign_monitor(
        session_factory, "mcp-other2@example.com"
    )
    async with mcp_session() as mcp:
        for tool in (
            "get_monitor",
            "get_monitor_stats",
            "get_monitor_results",
            "delete_monitor",
        ):
            with pytest.raises(ToolError, match="not found"):
                await mcp.call_tool(tool, {"monitor_id": stranger_id})


async def test_get_monitor_returns_configuration(mcp_session) -> None:
    async with mcp_session() as mcp:
        created = await _create(mcp)
        fetched = (await mcp.call_tool("get_monitor", {"monitor_id": created.id})).data
    assert fetched.id == created.id
    assert str(fetched.url) == "https://example.com/health"


async def test_results_are_newest_first(mcp_session, session_factory) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(mcp)
        now = utcnow()
        async with session_factory() as session:
            for offset, success in enumerate([True, False, True]):
                session.add(
                    CheckResult(
                        monitor_id=monitor.id,
                        status_code=200 if success else 500,
                        response_time_ms=10.0 * (offset + 1),
                        success=success,
                        checked_at=now.replace(microsecond=offset),
                    )
                )
            await session.commit()

        results = (
            await mcp.call_tool("get_monitor_results", {"monitor_id": monitor.id})
        ).data

    assert len(results) == 3
    assert [r.checked_at for r in results] == sorted(
        (r.checked_at for r in results), reverse=True
    )


async def test_stats_aggregate_the_window(mcp_session, session_factory) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(mcp)
        async with session_factory() as session:
            for success in [True, True, False, True]:
                session.add(
                    CheckResult(
                        monitor_id=monitor.id,
                        status_code=200 if success else 500,
                        response_time_ms=100.0,
                        success=success,
                    )
                )
            await session.commit()

        stats = (
            await mcp.call_tool("get_monitor_stats", {"monitor_id": monitor.id})
        ).data

    assert stats.total_checks == 4
    assert stats.uptime_percent == 75.0
    assert stats.avg_response_time_ms == 100.0
    assert stats.incident_count == 1


# --- update & delete ------------------------------------------------------

async def test_update_changes_only_supplied_fields(mcp_session, scheduler_spy) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(mcp, headers={"X-Token": "abc"})
        updated = (
            await mcp.call_tool(
                "update_monitor", {"monitor_id": monitor.id, "interval_seconds": 600}
            )
        ).data
    assert updated.interval_seconds == 600
    assert updated.headers == {"X-Token": "abc"}
    assert str(updated.url) == "https://example.com/health"
    assert scheduler_spy.synced[-1] == (monitor.id, True, 600)


async def test_pausing_a_monitor_syncs_it_inactive(mcp_session, scheduler_spy) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(mcp)
        updated = (
            await mcp.call_tool(
                "update_monitor", {"monitor_id": monitor.id, "is_active": False}
            )
        ).data
    assert updated.is_active is False
    assert scheduler_spy.synced[-1] == (monitor.id, False, 300)


async def test_delete_removes_monitor_and_unschedules(
    mcp_session, scheduler_spy
) -> None:
    async with mcp_session() as mcp:
        monitor = await _create(mcp)
        await mcp.call_tool("delete_monitor", {"monitor_id": monitor.id})
        remaining = (await mcp.call_tool("list_monitors", {})).data
    assert scheduler_spy.removed == [monitor.id]
    assert remaining == []
