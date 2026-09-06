"""Tools that reason about availability across calendars.

The interval maths these build on lives in :mod:`calendar_mcp.timeutil`
(``free_windows``, ``merge_intervals``, ``subtract_intervals``), the ranking and
block-finding in :mod:`calendar_mcp.scheduling`, and the user's own constraints
in :mod:`calendar_mcp.preferences` (``working_windows``), so a new scheduling
tool should not have to reimplement any of them.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import List, Optional, Tuple

from calendar_mcp import preferences as preferences_module
from calendar_mcp import scheduling as scheduling_logic
from calendar_mcp import server as srv
from calendar_mcp.models import (
    BusyPeriod,
    CalendarBusyPeriods,
    EventCreateRequest,
    EventDateTime,
    EventInfo,
    EventResult,
    FreeBusyResult,
)
from calendar_mcp.timeutil import Interval, clip_intervals, combine, iter_days, parse_clock


@srv.server.tool(
    name="query_free_busy",
    title="Query free/busy",
    annotations=srv.READ_ONLY,
)
async def query_free_busy(
    calendar_ids: List[str],
    time_min: str,
    time_max: str,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> FreeBusyResult:
    """Get busy intervals for one or more calendars, without revealing event details.

    Works for other people's calendars addressed by email, which is how you check
    whether someone is free before proposing a time.

    Args:
        calendar_ids: Calendar IDs or attendee email addresses to check.
        time_min: Start of the window, ISO 8601 with a UTC offset.
        time_max: End of the window, ISO 8601 with a UTC offset.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> FreeBusyResult:
        creds = provider.get(account)
        start = srv._require(
            srv.parse_datetime(time_min, "time_min", creds, "primary"), "time_min"
        )
        end = srv._require(srv.parse_datetime(time_max, "time_max", creds, "primary"), "time_max")
        raw = srv.calendar_actions.find_availability(
            credentials=creds,
            time_min=start,
            time_max=end,
            calendar_ids=list(calendar_ids),
        )
        if raw is None:
            raise srv._no_result("Querying free/busy")
        calendars = []
        for cal_id, data in raw.items():
            calendars.append(
                CalendarBusyPeriods(
                    calendar_id=cal_id,
                    busy=[
                        BusyPeriod(
                            start=interval["start"].isoformat(),
                            end=interval["end"].isoformat(),
                        )
                        for interval in data.get("busy", [])
                    ],
                    errors=[
                        err.get("reason", str(err)) if isinstance(err, dict) else str(err)
                        for err in (data.get("errors") or [])
                    ],
                )
            )
        return FreeBusyResult(
            time_min=start.isoformat(),
            time_max=end.isoformat(),
            calendars=calendars,
        )

    result = await srv._run(work)
    unreadable = [c.calendar_id for c in result.calendars if c.errors]
    if unreadable:
        await srv._warn(ctx, f"Free/busy could not be read for: {', '.join(unreadable)}.")
    return result


# ---------------------------------------------------------------------------
# schedule_mutual
#
# Two ways to place the meeting, with the same contract:
#
# * When the preferences reduce to one plain daily clock window over every day
#   the search touches -- no lunch, no buffer, no days off, no timezone skew --
#   Google's own first-slot search in ``calendar_actions`` is exactly right, and
#   is used unchanged.
# * Otherwise the constraints cannot be expressed as a single 'HH:MM'-'HH:MM'
#   pair, so the slot is found here: working windows from the preferences, minus
#   everyone's busy time, minus the buffer.
# ---------------------------------------------------------------------------


def _clocks_agree(zone, *moments: datetime) -> bool:
    """True when ``zone``'s wall clock matches each moment's own at that instant."""
    return all(moment.astimezone(zone).utcoffset() == moment.utcoffset() for moment in moments)


def _uniform_clock_window(
    prefs: "preferences_module.Preferences",
    time_min: datetime,
    time_max: datetime,
    zone,
) -> Optional[Tuple[dt_time, dt_time]]:
    """The single daily window the preferences reduce to, or ``None``.

    ``None`` means the constraints need the richer search: a lunch break, a
    buffer, a day off inside the window, or hours that differ by weekday.
    """
    if prefs.lunch or prefs.buffer_minutes:
        return None
    span: Optional[Tuple[str, str]] = None
    for day in iter_days(time_min, time_max, zone):
        day_spans = prefs.spans_for(day.weekday())
        if len(day_spans) != 1:
            return None
        if span is None:
            span = day_spans[0]
        elif day_spans[0] != span:
            return None
    if span is None:
        return None
    start_clock = parse_clock(span[0], "working hours start")
    end_clock = parse_clock(span[1], "working hours end")
    if start_clock is None or end_clock is None:  # pragma: no cover - validated on write
        return None
    return start_clock, end_clock


def _narrow(day_windows: List[Interval], start_clock, end_clock, zone) -> List[Interval]:
    """Clips each working window to an explicit ``HH:MM`` bound on its own day."""
    if start_clock is None and end_clock is None:
        return day_windows
    narrowed: List[Interval] = []
    for window in day_windows:
        day = window[0].astimezone(zone).date()
        low = combine(day, start_clock, zone) if start_clock else window[0]
        high = combine(day, end_clock, zone) if end_clock else window[1]
        narrowed.extend(clip_intervals([window], (low, high)))
    return narrowed


@srv.server.tool(
    name="schedule_mutual",
    title="Find a mutual slot and schedule",
    annotations=srv.WRITE,
)
async def schedule_mutual(
    attendee_calendar_ids: List[str],
    time_min: str,
    time_max: str,
    duration_minutes: int,
    summary: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    organizer_calendar_id: str = "primary",
    working_hours_start: Optional[str] = None,
    working_hours_end: Optional[str] = None,
    send_notifications: bool = True,
    respect_preferences: bool = True,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> EventResult:
    """Find the first slot where everyone is free, then book the meeting there.

    Reads each attendee's free/busy inside the window, picks the earliest gap
    that fits `duration_minutes`, and creates the event with all of them invited.
    Fails with an error if no common slot exists -- widen the window or shorten
    the meeting and try again.

    By default the search obeys the user's own settings from `get_preferences`:
    their working hours per weekday (so nothing lands on a day off), their lunch
    break, and the buffer they want around meetings. `working_hours_start` and
    `working_hours_end` narrow that further; they never widen it.

    Args:
        attendee_calendar_ids: Attendee email addresses whose availability matters.
        time_min: Earliest the meeting may start, ISO 8601 with a UTC offset.
        time_max: Latest the meeting may end, ISO 8601 with a UTC offset.
        duration_minutes: Length of the meeting in minutes.
        summary: Title for the meeting.
        description: Optional agenda or notes.
        location: Optional location or meeting link.
        organizer_calendar_id: Calendar the meeting is created on.
        working_hours_start: Optional daily earliest start, 'HH:MM' local to the
            calendar, e.g. '09:00'.
        working_hours_end: Optional daily latest end, 'HH:MM', e.g. '17:00'.
        send_notifications: Whether Google emails the attendees. Default true.
        respect_preferences: Obey the user's saved working hours, lunch and
            buffer. Set false to search the whole window instead.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)
    problems: List[str] = []

    def work() -> EventResult:
        if duration_minutes <= 0:
            raise srv.CalendarToolError("duration_minutes must be greater than zero.")
        if not attendee_calendar_ids:
            raise srv.CalendarToolError(
                "attendee_calendar_ids must contain at least one address."
            )
        explicit_start = srv._parse_clock(working_hours_start, "working_hours_start")
        explicit_end = srv._parse_clock(working_hours_end, "working_hours_end")

        creds = provider.get(account)
        start = srv._require(
            srv.parse_datetime(time_min, "time_min", creds, organizer_calendar_id), "time_min"
        )
        end = srv._require(
            srv.parse_datetime(time_max, "time_max", creds, organizer_calendar_id), "time_max"
        )
        if end - start < timedelta(minutes=duration_minutes):
            raise srv.CalendarToolError(
                "The search window is shorter than duration_minutes; widen time_min/time_max."
            )

        prefs = preferences_module.load() if respect_preferences else None
        uniform = None
        zone = None
        if prefs is not None:
            zone = prefs.tzinfo(None) or srv._default_tzinfo(creds, organizer_calendar_id)
            if _clocks_agree(zone, start, end):
                uniform = _uniform_clock_window(prefs, start, end, zone)

        if prefs is None or uniform is not None:
            clock_start, clock_end = (uniform or (None, None))
            # The tighter of the two bounds wins: explicit args narrow, never widen.
            if explicit_start and (clock_start is None or explicit_start > clock_start):
                clock_start = explicit_start
            if explicit_end and (clock_end is None or explicit_end < clock_end):
                clock_end = explicit_end
            return _book_first_slot(
                creds, start, end, clock_start, clock_end, prefs is not None
            )

        return _book_within_preferences(creds, prefs, zone, start, end, explicit_start, explicit_end)

    def _book_first_slot(creds, start, end, clock_start, clock_end, honoured) -> EventResult:
        """Books via Google's own first-slot search in ``calendar_actions``."""
        placeholder = EventDateTime(dateTime=start)
        details = EventCreateRequest(
            summary=summary,
            start=placeholder,
            end=placeholder,
            description=description,
            location=location,
        )
        created = srv.calendar_actions.find_mutual_availability_and_schedule(
            credentials=creds,
            attendee_calendar_ids=list(attendee_calendar_ids),
            time_min=start,
            time_max=end,
            duration_minutes=duration_minutes,
            event_details=details,
            organizer_calendar_id=organizer_calendar_id,
            working_hours_start=clock_start,
            working_hours_end=clock_end,
            send_notifications=send_notifications,
        )
        if created is None:
            raise _no_slot(start, end, honoured)
        return _result(created)

    def _book_within_preferences(
        creds, prefs, zone, start, end, explicit_start, explicit_end
    ) -> EventResult:
        """Finds the slot here, because the constraints are richer than one clock range."""
        windows = _narrow(
            preferences_module.working_windows(prefs, start, end, zone),
            explicit_start,
            explicit_end,
            zone,
        )
        if not windows:
            raise srv.CalendarToolError(
                "That window contains no working time at all (see get_preferences). "
                "Widen time_min/time_max, or pass respect_preferences=false."
            )

        # The organiser has to be free too, not just the attendees.
        lookup = list(dict.fromkeys([organizer_calendar_id, *attendee_calendar_ids]))
        raw = srv.calendar_actions.find_availability(
            credentials=creds, time_min=start, time_max=end, calendar_ids=lookup
        )
        if raw is None:
            raise srv._no_result("Querying free/busy")
        busy: List[Interval] = []
        for cal_id, data in raw.items():
            for error in data.get("errors") or []:
                reason = error.get("reason", str(error)) if isinstance(error, dict) else str(error)
                problems.append(f"{cal_id}: {reason}")
            for interval in data.get("busy", []):
                busy.append((interval["start"], interval["end"]))

        blocks = scheduling_logic.candidate_blocks(
            windows,
            busy,
            min_block_minutes=duration_minutes,
            buffer_minutes=prefs.buffer_minutes,
        )
        if not blocks:
            raise _no_slot(start, end, True)
        slot_start = blocks[0][0]
        slot_end = slot_start + timedelta(minutes=duration_minutes)

        invitees = [address for address in attendee_calendar_ids if "@" in address]
        details = EventCreateRequest(
            summary=summary,
            start=EventDateTime(dateTime=slot_start),
            end=EventDateTime(dateTime=slot_end),
            description=description,
            location=location,
            attendees=invitees or None,
        )
        created = srv.calendar_actions.create_event(
            credentials=creds,
            event_data=details,
            calendar_id=organizer_calendar_id,
            send_notifications=send_notifications,
        )
        if created is None:
            raise srv._no_result("Creating the meeting")
        return _result(created)

    def _no_slot(start, end, honoured) -> srv.CalendarToolError:
        hint = (
            " Widen the window, shorten the meeting, or relax the working-hours bounds."
            if not honoured
            else " Widen the window, shorten the meeting, or pass respect_preferences=false "
            "to ignore the saved working hours, lunch and buffer."
        )
        return srv.CalendarToolError(
            f"No {duration_minutes}-minute slot is free for everyone between "
            f"{start.isoformat()} and {end.isoformat()}.{hint}"
        )

    def _result(created) -> EventResult:
        info = EventInfo.from_event(created)
        return EventResult(
            calendar_id=organizer_calendar_id,
            event=info,
            message=(
                f"Booked '{summary}' at {info.start} with "
                f"{len(attendee_calendar_ids)} attendee(s)."
            ),
        )

    result = await srv._run(work)
    if problems:
        await srv._warn(
            ctx,
            "Free/busy could not be read for: " + "; ".join(problems)
            + ". Those calendars were treated as free.",
        )
    return result
