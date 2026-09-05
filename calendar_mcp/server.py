"""The calendar-mcp MCP server: Google Calendar as MCP tools.

Single process, no HTTP hop. Every tool calls :mod:`calendar_mcp.calendar_actions`
directly, and returns a pydantic model so the MCP client gets structured output.

Google credentials are loaded lazily, on the first tool call that needs them, so
the MCP handshake answers instantly and a missing token surfaces as a readable
tool error rather than a hung startup.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, AsyncIterator, Callable, List, Optional, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from dateutil import parser as date_parser
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import auth, calendar_actions
from .auth import AuthError
from .models import (
    AttendeeStatusEntry,
    AttendeeStatusResult,
    BusyPeriod,
    BusynessResult,
    CalendarBusyPeriods,
    CalendarInfo,
    CalendarListResult,
    DayBusyness,
    DeleteEventResult,
    EventCreateRequest,
    EventDateTime,
    EventInfo,
    EventListResult,
    EventResult,
    EventUpdateRequest,
    FreeBusyResult,
    ProjectedEventsResult,
    ProjectedOccurrence,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

SERVER_NAME = "calendar-mcp"
SERVER_VERSION = "1.0.1"

INSTRUCTIONS = """\
Read and manage the user's Google Calendar.

Calendars are addressed by `calendar_id`. Use `primary` for the user's own main
calendar (the default everywhere); call `list_calendars` to discover the IDs of
secondary and shared calendars.

Times are ISO 8601 strings. Always include a UTC offset when you know the user's
timezone -- `2026-03-14T15:00:00-04:00` or `2026-03-14T19:00:00Z`. A timestamp
without an offset is interpreted in the target calendar's own timezone, which is
usually what the user means but is worth stating back to them. All-day events
use a plain `YYYY-MM-DD` date instead.

Workflow notes:
  * `find_events` first when the user refers to an event by description -- the
    write tools need the `id` it returns.
  * `quick_add_event` is the fastest path for a one-line natural-language event
    ("lunch with Dana Friday 1pm"); `create_event` when you need attendees,
    a description, a location or exact control of the times.
  * `move_event` reschedules and keeps the original duration when you supply
    only `new_start`; prefer it over `update_event` for "push this back an hour".
  * `respond_to_event` sets the user's own RSVP on an invitation.
  * `delete_event` is irreversible and may ask the user to confirm.
  * `query_free_busy` and `schedule_mutual` read other people's calendars by
    email address; they return busy intervals only, never event details.
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CalendarToolError(ToolError):
    """A tool-level failure reported back to the MCP client as an error string."""


def _http_error_message(error: HttpError) -> str:
    """Renders a googleapiclient HttpError as one concise, actionable line."""
    status = getattr(getattr(error, "resp", None), "status", None) or "unknown"
    detail = ""
    try:
        payload = error.content.decode("utf-8") if error.content else ""
        parsed = json.loads(payload)
        detail = (parsed.get("error") or {}).get("message") or ""
        if not detail:
            errors = (parsed.get("error") or {}).get("errors") or []
            if errors:
                detail = errors[0].get("message", "")
    except Exception:
        detail = ""
    if not detail:
        detail = getattr(error, "reason", "") or str(error)

    hint = ""
    if status == 401:
        hint = " Run 'calendar-mcp auth' to sign in again."
    elif status == 403:
        hint = " The account may lack access to this calendar, or the API quota is exhausted."
    elif status == 404:
        hint = " Check the calendar_id and event_id."

    return f"Google Calendar API error {status}: {detail}{hint}"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class CredentialProvider:
    """Lazily loads, caches and refreshes Google credentials.

    Nothing touches the network until :meth:`get` is first called, which happens
    on the first tool invocation rather than at server startup.
    """

    def __init__(self) -> None:
        self._creds: Optional[Credentials] = None
        self._lock = threading.Lock()

    def get(self) -> Credentials:
        """Returns valid credentials, refreshing or reloading as needed.

        Raises:
            AuthError: When no usable token exists (the message says what to do).
        """
        with self._lock:
            if self._creds is not None and self._creds.valid:
                return self._creds
            # auth.get_credentials refreshes an expired token itself and will not
            # open a browser unless CALENDAR_MCP_ALLOW_BROWSER_AUTH is set.
            self._creds = auth.get_credentials()
            return self._creds

    def reset(self) -> None:
        """Drops the cached credentials so the next call reloads from disk."""
        with self._lock:
            self._creds = None


credential_provider = CredentialProvider()

# Calendar timezones are stable; cache them so naive timestamps do not cost an
# extra API round trip on every call.
_timezone_cache: dict[str, Optional[str]] = {}
_timezone_lock = threading.Lock()


def _calendar_timezone(creds: Credentials, calendar_id: str) -> Optional[str]:
    """Returns the IANA timezone of ``calendar_id``, cached per process."""
    with _timezone_lock:
        if calendar_id in _timezone_cache:
            return _timezone_cache[calendar_id]
    tz = calendar_actions.get_calendar_timezone(creds, calendar_id)
    with _timezone_lock:
        _timezone_cache[calendar_id] = tz
    return tz


def _default_tzinfo(creds: Optional[Credentials], calendar_id: Optional[str]):
    """The tzinfo naive timestamps are interpreted in: the calendar's, else local."""
    if creds is not None and calendar_id:
        name = _calendar_timezone(creds, calendar_id)
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning("Calendar '%s' reports unknown timezone '%s'.", calendar_id, name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_datetime(
    value: Optional[str],
    field: str,
    creds: Optional[Credentials] = None,
    calendar_id: Optional[str] = None,
) -> Optional[datetime]:
    """Parses an ISO 8601 string into an aware datetime.

    A value without a UTC offset is interpreted in ``calendar_id``'s timezone
    when credentials are available, and otherwise in the server's local zone.

    Raises:
        CalendarToolError: If the string is not a usable ISO 8601 timestamp.
    """
    if value is None or value == "":
        return None
    try:
        parsed = date_parser.isoparse(value)
    except (ValueError, TypeError):
        try:
            parsed = date_parser.parse(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise CalendarToolError(
                f"{field} is not a valid ISO 8601 timestamp: {value!r}. "
                "Use e.g. '2026-03-14T15:00:00-04:00' or '2026-03-14T19:00:00Z'."
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_default_tzinfo(creds, calendar_id))
    return parsed


def _require(value: Optional[datetime], field: str) -> datetime:
    if value is None:
        raise CalendarToolError(f"{field} is required.")
    return value


def _parse_clock(value: Optional[str], field: str) -> Optional[dt_time]:
    """Parses an 'HH:MM' working-hours bound."""
    if not value:
        return None
    try:
        hour, _, minute = value.partition(":")
        return dt_time(int(hour), int(minute or 0))
    except (ValueError, TypeError) as exc:
        raise CalendarToolError(f"{field} must look like 'HH:MM'; got {value!r}.") from exc


# ---------------------------------------------------------------------------
# Execution helper
# ---------------------------------------------------------------------------


async def _run(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Runs blocking Google API work off the event loop and normalises errors."""
    call = functools.partial(func, *args, **kwargs)
    try:
        return await anyio.to_thread.run_sync(call)
    except CalendarToolError:
        raise
    except HttpError as exc:
        raise CalendarToolError(_http_error_message(exc)) from exc
    except AuthError as exc:
        raise CalendarToolError(str(exc)) from exc
    except ValueError as exc:
        raise CalendarToolError(str(exc)) from exc


def _provider(ctx: Optional[Context] = None) -> CredentialProvider:
    """The CredentialProvider for this request (lifespan-scoped, else global)."""
    if ctx is not None:
        try:
            state = ctx.request_context.lifespan_context
        except (ValueError, AttributeError):
            state = None
        if isinstance(state, AppContext):
            return state.credentials
    return credential_provider


async def _warn(ctx: Optional[Context], message: str) -> None:
    """Best-effort warning to the client; always recorded in the server log."""
    logger.warning(message)
    if ctx is None:
        return
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await ctx.warning(message)
    except Exception:  # client without logging capability, or no live session
        pass


def _no_result(action: str) -> CalendarToolError:
    return CalendarToolError(
        f"{action} did not return a usable result. The server log has the details."
    )


# ---------------------------------------------------------------------------
# Server + lifespan
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    """Lifespan state shared by every request."""

    credentials: CredentialProvider


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    """Prepares shared state. Deliberately does no network or disk I/O."""
    logger.info("calendar-mcp %s starting (credentials load on first tool call)", SERVER_VERSION)
    try:
        yield AppContext(credentials=credential_provider)
    finally:
        logger.info("calendar-mcp shutting down")


server = MCPServer(
    name=SERVER_NAME,
    title="Google Calendar",
    version=SERVER_VERSION,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)


READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------


@server.tool(
    name="list_calendars",
    title="List calendars",
    annotations=READ_ONLY,
)
async def list_calendars(
    min_access_role: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> CalendarListResult:
    """List the calendars the signed-in user can see, with their IDs and timezones.

    Start here whenever the user mentions a calendar other than their own: the
    `id` values returned are what every other tool's `calendar_id` expects.

    Args:
        min_access_role: Only return calendars where the user has at least this
            role: 'freeBusyReader', 'reader', 'writer' or 'owner'. Omit for all.
    """
    provider = _provider(ctx)

    def work() -> CalendarListResult:
        creds = provider.get()
        response = calendar_actions.find_calendars(creds, min_access_role=min_access_role)
        if response is None:
            raise _no_result("Listing calendars")
        calendars = [CalendarInfo.from_entry(entry) for entry in response.items]
        return CalendarListResult(count=len(calendars), calendars=calendars)

    return await _run(work)


@server.tool(
    name="create_calendar",
    title="Create a calendar",
    annotations=WRITE,
)
async def create_calendar(
    summary: str,
    ctx: Optional[Context] = None,
) -> CalendarInfo:
    """Create a new secondary calendar owned by the user.

    Args:
        summary: Name for the new calendar, e.g. 'Client work'.
    """
    provider = _provider(ctx)

    def work() -> CalendarInfo:
        creds = provider.get()
        created = calendar_actions.create_calendar(creds, summary=summary)
        if created is None:
            raise _no_result("Creating the calendar")
        # Drop any cached timezone under this brand-new ID.
        with _timezone_lock:
            _timezone_cache.pop(created.id, None)
        return CalendarInfo.from_entry(created)

    return await _run(work)


# ---------------------------------------------------------------------------
# Reading events
# ---------------------------------------------------------------------------


@server.tool(
    name="find_events",
    title="Find events",
    annotations=READ_ONLY,
)
async def find_events(
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 50,
    ctx: Optional[Context] = None,
) -> EventListResult:
    """Search a calendar for events, expanding recurring series into instances.

    Use this before any tool that needs an `event_id`. Narrow the window with
    `time_min`/`time_max` rather than raising `max_results`.

    Args:
        calendar_id: Calendar to search. 'primary' is the user's own calendar.
        time_min: Inclusive start of the window, ISO 8601 with a UTC offset.
            Without an offset it is read in the calendar's own timezone.
        time_max: Exclusive end of the window, ISO 8601.
        query: Free-text search over title, description, location and attendees.
        max_results: Maximum events to return (default 50).
    """
    provider = _provider(ctx)

    def work() -> EventListResult:
        creds = provider.get()
        response = calendar_actions.find_events(
            credentials=creds,
            calendar_id=calendar_id,
            time_min=parse_datetime(time_min, "time_min", creds, calendar_id),
            time_max=parse_datetime(time_max, "time_max", creds, calendar_id),
            query=query,
            max_results=max_results,
        )
        if response is None:
            raise _no_result("Finding events")
        events = [EventInfo.from_event(item) for item in response.items]
        return EventListResult(
            calendar_id=calendar_id,
            count=len(events),
            events=events,
            time_zone=response.timeZone,
        )

    result = await _run(work)
    if result.count >= max_results:
        await _warn(
            ctx,
            f"find_events hit the max_results limit of {max_results}; there may be more "
            "events in this window.",
        )
    return result


@server.tool(
    name="check_attendee_status",
    title="Check attendee RSVPs",
    annotations=READ_ONLY,
)
async def check_attendee_status(
    event_id: str,
    calendar_id: str = "primary",
    attendee_emails: Optional[List[str]] = None,
    ctx: Optional[Context] = None,
) -> AttendeeStatusResult:
    """Report who has accepted, declined or not yet answered an event invitation.

    Args:
        event_id: The event to inspect (from `find_events`).
        calendar_id: Calendar the event lives on.
        attendee_emails: Restrict the report to these addresses. Omit for all.
    """
    provider = _provider(ctx)

    def work() -> AttendeeStatusResult:
        creds = provider.get()
        statuses = calendar_actions.check_attendee_status(
            credentials=creds,
            event_id=event_id,
            calendar_id=calendar_id,
            attendee_emails=attendee_emails,
        )
        if statuses is None:
            raise _no_result("Checking attendee status")
        entries = [
            AttendeeStatusEntry(email=str(email), response_status=status)
            for email, status in statuses.items()
        ]
        return AttendeeStatusResult(
            calendar_id=calendar_id,
            event_id=event_id,
            count=len(entries),
            attendees=entries,
        )

    return await _run(work)


@server.tool(
    name="query_free_busy",
    title="Query free/busy",
    annotations=READ_ONLY,
)
async def query_free_busy(
    calendar_ids: List[str],
    time_min: str,
    time_max: str,
    ctx: Optional[Context] = None,
) -> FreeBusyResult:
    """Get busy intervals for one or more calendars, without revealing event details.

    Works for other people's calendars addressed by email, which is how you check
    whether someone is free before proposing a time.

    Args:
        calendar_ids: Calendar IDs or attendee email addresses to check.
        time_min: Start of the window, ISO 8601 with a UTC offset.
        time_max: End of the window, ISO 8601 with a UTC offset.
    """
    provider = _provider(ctx)

    def work() -> FreeBusyResult:
        creds = provider.get()
        start = _require(parse_datetime(time_min, "time_min", creds, "primary"), "time_min")
        end = _require(parse_datetime(time_max, "time_max", creds, "primary"), "time_max")
        raw = calendar_actions.find_availability(
            credentials=creds,
            time_min=start,
            time_max=end,
            calendar_ids=list(calendar_ids),
        )
        if raw is None:
            raise _no_result("Querying free/busy")
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

    result = await _run(work)
    unreadable = [c.calendar_id for c in result.calendars if c.errors]
    if unreadable:
        await _warn(ctx, f"Free/busy could not be read for: {', '.join(unreadable)}.")
    return result


@server.tool(
    name="analyze_busyness",
    title="Analyze calendar load",
    annotations=READ_ONLY,
)
async def analyze_busyness(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    ctx: Optional[Context] = None,
) -> BusynessResult:
    """Summarise how loaded each day is: event count and total scheduled minutes.

    Use this to answer "how busy is my week" without listing every event.

    Args:
        time_min: Start of the window, ISO 8601.
        time_max: End of the window, ISO 8601.
        calendar_id: Calendar to analyse.
    """
    provider = _provider(ctx)

    def work() -> BusynessResult:
        creds = provider.get()
        start = _require(parse_datetime(time_min, "time_min", creds, calendar_id), "time_min")
        end = _require(parse_datetime(time_max, "time_max", creds, calendar_id), "time_max")
        stats = calendar_actions.get_busyness_analysis(
            credentials=creds,
            time_min=start,
            time_max=end,
            calendar_id=calendar_id,
        )
        if stats is None:
            raise _no_result("Analysing busyness")
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

    return await _run(work)


@server.tool(
    name="project_recurring_events",
    title="Project recurring events",
    annotations=READ_ONLY,
)
async def project_recurring_events(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    event_query: Optional[str] = None,
    ctx: Optional[Context] = None,
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
    """
    provider = _provider(ctx)

    def work() -> ProjectedEventsResult:
        creds = provider.get()
        start = _require(parse_datetime(time_min, "time_min", creds, calendar_id), "time_min")
        end = _require(parse_datetime(time_max, "time_max", creds, calendar_id), "time_max")
        occurrences = calendar_actions.get_projected_recurring_events(
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

    return await _run(work)


# ---------------------------------------------------------------------------
# Writing events
# ---------------------------------------------------------------------------


@server.tool(
    name="create_event",
    title="Create an event",
    annotations=WRITE,
)
async def create_event(
    calendar_id: str = "primary",
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendee_emails: Optional[List[str]] = None,
    send_notifications: bool = True,
    ctx: Optional[Context] = None,
) -> EventResult:
    """Create an event with explicit start and end times, and optional attendees.

    For a one-line natural-language description ("coffee with Sam tomorrow at 3")
    prefer `quick_add_event`.

    Args:
        calendar_id: Calendar to create the event on.
        summary: Event title. Required.
        start_time: Start, ISO 8601 with a UTC offset (e.g. '2026-03-14T15:00:00-04:00').
            Without an offset it is read in the calendar's own timezone.
        end_time: End, ISO 8601, same convention as start_time.
        description: Longer notes for the event body.
        location: Free-text location or meeting link.
        attendee_emails: People to invite. They receive an invitation email
            unless send_notifications is false.
        send_notifications: Whether Google emails the attendees. Default true.
    """
    provider = _provider(ctx)

    def work() -> EventResult:
        if not summary:
            raise CalendarToolError("summary is required.")
        creds = provider.get()
        start = _require(parse_datetime(start_time, "start_time", creds, calendar_id), "start_time")
        end = _require(parse_datetime(end_time, "end_time", creds, calendar_id), "end_time")
        if end <= start:
            raise CalendarToolError("end_time must be after start_time.")
        request = EventCreateRequest(
            summary=summary,
            start=EventDateTime(dateTime=start),
            end=EventDateTime(dateTime=end),
            description=description,
            location=location,
            attendees=attendee_emails or None,
        )
        created = calendar_actions.create_event(
            credentials=creds,
            event_data=request,
            calendar_id=calendar_id,
            send_notifications=send_notifications,
        )
        if created is None:
            raise _no_result("Creating the event")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(created),
            message=f"Created '{created.summary}' starting {start.isoformat()}.",
        )

    return await _run(work)


@server.tool(
    name="quick_add_event",
    title="Quick-add an event",
    annotations=WRITE,
)
async def quick_add_event(
    text: str,
    calendar_id: str = "primary",
    ctx: Optional[Context] = None,
) -> EventResult:
    """Create an event from a plain-English phrase, parsed by Google.

    Google interprets the date, time and title itself, in the calendar's own
    timezone. Check the returned start/end and tell the user what was booked --
    the parser guesses, and does not handle attendees or descriptions.

    Args:
        text: The phrase to parse, e.g. 'Dentist Thursday 9am' or
            'Team sync every Monday at 10 for 30 minutes'.
        calendar_id: Calendar to create the event on.
    """
    provider = _provider(ctx)

    def work() -> EventResult:
        if not text.strip():
            raise CalendarToolError("text is required.")
        creds = provider.get()
        created = calendar_actions.quick_add_event(
            credentials=creds,
            text=text,
            calendar_id=calendar_id,
        )
        if created is None:
            raise _no_result("Quick-adding the event")
        info = EventInfo.from_event(created)
        return EventResult(
            calendar_id=calendar_id,
            event=info,
            message=f"Google parsed {text!r} as '{info.summary}' starting {info.start}.",
        )

    return await _run(work)


@server.tool(
    name="update_event",
    title="Update an event",
    annotations=UPDATE,
)
async def update_event(
    calendar_id: str = "primary",
    event_id: str = "",
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    send_notifications: bool = True,
    ctx: Optional[Context] = None,
) -> EventResult:
    """Change fields on an existing event. Omitted fields are left untouched.

    To reschedule while keeping the duration, use `move_event` instead: changing
    only `start_time` here leaves the old end time in place.

    Args:
        calendar_id: Calendar the event lives on.
        event_id: The event to update (from `find_events`). Required.
        summary: New title.
        start_time: New start, ISO 8601.
        end_time: New end, ISO 8601.
        description: New description.
        location: New location.
        send_notifications: Whether Google emails the attendees. Default true.
    """
    provider = _provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise CalendarToolError("event_id is required.")
        creds = provider.get()
        start = parse_datetime(start_time, "start_time", creds, calendar_id)
        end = parse_datetime(end_time, "end_time", creds, calendar_id)
        if start and end and end <= start:
            raise CalendarToolError("end_time must be after start_time.")
        update = EventUpdateRequest(
            summary=summary,
            start=EventDateTime(dateTime=start) if start else None,
            end=EventDateTime(dateTime=end) if end else None,
            description=description,
            location=location,
        )
        if not update.model_dump(exclude_none=True):
            raise CalendarToolError(
                "Nothing to update: pass at least one of summary, start_time, "
                "end_time, description or location."
            )
        updated = calendar_actions.update_event(
            credentials=creds,
            event_id=event_id,
            update_data=update,
            calendar_id=calendar_id,
            send_notifications=send_notifications,
        )
        if updated is None:
            raise _no_result("Updating the event")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(updated),
            message=f"Updated event {event_id}.",
        )

    result = await _run(work)
    if start_time and not end_time:
        await _warn(
            ctx,
            "update_event changed only the start time; the end time is unchanged. "
            "Use move_event to shift an event and keep its duration.",
        )
    return result


@server.tool(
    name="move_event",
    title="Move or reschedule an event",
    annotations=UPDATE,
)
async def move_event(
    event_id: str,
    calendar_id: str = "primary",
    new_start: Optional[str] = None,
    new_end: Optional[str] = None,
    destination_calendar_id: Optional[str] = None,
    send_notifications: bool = True,
    ctx: Optional[Context] = None,
) -> EventResult:
    """Reschedule an event, and/or move it to a different calendar.

    Supplying only `new_start` keeps the event's original duration -- this is the
    right tool for "push my 2pm back an hour". Supplying
    `destination_calendar_id` transfers the event between calendars.

    Args:
        event_id: The event to move (from `find_events`).
        calendar_id: Calendar the event currently lives on.
        new_start: New start, ISO 8601. Duration is preserved if new_end is omitted.
        new_end: New end, ISO 8601. Optional when new_start is given.
        destination_calendar_id: Calendar to transfer the event to.
        send_notifications: Whether Google emails the attendees. Default true.
    """
    provider = _provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise CalendarToolError("event_id is required.")
        if not new_start and not new_end and not destination_calendar_id:
            raise CalendarToolError(
                "Pass at least one of new_start, new_end or destination_calendar_id."
            )
        creds = provider.get()
        start = parse_datetime(new_start, "new_start", creds, calendar_id)
        end = parse_datetime(new_end, "new_end", creds, calendar_id)
        if start and end and end <= start:
            raise CalendarToolError("new_end must be after new_start.")
        moved = calendar_actions.move_event(
            credentials=creds,
            event_id=event_id,
            calendar_id=calendar_id,
            new_start=start,
            new_end=end,
            destination_calendar_id=destination_calendar_id,
            send_notifications=send_notifications,
        )
        if moved is None:
            raise _no_result("Moving the event")
        info = EventInfo.from_event(moved)
        where = destination_calendar_id or calendar_id
        return EventResult(
            calendar_id=where,
            event=info,
            message=f"Event {event_id} now runs {info.start} to {info.end} on '{where}'.",
        )

    result = await _run(work)
    if new_start and not new_end and result.event.all_day:
        await _warn(
            ctx,
            "This is an all-day event, so no duration could be preserved; "
            "pass new_end explicitly if the length should change.",
        )
    return result


@server.tool(
    name="add_attendee",
    title="Add attendees",
    annotations=UPDATE,
)
async def add_attendee(
    event_id: str,
    attendee_emails: List[str],
    calendar_id: str = "primary",
    send_notifications: bool = True,
    ctx: Optional[Context] = None,
) -> EventResult:
    """Invite one or more people to an existing event.

    Existing attendees are kept; the new addresses are appended.

    Args:
        event_id: The event to invite people to (from `find_events`).
        attendee_emails: Email addresses to invite.
        calendar_id: Calendar the event lives on.
        send_notifications: Whether Google emails the attendees. Default true.
    """
    provider = _provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise CalendarToolError("event_id is required.")
        if not attendee_emails:
            raise CalendarToolError("attendee_emails must contain at least one address.")
        creds = provider.get()
        updated = calendar_actions.add_attendee(
            credentials=creds,
            event_id=event_id,
            attendee_emails=list(attendee_emails),
            calendar_id=calendar_id,
            send_notifications=send_notifications,
        )
        if updated is None:
            raise _no_result("Adding attendees")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(updated),
            message=f"Invited {', '.join(attendee_emails)} to event {event_id}.",
        )

    return await _run(work)


@server.tool(
    name="respond_to_event",
    title="RSVP to an event",
    annotations=UPDATE,
)
async def respond_to_event(
    event_id: str,
    response_status: str,
    calendar_id: str = "primary",
    comment: Optional[str] = None,
    send_notifications: bool = True,
    ctx: Optional[Context] = None,
) -> EventResult:
    """Set the user's own RSVP on an invitation they have received.

    Only works on events the user is an attendee of; it does not change anyone
    else's response.

    Args:
        event_id: The invitation to answer (from `find_events`).
        response_status: 'accepted', 'declined', 'tentative' or 'needsAction'.
        calendar_id: Calendar the invitation lives on.
        comment: Optional note sent to the organizer with the RSVP.
        send_notifications: Whether Google emails the organizer. Default true.
    """
    provider = _provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise CalendarToolError("event_id is required.")
        creds = provider.get()
        updated = calendar_actions.respond_to_event(
            credentials=creds,
            event_id=event_id,
            response_status=response_status,
            calendar_id=calendar_id,
            comment=comment,
            send_notifications=send_notifications,
        )
        if updated is None:
            raise _no_result("Responding to the event")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(updated),
            message=f"RSVP for event {event_id} set to '{response_status}'.",
        )

    return await _run(work)


@server.tool(
    name="schedule_mutual",
    title="Find a mutual slot and schedule",
    annotations=WRITE,
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
    ctx: Optional[Context] = None,
) -> EventResult:
    """Find the first slot where everyone is free, then book the meeting there.

    Reads each attendee's free/busy inside the window, picks the earliest gap
    that fits `duration_minutes`, and creates the event with all of them invited.
    Fails with an error if no common slot exists -- widen the window or shorten
    the meeting and try again.

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
    """
    provider = _provider(ctx)

    def work() -> EventResult:
        if duration_minutes <= 0:
            raise CalendarToolError("duration_minutes must be greater than zero.")
        if not attendee_calendar_ids:
            raise CalendarToolError("attendee_calendar_ids must contain at least one address.")
        creds = provider.get()
        start = _require(
            parse_datetime(time_min, "time_min", creds, organizer_calendar_id), "time_min"
        )
        end = _require(
            parse_datetime(time_max, "time_max", creds, organizer_calendar_id), "time_max"
        )
        if end - start < timedelta(minutes=duration_minutes):
            raise CalendarToolError(
                "The search window is shorter than duration_minutes; widen time_min/time_max."
            )
        placeholder = EventDateTime(dateTime=start)
        details = EventCreateRequest(
            summary=summary,
            start=placeholder,
            end=placeholder,
            description=description,
            location=location,
        )
        created = calendar_actions.find_mutual_availability_and_schedule(
            credentials=creds,
            attendee_calendar_ids=list(attendee_calendar_ids),
            time_min=start,
            time_max=end,
            duration_minutes=duration_minutes,
            event_details=details,
            organizer_calendar_id=organizer_calendar_id,
            working_hours_start=_parse_clock(working_hours_start, "working_hours_start"),
            working_hours_end=_parse_clock(working_hours_end, "working_hours_end"),
            send_notifications=send_notifications,
        )
        if created is None:
            raise CalendarToolError(
                f"No {duration_minutes}-minute slot is free for everyone between "
                f"{start.isoformat()} and {end.isoformat()}. Widen the window, shorten "
                "the meeting, or relax the working-hours bounds."
            )
        info = EventInfo.from_event(created)
        return EventResult(
            calendar_id=organizer_calendar_id,
            event=info,
            message=f"Booked '{summary}' at {info.start} with {len(attendee_calendar_ids)} attendee(s).",
        )

    return await _run(work)


# ---------------------------------------------------------------------------
# Deleting events (with optional confirmation)
# ---------------------------------------------------------------------------


class DeleteConfirmation(BaseModel):
    """Elicitation schema for the delete_event confirmation prompt."""

    confirm: bool = Field(
        default=False,
        description="Confirm permanently deleting this event.",
    )


async def _confirm_delete(ctx: Optional[Context], label: str) -> Optional[bool]:
    """Asks the client to confirm a deletion.

    Returns True/False when the client answered, and None when the client does
    not support elicitation (in which case the caller proceeds unprompted).
    """
    if ctx is None:
        return None
    try:
        result = await ctx.elicit(
            f"Permanently delete {label}? This cannot be undone.",
            DeleteConfirmation,
        )
    except Exception as exc:  # client without elicitation support
        logger.info("Skipping delete confirmation (client cannot elicit): %s", exc)
        return None

    action = getattr(result, "action", None)
    if action == "accept":
        data = getattr(result, "data", None)
        return bool(getattr(data, "confirm", False))
    if action in ("decline", "cancel"):
        return False
    return None


@server.tool(
    name="delete_event",
    title="Delete an event",
    annotations=DESTRUCTIVE,
)
async def delete_event(
    event_id: str,
    calendar_id: str = "primary",
    send_notifications: bool = True,
    ctx: Optional[Context] = None,
) -> DeleteEventResult:
    """Permanently delete an event. This cannot be undone.

    Clients that support elicitation are asked to confirm first; if the user
    declines, nothing is deleted and `deleted` comes back false.

    Args:
        event_id: The event to delete (from `find_events`).
        calendar_id: Calendar the event lives on.
        send_notifications: Whether Google emails the attendees that it was
            cancelled. Default true.
    """
    if not event_id:
        raise CalendarToolError("event_id is required.")

    confirmed = await _confirm_delete(ctx, f"event {event_id} on calendar '{calendar_id}'")
    if confirmed is False:
        return DeleteEventResult(
            calendar_id=calendar_id,
            event_id=event_id,
            deleted=False,
            confirmed_by_user=False,
            message="Deletion cancelled by the user; nothing was changed.",
        )

    provider = _provider(ctx)

    def work() -> bool:
        creds = provider.get()
        return bool(
            calendar_actions.delete_event(
                credentials=creds,
                event_id=event_id,
                calendar_id=calendar_id,
                send_notifications=send_notifications,
            )
        )

    deleted = await _run(work)
    if not deleted:
        raise _no_result("Deleting the event")
    return DeleteEventResult(
        calendar_id=calendar_id,
        event_id=event_id,
        deleted=True,
        confirmed_by_user=confirmed,
        message=f"Deleted event {event_id} from '{calendar_id}'.",
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_stdio() -> None:
    """Serves MCP over stdio (the default transport)."""
    logger.info("Serving calendar-mcp over stdio")
    server.run(transport="stdio")


def run_http(host: str = "127.0.0.1", port: int = 8000, path: str = "/mcp") -> None:
    """Serves MCP over streamable HTTP."""
    logger.info("Serving calendar-mcp over streamable-http at http://%s:%s%s", host, port, path)
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
    )


def http_app(path: str = "/mcp", host: str = "127.0.0.1"):
    """Returns the Starlette app, for mounting behind an existing ASGI server."""
    return server.streamable_http_app(streamable_http_path=path, host=host)


__all__ = [
    "server",
    "AppContext",
    "CalendarToolError",
    "CredentialProvider",
    "credential_provider",
    "lifespan",
    "parse_datetime",
    "run_stdio",
    "run_http",
    "http_app",
    "INSTRUCTIONS",
    "SERVER_NAME",
    "SERVER_VERSION",
]
