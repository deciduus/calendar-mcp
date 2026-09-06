"""Tools for discovering and creating calendars."""

from __future__ import annotations

from typing import Optional

from calendar_mcp import server as srv
from calendar_mcp.models import CalendarInfo, CalendarListResult


@srv.server.tool(
    name="list_calendars",
    title="List calendars",
    annotations=srv.READ_ONLY,
)
async def list_calendars(
    min_access_role: Optional[str] = None,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> CalendarListResult:
    """List the calendars the signed-in user can see, with their IDs and timezones.

    Start here whenever the user mentions a calendar other than their own: the
    `id` values returned are what every other tool's `calendar_id` expects.

    Args:
        min_access_role: Only return calendars where the user has at least this
            role: 'freeBusyReader', 'reader', 'writer' or 'owner'. Omit for all.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> CalendarListResult:
        creds = provider.get(account)
        response = srv.calendar_actions.find_calendars(creds, min_access_role=min_access_role)
        if response is None:
            raise srv._no_result("Listing calendars")
        calendars = [CalendarInfo.from_entry(entry) for entry in response.items]
        return CalendarListResult(count=len(calendars), calendars=calendars)

    return await srv._run(work)


@srv.server.tool(
    name="create_calendar",
    title="Create a calendar",
    annotations=srv.WRITE,
)
async def create_calendar(
    summary: str,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> CalendarInfo:
    """Create a new secondary calendar owned by the user.

    Args:
        summary: Name for the new calendar, e.g. 'Client work'.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> CalendarInfo:
        creds = provider.get(account)
        created = srv.calendar_actions.create_calendar(creds, summary=summary)
        if created is None:
            raise srv._no_result("Creating the calendar")
        # Drop any cached timezone under this brand-new ID.
        srv._forget_calendar_timezone(created.id)
        return CalendarInfo.from_entry(created)

    return await srv._run(work)
