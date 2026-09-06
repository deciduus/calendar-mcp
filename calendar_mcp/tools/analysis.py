"""Tools that summarise a calendar rather than listing it."""

from __future__ import annotations

from typing import Optional

from calendar_mcp import server as srv
from calendar_mcp.models import (
    BusynessResult,
    DayBusyness,
    ProjectedEventsResult,
    ProjectedOccurrence,
)


@srv.server.tool(
    name="analyze_busyness",
    title="Analyze calendar load",
    annotations=srv.READ_ONLY,
)
async def analyze_busyness(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> BusynessResult:
    """Summarise how loaded each day is: event count and total scheduled minutes.

    Use this to answer "how busy is my week" without listing every event.

    Args:
        time_min: Start of the window, ISO 8601.
        time_max: End of the window, ISO 8601.
        calendar_id: Calendar to analyse.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> BusynessResult:
        creds = provider.get(account)
        start = srv._require(
            srv.parse_datetime(time_min, "time_min", creds, calendar_id), "time_min"
        )
        end = srv._require(srv.parse_datetime(time_max, "time_max", creds, calendar_id), "time_max")
        stats = srv.calendar_actions.get_busyness_analysis(
            credentials=creds,
            time_min=start,
            time_max=end,
            calendar_id=calendar_id,
        )
        if stats is None:
            raise srv._no_result("Analysing busyness")
        days = [
            DayBusyness(
                date=day.isoformat() if hasattr(day, "isoformat") else str(day),
                event_count=int(values.get("event_count", 0)),
                total_duration_minutes=float(values.get("total_duration_minutes", 0.0)),
            )
            for day, values in stats.items()
        ]
        return BusynessResult(
            calendar_id=calendar_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
            days=days,
            total_events=sum(d.event_count for d in days),
            total_duration_minutes=sum(d.total_duration_minutes for d in days),
        )

    return await srv._run(work)


@srv.server.tool(
    name="project_recurring_events",
    title="Project recurring events",
    annotations=srv.READ_ONLY,
)
async def project_recurring_events(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    event_query: Optional[str] = None,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> ProjectedEventsResult:
    """Compute future occurrences of recurring events from their recurrence rules.

    Unlike `find_events`, this expands the RRULEs locally, so it reaches past the
    horizon Google materialises instances for -- useful for "when do my birthdays
    / standups land next year".

    Args:
        time_min: Start of the projection window, ISO 8601.
        time_max: End of the projection window, ISO 8601.
        calendar_id: Calendar whose recurring events should be projected.
        event_query: Only project recurring events matching this text, e.g. 'Birthday'.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> ProjectedEventsResult:
        creds = provider.get(account)
        start = srv._require(
            srv.parse_datetime(time_min, "time_min", creds, calendar_id), "time_min"
        )
        end = srv._require(srv.parse_datetime(time_max, "time_max", creds, calendar_id), "time_max")
        occurrences = srv.calendar_actions.get_projected_recurring_events(
            credentials=creds,
            time_min=start,
            time_max=end,
            calendar_id=calendar_id,
            event_query=event_query,
        )
        projected = [
            ProjectedOccurrence(
                event_id=item.original_event_id,
                summary=item.original_summary,
                start=item.occurrence_start.isoformat(),
                end=item.occurrence_end.isoformat(),
            )
            for item in (occurrences or [])
        ]
        return ProjectedEventsResult(
            calendar_id=calendar_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
            count=len(projected),
            occurrences=projected,
        )

    return await srv._run(work)
