"""Tools that read and write individual calendar events."""

from __future__ import annotations

from typing import List, Optional

from calendar_mcp import server as srv
from calendar_mcp.models import (
    AttendeeStatusEntry,
    AttendeeStatusResult,
    DeleteEventResult,
    EventCreateRequest,
    EventDateTime,
    EventInfo,
    EventListResult,
    EventResult,
    EventUpdateRequest,
)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@srv.server.tool(
    name="find_events",
    title="Find events",
    annotations=srv.READ_ONLY,
)
async def find_events(
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 50,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
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
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> EventListResult:
        creds = provider.get(account)
        response = srv.calendar_actions.find_events(
            credentials=creds,
            calendar_id=calendar_id,
            time_min=srv.parse_datetime(time_min, "time_min", creds, calendar_id),
            time_max=srv.parse_datetime(time_max, "time_max", creds, calendar_id),
            query=query,
            max_results=max_results,
        )
        if response is None:
            raise srv._no_result("Finding events")
        events = [EventInfo.from_event(item) for item in response.items]
        return EventListResult(
            calendar_id=calendar_id,
            count=len(events),
            events=events,
            time_zone=response.timeZone,
        )

    result = await srv._run(work)
    if result.count >= max_results:
        await srv._warn(
            ctx,
            f"find_events hit the max_results limit of {max_results}; there may be more "
            "events in this window.",
        )
    return result


@srv.server.tool(
    name="check_attendee_status",
    title="Check attendee RSVPs",
    annotations=srv.READ_ONLY,
)
async def check_attendee_status(
    event_id: str,
    calendar_id: str = "primary",
    attendee_emails: Optional[List[str]] = None,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> AttendeeStatusResult:
    """Report who has accepted, declined or not yet answered an event invitation.

    Args:
        event_id: The event to inspect (from `find_events`).
        calendar_id: Calendar the event lives on.
        attendee_emails: Restrict the report to these addresses. Omit for all.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> AttendeeStatusResult:
        creds = provider.get(account)
        statuses = srv.calendar_actions.check_attendee_status(
            credentials=creds,
            event_id=event_id,
            calendar_id=calendar_id,
            attendee_emails=attendee_emails,
        )
        if statuses is None:
            raise srv._no_result("Checking attendee status")
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

    return await srv._run(work)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@srv.server.tool(
    name="create_event",
    title="Create an event",
    annotations=srv.WRITE,
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
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
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
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> EventResult:
        if not summary:
            raise srv.CalendarToolError("summary is required.")
        creds = provider.get(account)
        start = srv._require(
            srv.parse_datetime(start_time, "start_time", creds, calendar_id), "start_time"
        )
        end = srv._require(
            srv.parse_datetime(end_time, "end_time", creds, calendar_id), "end_time"
        )
        if end <= start:
            raise srv.CalendarToolError("end_time must be after start_time.")
        request = EventCreateRequest(
            summary=summary,
            start=EventDateTime(dateTime=start),
            end=EventDateTime(dateTime=end),
            description=description,
            location=location,
            attendees=attendee_emails or None,
        )
        created = srv.calendar_actions.create_event(
            credentials=creds,
            event_data=request,
            calendar_id=calendar_id,
            send_notifications=send_notifications,
        )
        if created is None:
            raise srv._no_result("Creating the event")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(created),
            message=f"Created '{created.summary}' starting {start.isoformat()}.",
        )

    return await srv._run(work)


@srv.server.tool(
    name="quick_add_event",
    title="Quick-add an event",
    annotations=srv.WRITE,
)
async def quick_add_event(
    text: str,
    calendar_id: str = "primary",
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> EventResult:
    """Create an event from a plain-English phrase, parsed by Google.

    Google interprets the date, time and title itself, in the calendar's own
    timezone. Check the returned start/end and tell the user what was booked --
    the parser guesses, and does not handle attendees or descriptions.

    Args:
        text: The phrase to parse, e.g. 'Dentist Thursday 9am' or
            'Team sync every Monday at 10 for 30 minutes'.
        calendar_id: Calendar to create the event on.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> EventResult:
        if not text.strip():
            raise srv.CalendarToolError("text is required.")
        creds = provider.get(account)
        created = srv.calendar_actions.quick_add_event(
            credentials=creds,
            text=text,
            calendar_id=calendar_id,
        )
        if created is None:
            raise srv._no_result("Quick-adding the event")
        info = EventInfo.from_event(created)
        return EventResult(
            calendar_id=calendar_id,
            event=info,
            message=f"Google parsed {text!r} as '{info.summary}' starting {info.start}.",
        )

    return await srv._run(work)


@srv.server.tool(
    name="update_event",
    title="Update an event",
    annotations=srv.UPDATE,
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
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
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
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise srv.CalendarToolError("event_id is required.")
        creds = provider.get(account)
        start = srv.parse_datetime(start_time, "start_time", creds, calendar_id)
        end = srv.parse_datetime(end_time, "end_time", creds, calendar_id)
        if start and end and end <= start:
            raise srv.CalendarToolError("end_time must be after start_time.")
        update = EventUpdateRequest(
            summary=summary,
            start=EventDateTime(dateTime=start) if start else None,
            end=EventDateTime(dateTime=end) if end else None,
            description=description,
            location=location,
        )
        if not update.model_dump(exclude_none=True):
            raise srv.CalendarToolError(
                "Nothing to update: pass at least one of summary, start_time, "
                "end_time, description or location."
            )
        updated = srv.calendar_actions.update_event(
            credentials=creds,
            event_id=event_id,
            update_data=update,
            calendar_id=calendar_id,
            send_notifications=send_notifications,
        )
        if updated is None:
            raise srv._no_result("Updating the event")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(updated),
            message=f"Updated event {event_id}.",
        )

    result = await srv._run(work)
    if start_time and not end_time:
        await srv._warn(
            ctx,
            "update_event changed only the start time; the end time is unchanged. "
            "Use move_event to shift an event and keep its duration.",
        )
    return result


@srv.server.tool(
    name="move_event",
    title="Move or reschedule an event",
    annotations=srv.UPDATE,
)
async def move_event(
    event_id: str,
    calendar_id: str = "primary",
    new_start: Optional[str] = None,
    new_end: Optional[str] = None,
    destination_calendar_id: Optional[str] = None,
    send_notifications: bool = True,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
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
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise srv.CalendarToolError("event_id is required.")
        if not new_start and not new_end and not destination_calendar_id:
            raise srv.CalendarToolError(
                "Pass at least one of new_start, new_end or destination_calendar_id."
            )
        creds = provider.get(account)
        start = srv.parse_datetime(new_start, "new_start", creds, calendar_id)
        end = srv.parse_datetime(new_end, "new_end", creds, calendar_id)
        if start and end and end <= start:
            raise srv.CalendarToolError("new_end must be after new_start.")
        moved = srv.calendar_actions.move_event(
            credentials=creds,
            event_id=event_id,
            calendar_id=calendar_id,
            new_start=start,
            new_end=end,
            destination_calendar_id=destination_calendar_id,
            send_notifications=send_notifications,
        )
        if moved is None:
            raise srv._no_result("Moving the event")
        info = EventInfo.from_event(moved)
        where = destination_calendar_id or calendar_id
        return EventResult(
            calendar_id=where,
            event=info,
            message=f"Event {event_id} now runs {info.start} to {info.end} on '{where}'.",
        )

    result = await srv._run(work)
    if new_start and not new_end and result.event.all_day:
        await srv._warn(
            ctx,
            "This is an all-day event, so no duration could be preserved; "
            "pass new_end explicitly if the length should change.",
        )
    return result


@srv.server.tool(
    name="add_attendee",
    title="Add attendees",
    annotations=srv.UPDATE,
)
async def add_attendee(
    event_id: str,
    attendee_emails: List[str],
    calendar_id: str = "primary",
    send_notifications: bool = True,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> EventResult:
    """Invite one or more people to an existing event.

    Existing attendees are kept; the new addresses are appended.

    Args:
        event_id: The event to invite people to (from `find_events`).
        attendee_emails: Email addresses to invite.
        calendar_id: Calendar the event lives on.
        send_notifications: Whether Google emails the attendees. Default true.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise srv.CalendarToolError("event_id is required.")
        if not attendee_emails:
            raise srv.CalendarToolError("attendee_emails must contain at least one address.")
        creds = provider.get(account)
        updated = srv.calendar_actions.add_attendee(
            credentials=creds,
            event_id=event_id,
            attendee_emails=list(attendee_emails),
            calendar_id=calendar_id,
            send_notifications=send_notifications,
        )
        if updated is None:
            raise srv._no_result("Adding attendees")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(updated),
            message=f"Invited {', '.join(attendee_emails)} to event {event_id}.",
        )

    return await srv._run(work)


@srv.server.tool(
    name="respond_to_event",
    title="RSVP to an event",
    annotations=srv.UPDATE,
)
async def respond_to_event(
    event_id: str,
    response_status: str,
    calendar_id: str = "primary",
    comment: Optional[str] = None,
    send_notifications: bool = True,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
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
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    provider = srv._provider(ctx)

    def work() -> EventResult:
        if not event_id:
            raise srv.CalendarToolError("event_id is required.")
        creds = provider.get(account)
        updated = srv.calendar_actions.respond_to_event(
            credentials=creds,
            event_id=event_id,
            response_status=response_status,
            calendar_id=calendar_id,
            comment=comment,
            send_notifications=send_notifications,
        )
        if updated is None:
            raise srv._no_result("Responding to the event")
        return EventResult(
            calendar_id=calendar_id,
            event=EventInfo.from_event(updated),
            message=f"RSVP for event {event_id} set to '{response_status}'.",
        )

    return await srv._run(work)


# ---------------------------------------------------------------------------
# Deleting (with optional confirmation)
# ---------------------------------------------------------------------------


@srv.server.tool(
    name="delete_event",
    title="Delete an event",
    annotations=srv.DESTRUCTIVE,
)
async def delete_event(
    event_id: str,
    calendar_id: str = "primary",
    send_notifications: bool = True,
    account: Optional[str] = None,
    ctx: Optional[srv.Context] = None,
) -> DeleteEventResult:
    """Permanently delete an event. This cannot be undone.

    Clients that support elicitation are asked to confirm first; if the user
    declines, nothing is deleted and `deleted` comes back false.

    Args:
        event_id: The event to delete (from `find_events`).
        calendar_id: Calendar the event lives on.
        send_notifications: Whether Google emails the attendees that it was
            cancelled. Default true.
        account: Account name from 'calendar-mcp accounts'; omit for the default.
    """
    if not event_id:
        raise srv.CalendarToolError("event_id is required.")

    confirmed = await srv._confirm_delete(ctx, f"event {event_id} on calendar '{calendar_id}'")
    if confirmed is False:
        return DeleteEventResult(
            calendar_id=calendar_id,
            event_id=event_id,
            deleted=False,
            confirmed_by_user=False,
            message="Deletion cancelled by the user; nothing was changed.",
        )

    provider = srv._provider(ctx)

    def work() -> bool:
        creds = provider.get(account)
        return bool(
            srv.calendar_actions.delete_event(
                credentials=creds,
                event_id=event_id,
                calendar_id=calendar_id,
                send_notifications=send_notifications,
            )
        )

    deleted = await srv._run(work)
    if not deleted:
        raise srv._no_result("Deleting the event")
    return DeleteEventResult(
        calendar_id=calendar_id,
        event_id=event_id,
        deleted=True,
        confirmed_by_user=confirmed,
        message=f"Deleted event {event_id} from '{calendar_id}'.",
    )
