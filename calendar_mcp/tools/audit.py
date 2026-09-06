"""The ``time_audit`` tool: a retrospective on where the user's time went.

The Google call here is deliberately thin -- fetch the events, normalise them,
and hand the list to :func:`calendar_mcp.audit.build_audit`, which does all the
arithmetic offline and is tested without a network.
"""

from __future__ import annotations

from typing import List, Optional

try:  # Python 3.8+ typing shim; the SDK reads this off the signature.
    from typing import Literal
except ImportError:  # pragma: no cover - 3.8+ always has it
    Literal = None  # type: ignore[assignment]

from calendar_mcp import audit as audit_logic
from calendar_mcp import preferences as preferences_module
from calendar_mcp import server as srv
from calendar_mcp.models import TimeAuditResult

#: Fetched per calendar. Google caps a single events.list page at 2500.
MAX_EVENTS_PER_CALENDAR = 2500


@srv.server.tool(
    name="time_audit",
    title="Audit where time went",
    annotations=srv.READ_ONLY,
)
async def time_audit(
    time_min: str,
    time_max: str,
    calendar_ids: Optional[List[str]] = None,
    group_by: Literal["day", "week"] = "week",
    include_all_day: bool = False,
    include_declined: bool = False,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> TimeAuditResult:
    """Report where the user's time went: meeting hours, who with, and what focus time is left.

    Answers "how much of my week is meetings?", "who am I spending my time
    with?" and "where did my focus time go?" in one pass. Working hours, lunch,
    buffer and the minimum focus block come from the saved preferences
    (see get_preferences), so the percentages reflect this user's actual day.

    Declined meetings, events marked 'free', and all-day entries are excluded by
    default; the 'excluded' field says how many were skipped.

    Args:
        time_min: Start of the window to audit, ISO 8601.
        time_max: End of the window to audit, ISO 8601.
        calendar_ids: Calendars to include. Defaults to ['primary'].
        group_by: Break the window down by 'day' or by ISO 'week'.
        include_all_day: Count all-day events as meeting time.
        include_declined: Count meetings the user declined.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)
    calendars = [str(item) for item in (calendar_ids or []) if str(item).strip()] or ["primary"]

    def work() -> TimeAuditResult:
        creds = provider.get(account)
        primary = calendars[0]
        start = srv._require(srv.parse_datetime(time_min, "time_min", creds, primary), "time_min")
        end = srv._require(srv.parse_datetime(time_max, "time_max", creds, primary), "time_max")
        if end <= start:
            raise srv.CalendarToolError("time_max must be after time_min.")

        prefs = preferences_module.load()
        tzinfo = prefs.tzinfo(srv._default_tzinfo(creds, primary))

        events: List[audit_logic.AuditEvent] = []
        unreadable: List[str] = []
        for calendar_id in calendars:
            response = srv.calendar_actions.find_events(
                credentials=creds,
                calendar_id=calendar_id,
                time_min=start,
                time_max=end,
                max_results=MAX_EVENTS_PER_CALENDAR,
                single_events=True,
                order_by="startTime",
            )
            if response is None:
                unreadable.append(calendar_id)
                continue
            for raw in getattr(response, "items", None) or []:
                normalised = audit_logic.event_from_google(raw, tzinfo, calendar_id=calendar_id)
                if normalised is not None:
                    events.append(normalised)

        if unreadable and len(unreadable) == len(calendars):
            raise srv._no_result("Auditing time")

        result = audit_logic.build_audit(
            events,
            (start, end),
            prefs,
            tzinfo,
            group_by=group_by,
            calendar_ids=calendars,
            include_all_day=include_all_day,
            include_declined=include_declined,
        )
        result.calendar_ids = [c for c in calendars if c not in unreadable]
        return result

    result = await srv._run(work)
    skipped = [c for c in calendars if c not in result.calendar_ids]
    if skipped:
        await srv._warn(ctx, f"No events could be read for: {', '.join(skipped)}.")
    return result
