"""Tools that find what is wrong with a schedule, and what to do about it.

``detect_conflicts`` reads events across every signed-in account and reports
double-bookings, plus the transitions that are technically legal but leave less
room than the user's buffer asks for. ``suggest_reschedule`` takes one event and
proposes better times for it, ranked, without moving anything unless asked.

Both lean on :mod:`calendar_mcp.scheduling` for the actual reasoning and on
:mod:`calendar_mcp.tools.focus` for the two Google-facing helpers they share
with the focus tools.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from googleapiclient.errors import HttpError

from calendar_mcp import accounts as accounts_module
from calendar_mcp import preferences as preferences_module
from calendar_mcp import scheduling as scheduling_logic
from calendar_mcp import server as srv
from calendar_mcp.models import (
    ConflictEventRef,
    ConflictsResult,
    EventConflict,
    EventInfo,
    RescheduleSuggestion,
    RescheduleSuggestions,
    TightTransition,
)
from calendar_mcp.timeutil import Interval
from calendar_mcp.tools.focus import selected_calendar_ids, zone_for

#: Events read per calendar before the report warns that it may be incomplete.
DEFAULT_MAX_EVENTS = 250
#: Most conflicting (or tight) pairs listed; the counts stay honest either way.
MAX_PAIRS = 100
#: Grid the reschedule suggestions are placed on.
SLOT_STEP_MINUTES = 15
#: Ceiling on candidate slots, so a wide window cannot blow up the search.
MAX_CANDIDATE_SLOTS = 2000

_MIDNIGHT = dt_time(0, 0)


# ---------------------------------------------------------------------------
# Event filtering
# ---------------------------------------------------------------------------


def _is_declined(event) -> bool:
    """True when the signed-in user has declined this invitation."""
    for attendee in event.attendees or []:
        if getattr(attendee, "self", False) and (attendee.responseStatus or "") == "declined":
            return True
    return False


def _is_transparent(event) -> bool:
    """True when the event is marked 'free' rather than 'busy'.

    Reads :attr:`~calendar_mcp.models.GoogleCalendarEvent.transparency`, which
    Google sets to ``'transparent'`` for an event the user marked "free"; the
    ``getattr`` keeps this working against a stub event that omits the field.
    """
    return str(getattr(event, "transparency", "") or "").lower() == "transparent"


def _event_span(event, zone, include_all_day: bool) -> Optional[Tuple[Interval, bool]]:
    """The interval an event occupies, or ``None`` when it should be ignored.

    Cancelled events, events the user declined, events marked free, and (unless
    asked for) all-day events do not clash with anything.
    """
    if (event.status or "") == "cancelled":
        return None
    if _is_declined(event) or _is_transparent(event):
        return None
    start, end = event.start, event.end
    if start is None or end is None:
        return None
    if start.dateTime and end.dateTime:
        return (start.dateTime, end.dateTime), False
    if not include_all_day:
        return None
    if start.date and end.date:
        return (srv.combine(start.date, _MIDNIGHT, zone), srv.combine(end.date, _MIDNIGHT, zone)), True
    return None


def _ref(event, account: str, calendar_id: str, span: Interval, all_day: bool, zone) -> ConflictEventRef:
    return ConflictEventRef(
        account=account,
        calendar_id=calendar_id,
        event_id=event.id,
        summary=event.summary,
        start=span[0].astimezone(zone).isoformat(),
        end=span[1].astimezone(zone).isoformat(),
        all_day=all_day,
    )


def _account_names(requested: Optional[Sequence[str]]) -> List[str]:
    """The accounts to read: the ones asked for, else every signed-in one."""
    if requested:
        return [accounts_module.validate_account_name(str(name)) for name in requested]
    known = [info.name for info in accounts_module.list_accounts() if info.valid]
    return known or [accounts_module.resolve_account(None)]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@srv.server.tool(
    name="detect_conflicts",
    title="Detect calendar conflicts",
    annotations=srv.READ_ONLY,
)
async def detect_conflicts(
    time_min: str,
    time_max: str,
    accounts: Optional[List[str]] = None,
    calendar_ids: Optional[List[str]] = None,
    include_all_day: bool = False,
    max_events_per_calendar: int = DEFAULT_MAX_EVENTS,
    ctx: Optional[srv.Context] = None,
) -> ConflictsResult:
    """Find double-booked events in a window, across every signed-in account.

    Reports two things. `conflicts` are genuine overlaps -- the user cannot be
    in both places. `tight` transitions do not overlap but leave less than the
    buffer from `get_preferences`, which is what makes a day feel impossible.

    Events the user declined, events marked free, cancelled events and (unless
    `include_all_day` is set) all-day events are ignored, because none of them
    actually occupy the user.

    Args:
        time_min: Start of the window to check, ISO 8601.
        time_max: End of the window to check, ISO 8601.
        accounts: Account names to check. Omit for every signed-in account,
            which is the point of the tool -- a work meeting clashing with a
            personal one is invisible from either calendar alone.
        calendar_ids: Restrict to these calendars (in every account checked).
            Omit for each account's selected calendars.
        include_all_day: True to let all-day events clash with timed ones.
        max_events_per_calendar: Safety limit per calendar. Default 250.
    """
    provider = srv._provider(ctx)
    skipped: List[str] = []

    def work() -> ConflictsResult:
        prefs = preferences_module.load()
        names = _account_names(accounts)

        refs: List[ConflictEventRef] = []
        spans: List[Interval] = []
        read_calendars: List[str] = []
        zone = None
        window: Optional[Interval] = None

        for name in names:
            try:
                creds = provider.get(name)
            except srv.AuthError as exc:
                skipped.append(f"{name}: {exc}")
                continue

            if window is None:
                start = srv._require(
                    srv.parse_datetime(time_min, "time_min", creds, "primary"), "time_min"
                )
                end = srv._require(
                    srv.parse_datetime(time_max, "time_max", creds, "primary"), "time_max"
                )
                if end <= start:
                    raise srv.CalendarToolError("time_max must be after time_min.")
                window = (start, end)
                zone = zone_for(prefs, creds)

            ids = (
                [str(item) for item in calendar_ids]
                if calendar_ids
                else selected_calendar_ids(creds)
            )
            for calendar_id in ids:
                try:
                    response = srv.calendar_actions.find_events(
                        credentials=creds,
                        calendar_id=calendar_id,
                        time_min=window[0],
                        time_max=window[1],
                        max_results=max_events_per_calendar,
                    )
                except HttpError as exc:
                    skipped.append(f"{name}:{calendar_id}: {srv._http_error_message(exc)}")
                    continue
                if response is None:
                    skipped.append(f"{name}:{calendar_id}: no result")
                    continue
                read_calendars.append(f"{name}:{calendar_id}")
                if len(response.items) >= max_events_per_calendar:
                    skipped.append(
                        f"{name}:{calendar_id}: only the first {max_events_per_calendar} "
                        "events were read, so later conflicts may be missing"
                    )
                for event in response.items:
                    found = _event_span(event, zone, include_all_day)
                    if found is None:
                        continue
                    span, all_day = found
                    refs.append(_ref(event, name, calendar_id, span, all_day, zone))
                    spans.append(span)

        if window is None:
            raise srv.CalendarToolError(
                "No account could be read. " + (" ".join(skipped) or "Run 'calendar-mcp auth'.")
            )

        overlaps = scheduling_logic.overlapping_pairs(spans)
        tights = scheduling_logic.tight_pairs(spans, prefs.buffer_minutes)

        conflicts = [
            EventConflict(
                overlap_minutes=round(minutes, 1),
                same_calendar=refs[first].calendar_id == refs[second].calendar_id
                and refs[first].account == refs[second].account,
                same_account=refs[first].account == refs[second].account,
                first=refs[first],
                second=refs[second],
            )
            for first, second, minutes in overlaps[:MAX_PAIRS]
        ]
        tight = [
            TightTransition(
                gap_minutes=round(minutes, 1),
                buffer_minutes=prefs.buffer_minutes,
                first=refs[first],
                second=refs[second],
            )
            for first, second, minutes in tights[:MAX_PAIRS]
        ]

        if overlaps:
            message = (
                f"{len(overlaps)} conflict(s) across {len(refs)} events on "
                f"{len(read_calendars)} calendar(s)."
            )
        else:
            message = f"No conflicts among {len(refs)} events on {len(read_calendars)} calendar(s)."
        if tights:
            message += f" {len(tights)} transition(s) leave less than {prefs.buffer_minutes} minutes."
        if len(overlaps) > MAX_PAIRS or len(tights) > MAX_PAIRS:
            message += f" Only the first {MAX_PAIRS} of each are listed."
        if skipped:
            message += f" {len(skipped)} calendar(s) could not be read in full."

        return ConflictsResult(
            time_min=window[0].astimezone(zone).isoformat(),
            time_max=window[1].astimezone(zone).isoformat(),
            timezone=str(zone),
            accounts=names,
            calendar_ids=read_calendars,
            event_count=len(refs),
            buffer_minutes=prefs.buffer_minutes,
            include_all_day=include_all_day,
            conflict_count=len(overlaps),
            conflicts=conflicts,
            tight_count=len(tights),
            tight=tight,
            skipped=skipped,
            message=message,
        )

    result = await srv._run(work)
    if result.skipped:
        await srv._warn(ctx, "Not everything could be read: " + "; ".join(result.skipped))
    return result


@srv.server.tool(
    name="suggest_reschedule",
    title="Suggest better times for an event",
    annotations=srv.UPDATE,
)
async def suggest_reschedule(
    event_id: str,
    calendar_id: str = "primary",
    search_days: int = 7,
    max_suggestions: int = 5,
    search_from: Optional[str] = None,
    apply: bool = False,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> RescheduleSuggestions:
    """Propose better times for an existing meeting, ranked, keeping its length.

    Reads the event, then looks for slots of the same duration inside the user's
    working hours where the organiser is free, and ranks them: fewest attendee
    conflicts first, the event's current day next, then earliest. Slots that sit
    closer to another meeting than the user's buffer allows are penalised, not
    hidden.

    Read-only by default -- it suggests, the user decides. Set `apply` to move
    the event to the top suggestion once they have agreed to it.

    Args:
        event_id: The event to reschedule (from `find_events`).
        calendar_id: Calendar the event lives on.
        search_days: How many days ahead to search. Default 7, maximum 60.
        max_suggestions: How many times to propose. Default 5, maximum 20.
        search_from: Start searching from this time, ISO 8601. Defaults to now.
        apply: True to actually move the event to the best suggestion. Only set
            this when the user has agreed to the time.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> RescheduleSuggestions:
        if not event_id:
            raise srv.CalendarToolError("event_id is required.")
        if not 1 <= search_days <= 60:
            raise srv.CalendarToolError("search_days must be between 1 and 60.")
        if not 1 <= max_suggestions <= 20:
            raise srv.CalendarToolError("max_suggestions must be between 1 and 20.")

        creds = provider.get(account)
        prefs = preferences_module.load()
        zone = zone_for(prefs, creds, calendar_id)

        event = srv.calendar_actions.get_event(
            credentials=creds, event_id=event_id, calendar_id=calendar_id
        )
        if event is None:
            raise srv._no_result("Reading the event")
        if not (event.start and event.start.dateTime and event.end and event.end.dateTime):
            raise srv.CalendarToolError(
                "suggest_reschedule needs a timed event; this one is all-day, so there is "
                "no duration to preserve."
            )
        original_start = event.start.dateTime
        original_end = event.end.dateTime
        duration = scheduling_logic.interval_minutes((original_start, original_end))
        if duration <= 0:
            raise srv.CalendarToolError("The event has no duration, so there is nothing to place.")

        attendees = sorted(
            {
                str(attendee.email)
                for attendee in event.attendees or []
                if attendee.email
                and not attendee.resource
                and not getattr(attendee, "self", False)
                and (attendee.responseStatus or "") != "declined"
            }
        )

        search_min = srv.parse_datetime(search_from, "search_from", creds, calendar_id)
        if search_min is None:
            search_min = datetime.now(zone)
        search_min = scheduling_logic.align_up(search_min, SLOT_STEP_MINUTES)
        search_max = search_min + timedelta(days=search_days)

        busy_by_key: Dict[str, List[Interval]] = {}
        organizer_busy: List[Interval] = []
        raw = srv.calendar_actions.find_availability(
            credentials=creds,
            time_min=search_min,
            time_max=search_max,
            calendar_ids=[calendar_id] + attendees,
        )
        if raw is None:
            raise srv._no_result("Reading free/busy")
        for key, data in raw.items():
            intervals = [(item["start"], item["end"]) for item in data.get("busy", [])]
            if key == calendar_id:
                organizer_busy = intervals
            else:
                busy_by_key[key] = intervals

        windows = preferences_module.working_windows(prefs, search_min, search_max, zone)
        candidates = scheduling_logic.generate_slots(
            windows,
            duration_minutes=int(round(duration)),
            step_minutes=SLOT_STEP_MINUTES,
            limit=MAX_CANDIDATE_SLOTS,
        )
        ranked = scheduling_logic.rank_slots(
            candidates,
            busy_by_key=busy_by_key,
            unavailable=organizer_busy,
            original_start=original_start,
            buffer_minutes=prefs.buffer_minutes,
            tzinfo=zone,
            limit=max_suggestions,
        )

        suggestions = [
            RescheduleSuggestion(
                start=slot.start.astimezone(zone).isoformat(),
                end=slot.end.astimezone(zone).isoformat(),
                score=slot.score,
                attendee_conflicts=list(slot.conflicts),
                reasons=list(slot.reasons),
            )
            for slot in ranked
        ]

        applied = False
        applied_event: Optional[EventInfo] = None
        if apply:
            if not ranked:
                raise srv.CalendarToolError(
                    "There is no free slot to move this event to in the next "
                    f"{search_days} day(s), so nothing was moved. Widen search_days."
                )
            best = ranked[0]
            moved = srv.calendar_actions.move_event(
                credentials=creds,
                event_id=event_id,
                calendar_id=calendar_id,
                new_start=best.start,
                new_end=best.end,
                send_notifications=True,
            )
            if moved is None:
                raise srv._no_result("Moving the event")
            applied = True
            applied_event = EventInfo.from_event(moved)

        if not suggestions:
            message = (
                f"No free {duration:g}-minute slot inside the working hours in the next "
                f"{search_days} day(s). Widen search_days or adjust the working hours."
            )
        elif applied:
            message = (
                f"Moved '{event.summary or event_id}' to {suggestions[0].start} "
                f"(score {suggestions[0].score:g})."
            )
        else:
            message = (
                f"{len(suggestions)} suggestion(s) for '{event.summary or event_id}', best first. "
                "Nothing has been moved."
            )

        return RescheduleSuggestions(
            calendar_id=calendar_id,
            event_id=event_id,
            summary=event.summary,
            current_start=original_start.astimezone(zone).isoformat(),
            current_end=original_end.astimezone(zone).isoformat(),
            duration_minutes=round(duration, 1),
            timezone=str(zone),
            attendees=attendees,
            search_time_min=search_min.astimezone(zone).isoformat(),
            search_time_max=search_max.astimezone(zone).isoformat(),
            count=len(suggestions),
            suggestions=suggestions,
            applied=applied,
            applied_event=applied_event,
            message=message,
        )

    return await srv._run(work)
