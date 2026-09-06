"""Tools that find and book uninterrupted time.

``find_focus_time`` answers "when could I actually think this week"; it reads
the user's working hours, lunch and buffer from
:mod:`calendar_mcp.preferences`, subtracts everything already on their
calendars, and reports what is left. ``block_focus_time`` takes the same
candidates and defends them by putting real events on the calendar.

The reasoning is in :mod:`calendar_mcp.scheduling`, which is pure and unit
tested; the tools here only fetch, convert and report. Two small Google-facing
helpers -- :func:`zone_for` and :func:`selected_calendar_ids` -- are shared with
:mod:`calendar_mcp.tools.conflicts`, which imports them from here.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from googleapiclient.errors import HttpError

from calendar_mcp import preferences as preferences_module
from calendar_mcp import scheduling as scheduling_logic
from calendar_mcp import server as srv
from calendar_mcp.models import (
    BlockedFocusEvent,
    BlockedFocusResult,
    EventCreateRequest,
    EventDateTime,
    EventReminders,
    FocusBlock,
    FocusTimeResult,
)
from calendar_mcp.timeutil import Interval

#: What a focus block is called when the caller does not say.
DEFAULT_FOCUS_TITLE = "Focus time"


# ---------------------------------------------------------------------------
# Shared helpers (also used by calendar_mcp.tools.conflicts)
# ---------------------------------------------------------------------------


def zone_for(prefs: "preferences_module.Preferences", creds, calendar_id: str = "primary"):
    """The timezone to express results in: the user's preference, else the calendar's."""
    return prefs.tzinfo(None) or srv._default_tzinfo(creds, calendar_id)


def selected_calendar_ids(creds) -> List[str]:
    """Every calendar the user has ticked on, which is what "my calendar" means.

    Hidden, deleted and unselected calendars are skipped -- a calendar the user
    has switched off in the Google UI should not eat their focus time. Falls
    back to ``['primary']`` when the list cannot be read or is empty.
    """
    response = srv.calendar_actions.find_calendars(creds)
    if response is None:
        return ["primary"]
    chosen = [
        entry.id
        for entry in response.items
        if entry.id and not entry.deleted and not entry.hidden and entry.selected is not False
    ]
    return chosen or ["primary"]


def _busy_intervals(
    creds,
    time_min: datetime,
    time_max: datetime,
    calendar_ids: List[str],
) -> Tuple[List[Interval], List[str]]:
    """Free/busy across ``calendar_ids``, as intervals plus per-calendar problems."""
    raw = srv.calendar_actions.find_availability(
        credentials=creds,
        time_min=time_min,
        time_max=time_max,
        calendar_ids=list(calendar_ids),
    )
    if raw is None:
        raise srv._no_result("Reading free/busy")

    busy: List[Interval] = []
    problems: List[str] = []
    for calendar_id, data in raw.items():
        for error in data.get("errors") or []:
            reason = error.get("reason", str(error)) if isinstance(error, dict) else str(error)
            problems.append(f"{calendar_id}: {reason}")
        for interval in data.get("busy", []):
            busy.append((interval["start"], interval["end"]))
    return busy, problems


def _to_block(interval: Interval, zone) -> FocusBlock:
    """Renders one free interval as the FocusBlock the client sees."""
    start = interval[0].astimezone(zone)
    end = interval[1].astimezone(zone)
    return FocusBlock(
        start=start.isoformat(),
        end=end.isoformat(),
        duration_minutes=round(scheduling_logic.interval_minutes(interval), 1),
        date=start.date().isoformat(),
        weekday=start.strftime("%A"),
    )


def _find_blocks(
    creds,
    prefs: "preferences_module.Preferences",
    time_min: datetime,
    time_max: datetime,
    calendar_ids: List[str],
    zone,
) -> Tuple[List[Interval], List[str]]:
    """The usable focus blocks in the window, plus any unreadable calendars.

    Working hours (minus lunch) come from the preferences, busy time from
    free/busy across ``calendar_ids``, and the buffer and minimum block length
    from the preferences again.
    """
    busy, problems = _busy_intervals(creds, time_min, time_max, calendar_ids)
    windows = preferences_module.working_windows(prefs, time_min, time_max, zone)
    blocks = scheduling_logic.candidate_blocks(
        windows,
        busy,
        min_block_minutes=prefs.min_focus_block_minutes,
        buffer_minutes=prefs.buffer_minutes,
    )
    return blocks, problems


def _validate_window(start: datetime, end: datetime, hours_needed: float) -> None:
    if end <= start:
        raise srv.CalendarToolError("time_max must be after time_min.")
    if hours_needed < 0:
        raise srv.CalendarToolError("hours_needed cannot be negative.")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@srv.server.tool(
    name="find_focus_time",
    title="Find focus time",
    annotations=srv.READ_ONLY,
)
async def find_focus_time(
    time_min: str,
    time_max: str,
    hours_needed: float,
    calendar_ids: Optional[List[str]] = None,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> FocusTimeResult:
    """Find the uninterrupted blocks in a window that could be used for deep work.

    Searches inside the user's working hours (`get_preferences`), removes lunch,
    everything already booked on their calendars, and the buffer they like
    around meetings, then reports what is left -- longest block first, because
    that is the one worth protecting. Blocks shorter than
    `min_focus_block_minutes` are not reported at all.

    Read-only: use `block_focus_time` to actually defend the time.

    Args:
        time_min: Start of the window to search, ISO 8601. Without a UTC offset
            it is read in the calendar's own timezone.
        time_max: End of the window to search, ISO 8601.
        hours_needed: How many focus hours the user is trying to find. Used to
            report whether the window can supply them; pass 0 to just look.
        calendar_ids: Calendars whose events count as busy. Omit for every
            calendar the account has selected.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)
    problems: List[str] = []

    def work() -> FocusTimeResult:
        creds = provider.get(account)
        prefs = preferences_module.load()
        start = srv._require(srv.parse_datetime(time_min, "time_min", creds, "primary"), "time_min")
        end = srv._require(srv.parse_datetime(time_max, "time_max", creds, "primary"), "time_max")
        _validate_window(start, end, hours_needed)

        zone = zone_for(prefs, creds)
        ids = [str(item) for item in calendar_ids] if calendar_ids else selected_calendar_ids(creds)
        blocks, issues = _find_blocks(creds, prefs, start, end, ids, zone)
        problems.extend(issues)

        available = scheduling_logic.total_hours(blocks)
        satisfiable = available + 1e-9 >= hours_needed
        ranked = scheduling_logic.rank_blocks(blocks)

        if not ranked:
            message = (
                f"No free block of at least {prefs.min_focus_block_minutes} minutes exists "
                "inside the working hours in that window."
            )
        elif satisfiable:
            message = (
                f"{available:g} focus hours available in {len(ranked)} block(s); "
                f"{hours_needed:g} requested."
            )
        else:
            message = (
                f"Only {available:g} of the {hours_needed:g} focus hours requested are "
                f"available, in {len(ranked)} block(s). Widen the window or shorten the ask."
            )

        return FocusTimeResult(
            time_min=start.astimezone(zone).isoformat(),
            time_max=end.astimezone(zone).isoformat(),
            timezone=str(zone),
            calendar_ids=ids,
            hours_needed=float(hours_needed),
            total_free_hours=available,
            satisfiable=satisfiable,
            min_block_minutes=prefs.min_focus_block_minutes,
            buffer_minutes=prefs.buffer_minutes,
            count=len(ranked),
            blocks=[_to_block(block, zone) for block in ranked],
            message=message,
        )

    result = await srv._run(work)
    if problems:
        await srv._warn(
            ctx,
            "Free/busy could not be read for: " + "; ".join(problems)
            + ". Those calendars were treated as free.",
        )
    return result


@srv.server.tool(
    name="block_focus_time",
    title="Book focus time",
    annotations=srv.WRITE,
)
async def block_focus_time(
    time_min: str,
    time_max: str,
    hours_needed: float,
    title: str = DEFAULT_FOCUS_TITLE,
    calendar_id: Optional[str] = None,
    check_calendar_ids: Optional[List[str]] = None,
    description: Optional[str] = None,
    dry_run: bool = False,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> BlockedFocusResult:
    """Book the best free blocks in a window as focus time, until the hours add up.

    Takes the blocks `find_focus_time` would report, longest first, and creates
    an event on each until `hours_needed` is covered. The last block is trimmed
    to what is still needed rather than swallowing a whole afternoon. Events are
    created busy, with reminders off and without notifying anyone.

    Run it with `dry_run` first when the user has not yet agreed to the times.

    Args:
        time_min: Start of the window to book inside, ISO 8601.
        time_max: End of the window, ISO 8601.
        hours_needed: How many focus hours to book.
        title: Title for the blocks. Default 'Focus time'.
        calendar_id: Calendar to book on. Omit for the user's configured
            focus_calendar_id (see `get_preferences`).
        check_calendar_ids: Calendars whose events count as busy. Omit for every
            calendar the account has selected.
        description: Optional note to put in each block.
        dry_run: True to report the blocks that would be booked without writing
            anything to the calendar.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)
    problems: List[str] = []

    def work() -> BlockedFocusResult:
        creds = provider.get(account)
        prefs = preferences_module.load()
        target = (calendar_id or prefs.focus_calendar_id or "primary").strip()
        start = srv._require(srv.parse_datetime(time_min, "time_min", creds, target), "time_min")
        end = srv._require(srv.parse_datetime(time_max, "time_max", creds, target), "time_max")
        _validate_window(start, end, hours_needed)
        if hours_needed <= 0:
            raise srv.CalendarToolError(
                "hours_needed must be greater than zero; use find_focus_time to just look."
            )

        zone = zone_for(prefs, creds, target)
        ids = (
            [str(item) for item in check_calendar_ids]
            if check_calendar_ids
            else selected_calendar_ids(creds)
        )
        blocks, issues = _find_blocks(creds, prefs, start, end, ids, zone)
        problems.extend(issues)

        available = scheduling_logic.total_hours(blocks)
        chosen = scheduling_logic.select_blocks(
            blocks, hours_needed, min_block_minutes=prefs.min_focus_block_minutes
        )
        if not chosen:
            raise srv.CalendarToolError(
                f"No free block of at least {prefs.min_focus_block_minutes} minutes exists "
                f"inside the working hours between {start.astimezone(zone).isoformat()} and "
                f"{end.astimezone(zone).isoformat()}. Widen the window, or lower "
                "min_focus_block_minutes with set_preferences."
            )

        booked: List[BlockedFocusEvent] = []
        failure: Optional[str] = None
        for block_start, block_end in chosen:
            local_start = block_start.astimezone(zone)
            local_end = block_end.astimezone(zone)
            entry = BlockedFocusEvent(
                start=local_start.isoformat(),
                end=local_end.isoformat(),
                duration_minutes=round(scheduling_logic.interval_minutes((block_start, block_end)), 1),
                summary=title,
                created=False,
            )
            if dry_run:
                booked.append(entry)
                continue
            details = EventCreateRequest(
                summary=title,
                start=EventDateTime(dateTime=local_start),
                end=EventDateTime(dateTime=local_end),
                description=description,
                # A promise to yourself, not a meeting: no reminder, no email.
                reminders=EventReminders(useDefault=False, overrides=[]),
            )
            try:
                created = srv.calendar_actions.create_event(
                    credentials=creds,
                    event_data=details,
                    calendar_id=target,
                    send_notifications=False,
                )
            except HttpError as exc:
                # Stop, but still report the blocks that did get booked -- a
                # half-applied write the caller cannot see is far worse.
                failure = srv._http_error_message(exc)
                break
            if created is None:
                failure = "Google did not return the created event."
                break
            entry = entry.model_copy(
                update={
                    "created": True,
                    "event_id": created.id,
                    "html_link": created.html_link,
                }
            )
            booked.append(entry)

        hours_booked = scheduling_logic.total_hours(
            [(block_start, block_end) for block_start, block_end in chosen[: len(booked)]]
        )
        satisfied = hours_booked + 1e-9 >= hours_needed
        verb = "Would book" if dry_run else "Booked"
        message = (
            f"{verb} {hours_booked:g} of the {hours_needed:g} focus hours requested "
            f"in {len(booked)} block(s) on '{target}'."
        )
        if failure:
            message += f" Stopped early: {failure}"
        elif not satisfied:
            message += " The window did not have enough free time for the rest."

        return BlockedFocusResult(
            calendar_id=target,
            time_min=start.astimezone(zone).isoformat(),
            time_max=end.astimezone(zone).isoformat(),
            timezone=str(zone),
            dry_run=dry_run,
            hours_needed=float(hours_needed),
            hours_booked=hours_booked,
            total_free_hours=available,
            satisfied=satisfied,
            count=len(booked),
            events=booked,
            message=message,
        )

    result = await srv._run(work)
    if problems:
        await srv._warn(
            ctx,
            "Free/busy could not be read for: " + "; ".join(problems)
            + ". Those calendars were treated as free.",
        )
    if not result.dry_run and not result.satisfied:
        await srv._warn(ctx, result.message)
    return result
