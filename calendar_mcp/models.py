import datetime # Import the module itself
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List, Dict, Any
# from datetime import datetime, date # Keep original import commented for reference

# Based on Google Calendar API v3 Event resource documentation:
# https://developers.google.com/calendar/api/v3/reference/events#resource

class EventDateTime(BaseModel):
    """Represents the start or end time of an event."""
    date: Optional[datetime.date] = None
    dateTime: Optional[datetime.datetime] = None  # Renamed from 'date_time' to match API JSON
    timeZone: Optional[str] = None  # Renamed from 'time_zone'

    class Config:
        populate_by_name = True  # Changed from allow_population_by_field_name
        # orm_mode = True # Removed, orm_mode is deprecated in Pydantic V2, use from_attributes=True

class EventAttendee(BaseModel):
    """Represents an attendee of an event."""
    id: Optional[str] = None
    email: Optional[EmailStr] = None
    displayName: Optional[str] = None  # Renamed from 'display_name'
    organizer: Optional[bool] = None
    self: Optional[bool] = None
    resource: Optional[bool] = None
    optional: Optional[bool] = None
    responseStatus: Optional[str] = None  # Renamed from 'response_status'
    comment: Optional[str] = None
    additionalGuests: Optional[int] = None  # Renamed from 'additional_guests'

    class Config:
        populate_by_name = True  # Changed from allow_population_by_field_name
        # orm_mode = True # Removed, orm_mode is deprecated in Pydantic V2, use from_attributes=True

class EventCreator(BaseModel):
    """Represents the creator of an event."""
    id: Optional[str] = None
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(None, alias='displayName')
    self: Optional[bool] = None # Whether the creator corresponds to the calendar on which this copy of the event appears.

    class Config:
        populate_by_name = True

class EventOrganizer(BaseModel):
    """Represents the organizer of an event."""
    id: Optional[str] = None
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(None, alias='displayName')
    self: Optional[bool] = None # Whether the organizer corresponds to the calendar on which this copy of the event appears.

    class Config:
        populate_by_name = True

class EventReminderOverride(BaseModel):
    method: Optional[str] = None
    minutes: Optional[int] = None

    class Config:
        populate_by_name = True  # Changed from allow_population_by_field_name
        # orm_mode = True # Removed, orm_mode is deprecated in Pydantic V2, use from_attributes=True

class EventReminders(BaseModel):
    useDefault: bool = Field(..., alias="useDefault")  # Renamed from 'use_default'
    overrides: Optional[List[EventReminderOverride]] = None

    class Config:
        populate_by_name = True  # Changed from allow_population_by_field_name
        # orm_mode = True # Removed, orm_mode is deprecated in Pydantic V2, use from_attributes=True

# --- Main Event Model --- 

class GoogleCalendarEvent(BaseModel):
    """Pydantic model representing a Google Calendar event resource."""
    kind: str = "calendar#event"
    id: Optional[str] = Field(None, description="Opaque identifier of the event.")
    status: Optional[str] = Field(None, description="Status of the event ('confirmed', 'tentative', 'cancelled').")
    html_link: Optional[str] = Field(None, alias='htmlLink', description="URL for the event in the Google Calendar UI.")
    created: Optional[datetime.datetime] = Field(None, description="Creation time of the event (RFC3339 format).")
    updated: Optional[datetime.datetime] = Field(None, description="Last modification time of the event (RFC3339 format).")
    summary: Optional[str] = Field(None, description="Title of the event.")
    description: Optional[str] = Field(None, description="Description of the event. Optional.")
    location: Optional[str] = Field(None, description="Geographic location of the event. Optional.")
    color_id: Optional[str] = Field(None, alias='colorId', description="Color of the event. Optional.")
    transparency: Optional[str] = Field(None, description="Whether the event blocks time on the calendar: 'opaque' (busy, the default) or 'transparent' (free).")
    creator: Optional[EventCreator] = Field(None, description="The creator of the event. Read-only.")
    organizer: Optional[EventOrganizer] = Field(None, description="The organizer of the event.")
    start: Optional[EventDateTime] = Field(None, description="The start time of the event.")
    end: Optional[EventDateTime] = Field(None, description="The end time of the event.")
    end_time_unspecified: Optional[bool] = Field(None, alias='endTimeUnspecified', description="Whether the end time is actually unspecified.")
    recurrence: Optional[List[str]] = Field(None, description="List of RRULE, EXRULE, RDATE or EXDATE properties for recurring events.")
    recurring_event_id: Optional[str] = Field(None, alias='recurringEventId', description="For an instance of a recurring event, this is the id of the recurring event itself.")
    original_start_time: Optional[EventDateTime] = Field(None, alias='originalStartTime', description="For an instance of a recurring event, this is the original start time of the instance before modification.")
    attendees: Optional[List[EventAttendee]] = Field([], description="The attendees of the event.")
    attendees_omitted: Optional[bool] = Field(None, alias='attendeesOmitted', description="Whether attendees were omitted.")
    reminders: Optional[EventReminders] = Field(None, description="Information about the event's reminders.")
    # Add other fields as needed (e.g., attachments, conferenceData, gadget, source, etc.)

    class Config:
        populate_by_name = True
        # Consider adding validation logic, e.g., ensuring start is before end

# --- Models for API Requests/Responses --- 

class EventCreateRequest(BaseModel):
    """Model for the request body when creating a detailed event."""
    summary: str
    start: EventDateTime
    end: EventDateTime
    description: Optional[str] = None
    location: Optional[str] = None
    attendees: Optional[List[EmailStr]] = Field(None, description="List of attendee email addresses to invite.")
    recurrence: Optional[List[str]] = Field(None, description="List of RRULEs, EXRULEs, RDATEs or EXDATEs for recurring events.")
    reminders: Optional[EventReminders] = Field(None, description="Notification settings for the event.")
    # Add other creatable fields as needed

class QuickAddEventRequest(BaseModel):
    """Model for the request body when using the quickAdd endpoint."""
    text: str = Field(..., description="The text describing the event to be parsed by Google Calendar.")

class EventUpdateRequest(BaseModel):
    """Model for the request body when updating an event.
       Contains only the fields that can be updated.
    """
    summary: Optional[str] = None
    start: Optional[EventDateTime] = None
    end: Optional[EventDateTime] = None
    description: Optional[str] = None
    location: Optional[str] = None
    attendees: Optional[List[EventAttendee]] = None # Allow updating attendee details or list
    # Add other updatable fields

class AddAttendeeRequest(BaseModel):
    """Model for adding attendees to an existing event."""
    attendee_emails: List[EmailStr] = Field(..., description="List of email addresses to add as attendees.")

# You might also want models for CalendarList entries, etc.

# Define NotificationSettings first as it's used in CalendarListEntry
class NotificationSettings(BaseModel):
    """Represents notification settings for a calendar."""
    notifications: Optional[List[Dict[str, str]]] = None # List of {'type': 'eventCreation', 'method': 'email'} etc.

    class Config:
        populate_by_name = True # Changed from allow_population_by_field_name

class CalendarListEntry(BaseModel):
    """Represents an entry in the user's calendar list."""
    kind: str = "calendar#calendarListEntry"
    etag: str
    id: str
    summary: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    timeZone: Optional[str] = None # Renamed from 'time_zone'
    summaryOverride: Optional[str] = None # Renamed from 'summary_override'
    colorId: Optional[str] = None # Renamed from 'color_id'
    backgroundColor: Optional[str] = None # Renamed from 'background_color'
    foregroundColor: Optional[str] = None # Renamed from 'foreground_color'
    hidden: Optional[bool] = None
    selected: Optional[bool] = None
    accessRole: Optional[str] = None # Renamed from 'access_role'
    defaultReminders: Optional[List[EventReminderOverride]] = None # Renamed from 'default_reminders'
    notificationSettings: Optional[NotificationSettings] = None # Renamed from 'notification_settings'
    primary: Optional[bool] = None
    deleted: Optional[bool] = None

class CalendarListResponse(BaseModel):
    """Response containing a list of calendars."""
    kind: str = "calendar#calendarList"
    items: List[CalendarListEntry] = []
    nextPageToken: Optional[str] = None
    nextSyncToken: Optional[str] = None

# Re-inserting EventsResponse definition
class EventsResponse(BaseModel):
    """Response containing a list of events."""
    kind: str = "calendar#events"
    summary: Optional[str] = None
    description: Optional[str] = None
    updated: Optional[datetime.datetime] = None
    timeZone: Optional[str] = None
    accessRole: Optional[str] = None
    defaultReminders: Optional[List[EventReminderOverride]] = []
    items: List[GoogleCalendarEvent] = []
    nextPageToken: Optional[str] = None
    nextSyncToken: Optional[str] = None

class CalendarList(BaseModel):
    """Represents the user's list of calendars."""
    kind: str = "calendar#calendarList"
    etag: str
    nextPageToken: Optional[str] = None # Renamed from 'next_page_token'
    nextSyncToken: Optional[str] = None # Renamed from 'next_sync_token'
    items: List[CalendarListEntry]

    class Config:
        populate_by_name = True # Changed from allow_population_by_field_name

# --- Models for Advanced Actions --- 

# --- Check Attendee Status ---
class CheckAttendeeStatusRequest(BaseModel):
    event_id: str
    calendar_id: str = 'primary'
    attendee_emails: Optional[List[EmailStr]] = None

class CheckAttendeeStatusResponse(BaseModel):
    status_map: Dict[EmailStr, str] = Field(..., description="Mapping of attendee email to their responseStatus ('accepted', 'declined', etc.)")

# --- Find Availability (Free/Busy) ---
class FreeBusyRequestItem(BaseModel):
    id: str # Calendar ID

class FreeBusyRequest(BaseModel):
    time_min: datetime.datetime = Field(..., alias='timeMin')
    time_max: datetime.datetime = Field(..., alias='timeMax')
    items: List[FreeBusyRequestItem]
    # Optional: timeZone, groupExpansionMax, calendarExpansionMax
    time_zone: Optional[str] = Field(None, alias='timeZone')

    class Config:
        populate_by_name = True

class TimePeriod(BaseModel):
    start: datetime.datetime
    end: datetime.datetime

class FreeBusyError(BaseModel):
    domain: str
    reason: str

class CalendarBusyInfo(BaseModel):
    errors: Optional[List[FreeBusyError]] = None
    busy: List[TimePeriod] = []

class FreeBusyResponse(BaseModel):
    kind: str = "calendar#freeBusy"
    time_min: datetime.datetime = Field(..., alias='timeMin')
    time_max: datetime.datetime = Field(..., alias='timeMax')
    calendars: Dict[str, CalendarBusyInfo] = {}
    # Optional: groups

    class Config:
        populate_by_name = True

# --- Find Mutual Availability & Schedule ---
class ScheduleMutualRequest(BaseModel):
    attendee_calendar_ids: List[str] = Field(..., description="List of calendar IDs (usually emails) for attendees whose availability should be checked.")
    time_min: datetime.datetime
    time_max: datetime.datetime
    duration_minutes: int
    event_details: EventCreateRequest # Use the existing model for core event info
    organizer_calendar_id: str = 'primary'
    working_hours_start_str: Optional[str] = Field(None, description="Optional start time for working hours constraint (HH:MM format)")
    working_hours_end_str: Optional[str] = Field(None, description="Optional end time for working hours constraint (HH:MM format)")
    send_notifications: bool = True

# Response is GoogleCalendarEvent

# --- Project Recurring Events ---
class ProjectRecurringRequest(BaseModel):
    time_min: datetime.datetime
    time_max: datetime.datetime
    calendar_id: str = 'primary'
    event_query: Optional[str] = None

# Define ProjectedEventOccurrence within models.py for consistency
class ProjectedEventOccurrenceModel(BaseModel):
    original_event_id: str
    original_summary: str
    occurrence_start: datetime.datetime
    occurrence_end: datetime.datetime

class ProjectRecurringResponse(BaseModel):
    projected_occurrences: List[ProjectedEventOccurrenceModel]

# --- Analyze Busyness ---
class AnalyzeBusynessRequest(BaseModel):
    time_min: datetime.datetime
    time_max: datetime.datetime
    calendar_id: str = 'primary'

class DailyBusynessStats(BaseModel):
    event_count: int
    total_duration_minutes: float

class AnalyzeBusynessResponse(BaseModel):
    # Use string representation for date keys in JSON
    busyness_by_date: Dict[str, DailyBusynessStats] = Field(..., description="Mapping of date string (YYYY-MM-DD) to busyness stats") 

# ---------------------------------------------------------------------------
# MCP tool result models
#
# These are the *structured output* schemas the MCP tools in
# ``calendar_mcp.server`` declare as their return types. They are deliberately
# flatter and smaller than the raw Google resources above: every timestamp is a
# plain ISO 8601 string so the shape survives JSON round-tripping unchanged, and
# only the fields an assistant actually reasons about are included.
# ---------------------------------------------------------------------------


def _iso(value: Any) -> Optional[str]:
    """Renders a date/datetime (or passes through a string) as ISO 8601."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value)


class AttendeeInfo(BaseModel):
    """One attendee of an event, flattened for tool output."""
    email: Optional[str] = Field(None, description="Attendee email address.")
    display_name: Optional[str] = Field(None, description="Attendee display name, if Google knows one.")
    response_status: Optional[str] = Field(
        None, description="RSVP state: 'accepted', 'declined', 'tentative' or 'needsAction'."
    )
    optional: bool = Field(False, description="True when attendance is marked optional.")
    organizer: bool = Field(False, description="True when this attendee organises the event.")
    is_self: bool = Field(False, description="True when this attendee is the authenticated user.")

    @classmethod
    def from_attendee(cls, attendee: "EventAttendee") -> "AttendeeInfo":
        return cls(
            email=str(attendee.email) if attendee.email else None,
            display_name=attendee.displayName,
            response_status=attendee.responseStatus,
            optional=bool(attendee.optional),
            organizer=bool(attendee.organizer),
            is_self=bool(getattr(attendee, "self", False)),
        )


class EventInfo(BaseModel):
    """A calendar event, flattened for tool output."""
    id: Optional[str] = Field(None, description="Event ID. Pass this to update/move/delete tools.")
    summary: Optional[str] = Field(None, description="Event title.")
    description: Optional[str] = Field(None, description="Event description / notes.")
    location: Optional[str] = Field(None, description="Event location.")
    start: Optional[str] = Field(
        None, description="Start as ISO 8601 (date-time with offset, or a plain date for all-day events)."
    )
    end: Optional[str] = Field(None, description="End as ISO 8601, same convention as 'start'.")
    all_day: bool = Field(False, description="True when the event occupies whole days rather than a time range.")
    time_zone: Optional[str] = Field(None, description="IANA timezone the event's start is expressed in, if given.")
    status: Optional[str] = Field(None, description="'confirmed', 'tentative' or 'cancelled'.")
    html_link: Optional[str] = Field(None, description="Link to the event in the Google Calendar web UI.")
    organizer_email: Optional[str] = Field(None, description="Email of the event organizer.")
    attendees: List[AttendeeInfo] = Field(default_factory=list, description="Invited attendees and their RSVPs.")
    recurrence: Optional[List[str]] = Field(None, description="RRULE/EXDATE lines when this is a recurring master event.")
    recurring_event_id: Optional[str] = Field(
        None, description="ID of the master event when this is one instance of a recurring series."
    )

    @classmethod
    def from_event(cls, event: "GoogleCalendarEvent") -> "EventInfo":
        """Builds an EventInfo from the richer GoogleCalendarEvent model."""
        start, end = event.start, event.end
        all_day = bool(start and start.dateTime is None and start.date is not None)
        return cls(
            id=event.id,
            summary=event.summary,
            description=event.description,
            location=event.location,
            start=_iso(start.dateTime or start.date) if start else None,
            end=_iso(end.dateTime or end.date) if end else None,
            all_day=all_day,
            time_zone=start.timeZone if start else None,
            status=event.status,
            html_link=event.html_link,
            organizer_email=(
                str(event.organizer.email) if event.organizer and event.organizer.email else None
            ),
            attendees=[AttendeeInfo.from_attendee(a) for a in (event.attendees or [])],
            recurrence=event.recurrence,
            recurring_event_id=event.recurring_event_id,
        )


class EventResult(BaseModel):
    """Result of a tool that creates, updates, moves or RSVPs to a single event."""
    calendar_id: str = Field(..., description="Calendar the event lives on.")
    event: EventInfo = Field(..., description="The event after the operation.")
    message: str = Field("", description="One-line human-readable summary of what happened.")


class EventListResult(BaseModel):
    """Result of a tool that returns several events."""
    calendar_id: str = Field(..., description="Calendar that was searched.")
    count: int = Field(..., description="Number of events returned.")
    events: List[EventInfo] = Field(default_factory=list, description="The matching events, earliest first.")
    time_zone: Optional[str] = Field(None, description="IANA timezone of the calendar that was searched.")


class CalendarInfo(BaseModel):
    """One calendar from the user's calendar list."""
    id: str = Field(..., description="Calendar ID. Pass this as calendar_id to other tools.")
    summary: Optional[str] = Field(None, description="Calendar name.")
    description: Optional[str] = Field(None, description="Calendar description.")
    time_zone: Optional[str] = Field(None, description="IANA timezone of the calendar, e.g. 'Europe/Berlin'.")
    access_role: Optional[str] = Field(None, description="The user's role: 'owner', 'writer', 'reader', 'freeBusyReader'.")
    primary: bool = Field(False, description="True for the user's main calendar (also addressable as 'primary').")

    @classmethod
    def from_entry(cls, entry: "CalendarListEntry") -> "CalendarInfo":
        return cls(
            id=entry.id,
            summary=entry.summaryOverride or entry.summary,
            description=entry.description,
            time_zone=entry.timeZone,
            access_role=entry.accessRole,
            primary=bool(entry.primary),
        )


class CalendarListResult(BaseModel):
    """Result of the list_calendars tool."""
    count: int = Field(..., description="Number of calendars returned.")
    calendars: List[CalendarInfo] = Field(default_factory=list, description="The user's calendars.")


class DeleteEventResult(BaseModel):
    """Result of the delete_event tool."""
    calendar_id: str = Field(..., description="Calendar the event was on.")
    event_id: str = Field(..., description="ID of the deleted event.")
    deleted: bool = Field(..., description="True when the event was removed.")
    confirmed_by_user: Optional[bool] = Field(
        None,
        description="True/False when the client answered a confirmation prompt; null when the client does not support elicitation.",
    )
    message: str = Field("", description="One-line human-readable summary of what happened.")


class AttendeeStatusEntry(BaseModel):
    """One attendee's RSVP state."""
    email: str = Field(..., description="Attendee email address.")
    response_status: str = Field(
        ..., description="'accepted', 'declined', 'tentative' or 'needsAction'."
    )


class AttendeeStatusResult(BaseModel):
    """Result of the check_attendee_status tool."""
    calendar_id: str = Field(..., description="Calendar the event lives on.")
    event_id: str = Field(..., description="Event that was checked.")
    count: int = Field(..., description="Number of attendees reported.")
    attendees: List[AttendeeStatusEntry] = Field(
        default_factory=list, description="Attendees and their current RSVP state."
    )


class BusyPeriod(BaseModel):
    """A single busy interval."""
    start: str = Field(..., description="Interval start, ISO 8601.")
    end: str = Field(..., description="Interval end, ISO 8601.")


class CalendarBusyPeriods(BaseModel):
    """Busy intervals for one calendar."""
    calendar_id: str = Field(..., description="Calendar these intervals belong to.")
    busy: List[BusyPeriod] = Field(default_factory=list, description="Busy intervals, merged and sorted.")
    errors: List[str] = Field(
        default_factory=list,
        description="Reasons this calendar could not be read (e.g. 'notFound', 'forbidden'), if any.",
    )


class FreeBusyResult(BaseModel):
    """Result of the query_free_busy tool."""
    time_min: str = Field(..., description="Start of the queried window, ISO 8601.")
    time_max: str = Field(..., description="End of the queried window, ISO 8601.")
    calendars: List[CalendarBusyPeriods] = Field(
        default_factory=list, description="Per-calendar busy intervals."
    )


class DayBusyness(BaseModel):
    """Aggregated event load for a single day."""
    date: str = Field(..., description="The day, as YYYY-MM-DD.")
    event_count: int = Field(..., description="Number of events that day.")
    total_duration_minutes: float = Field(..., description="Total scheduled minutes that day.")


class BusynessResult(BaseModel):
    """Result of the analyze_busyness tool."""
    calendar_id: str = Field(..., description="Calendar that was analysed.")
    time_min: str = Field(..., description="Start of the analysed window, ISO 8601.")
    time_max: str = Field(..., description="End of the analysed window, ISO 8601.")
    days: List[DayBusyness] = Field(default_factory=list, description="Per-day totals, in date order.")
    total_events: int = Field(0, description="Sum of event_count across all days.")
    total_duration_minutes: float = Field(0.0, description="Sum of total_duration_minutes across all days.")


class ProjectedOccurrence(BaseModel):
    """One computed occurrence of a recurring event."""
    event_id: str = Field(..., description="ID of the master recurring event.")
    summary: str = Field(..., description="Title of the recurring event.")
    start: str = Field(..., description="Occurrence start, ISO 8601.")
    end: str = Field(..., description="Occurrence end, ISO 8601.")


class ProjectedEventsResult(BaseModel):
    """Result of the project_recurring_events tool."""
    calendar_id: str = Field(..., description="Calendar that was projected.")
    time_min: str = Field(..., description="Start of the projection window, ISO 8601.")
    time_max: str = Field(..., description="End of the projection window, ISO 8601.")
    count: int = Field(..., description="Number of projected occurrences.")
    occurrences: List[ProjectedOccurrence] = Field(
        default_factory=list, description="Computed occurrences, earliest first."
    )


# ---------------------------------------------------------------------------
# Accounts
#
# calendar-mcp can hold a signed-in token per named account ("default", "work",
# "personal", ...). These models are the structured output of the
# ``list_accounts`` tool; the account bookkeeping itself lives in
# :mod:`calendar_mcp.accounts`.
# ---------------------------------------------------------------------------


class AccountInfo(BaseModel):
    """One named Google account calendar-mcp holds (or could hold) a token for."""
    name: str = Field(..., description="Account name. Pass this as the 'account' argument of any tool.")
    token_path: str = Field(..., description="Absolute path of the file this account's OAuth token is cached in.")
    valid: bool = Field(
        False,
        description="True when a usable saved token exists. False means 'calendar-mcp auth --account <name>' is needed.",
    )
    email: Optional[str] = Field(
        None, description="Google account address, when the saved token records one."
    )
    is_default: bool = Field(
        False, description="True for the account used when a tool's 'account' argument is omitted."
    )


class AccountListResult(BaseModel):
    """Result of the list_accounts tool."""
    count: int = Field(..., description="Number of known accounts.")
    default_account: str = Field(..., description="Account used when 'account' is omitted.")
    config_dir: str = Field(..., description="Directory holding the account tokens and preferences.")
    accounts: List[AccountInfo] = Field(
        default_factory=list, description="Known accounts, the default one first."
    )


# ---------------------------------------------------------------------------
# Time audit  (appended by the time_audit agent -- keep this block together)
#
# Structured output of the ``time_audit`` tool. The maths that fills these in
# lives in :mod:`calendar_mcp.audit`; nothing here touches Google or the disk.
# All durations are hours (float, rounded to 2dp) and all shares are fractions
# in 0..1 rather than percentages.
# ---------------------------------------------------------------------------


class AuditPeriod(BaseModel):
    """Meeting load for one day or one ISO week."""
    period: str = Field(..., description="Period label: 'YYYY-MM-DD' for a day, 'YYYY-Www' for a week.")
    start: str = Field(..., description="Inclusive start of the period, ISO 8601.")
    end: str = Field(..., description="Exclusive end of the period, ISO 8601.")
    meeting_count: int = Field(0, description="Meetings that overlap this period.")
    meeting_hours: float = Field(0.0, description="Total meeting hours inside this period.")
    working_hours: float = Field(0.0, description="Working hours available in this period, lunch removed.")
    meeting_hours_in_working_hours: float = Field(
        0.0, description="Meeting hours that fall inside the working hours (overlaps merged)."
    )
    share_of_working_hours: float = Field(
        0.0, description="meeting_hours_in_working_hours / working_hours, 0..1."
    )


class AuditBucket(BaseModel):
    """One slice of the meeting time: a size class, a domain, or recurring/one-off."""
    label: str = Field(..., description="Bucket name, e.g. '1:1', 'large', 'corp.com', 'recurring'.")
    meeting_count: int = Field(0, description="Meetings counted in this bucket.")
    hours: float = Field(0.0, description="Meeting hours in this bucket.")
    share_of_meeting_hours: float = Field(
        0.0, description="Bucket hours / total meeting hours, 0..1. Domain buckets can sum above 1."
    )


class AuditPerson(BaseModel):
    """How much time the user spent in meetings with one other person."""
    email: str = Field(..., description="Attendee address, lower-cased.")
    meeting_count: int = Field(0, description="Meetings shared with this person.")
    hours: float = Field(0.0, description="Meeting hours shared with this person.")


class AuditStretch(BaseModel):
    """A run of three or more meetings with no usable gap between them."""
    date: str = Field(..., description="Local date the stretch starts on, 'YYYY-MM-DD'.")
    start: str = Field(..., description="Start of the first meeting, ISO 8601.")
    end: str = Field(..., description="End of the last meeting, ISO 8601.")
    meeting_count: int = Field(0, description="Meetings in the stretch.")
    hours: float = Field(0.0, description="Wall-clock length of the whole stretch.")


class AuditFocusBlock(BaseModel):
    """An unbooked stretch of working time long enough to be useful."""
    start: str = Field(..., description="Start of the free block, ISO 8601.")
    end: str = Field(..., description="End of the free block, ISO 8601.")
    hours: float = Field(0.0, description="Length of the block in hours.")


class AuditExclusions(BaseModel):
    """Events that were seen but deliberately not counted as meeting time."""
    all_day: int = Field(0, description="All-day events skipped (pass include_all_day to count them).")
    declined: int = Field(0, description="Meetings the user declined.")
    marked_free: int = Field(0, description="Events marked 'free' rather than 'busy'.")
    outside_window: int = Field(0, description="Events that did not overlap the requested window.")


class TimeAuditResult(BaseModel):
    """Result of the time_audit tool: where the user's time actually went."""
    time_min: str = Field(..., description="Inclusive start of the audited window, ISO 8601.")
    time_max: str = Field(..., description="Exclusive end of the audited window, ISO 8601.")
    timezone: str = Field(..., description="Timezone the days, weeks and working hours are expressed in.")
    group_by: str = Field("week", description="Period grouping used: 'day' or 'week'.")
    calendar_ids: List[str] = Field(default_factory=list, description="Calendars the events came from.")

    total_meeting_hours: float = Field(0.0, description="Total meeting hours in the window; overlaps counted twice.")
    total_meeting_count: int = Field(0, description="Meetings counted.")
    working_hours_available: float = Field(0.0, description="Working hours in the window, lunch removed.")
    meeting_hours_in_working_hours: float = Field(
        0.0, description="Meeting hours inside the working hours, overlaps merged."
    )
    share_of_working_hours: float = Field(
        0.0, description="Fraction of the working hours spent in meetings, 0..1."
    )

    periods: List[AuditPeriod] = Field(default_factory=list, description="Per-day or per-week breakdown, earliest first.")
    by_size: List[AuditBucket] = Field(
        default_factory=list,
        description="Meeting time by headcount: 'solo', '1:1', 'small' (<=4), 'large'.",
    )
    by_domain: List[AuditBucket] = Field(
        default_factory=list,
        description="Meeting time by attendee email domain. A meeting with two domains counts in both.",
    )
    by_recurrence: List[AuditBucket] = Field(
        default_factory=list, description="Meeting time split into 'recurring' and 'one-off'."
    )
    top_people: List[AuditPerson] = Field(
        default_factory=list, description="People the user shared the most meeting hours with, most first."
    )

    longest_meeting_day: Optional[AuditPeriod] = Field(
        None, description="The single heaviest day, regardless of group_by. None when nothing was booked."
    )
    busiest_period: Optional[AuditPeriod] = Field(
        None, description="The heaviest entry of 'periods'. None when nothing was booked."
    )

    back_to_back_count: int = Field(0, description="Runs of three or more meetings with no usable gap.")
    back_to_back_hours: float = Field(0.0, description="Total wall-clock hours those runs cover.")
    back_to_back_stretches: List[AuditStretch] = Field(
        default_factory=list, description="The individual back-to-back runs, earliest first."
    )

    focus_hours_available: float = Field(
        0.0, description="Unbooked working hours in blocks at least min_focus_block_minutes long."
    )
    focus_block_count: int = Field(0, description="Number of such blocks.")
    largest_focus_blocks: List[AuditFocusBlock] = Field(
        default_factory=list, description="The five longest free blocks, longest first."
    )

    excluded: AuditExclusions = Field(
        default_factory=AuditExclusions, description="What was skipped and why."
    )
    insights: List[str] = Field(
        default_factory=list, description="Three to five plain-English takeaways."
    )


# ---------------------------------------------------------------------------
# Scheduling: focus time, conflicts and reschedule suggestions
#
# Structured output for the tools in ``calendar_mcp.tools.focus`` and
# ``calendar_mcp.tools.conflicts``. The reasoning behind them lives in
# :mod:`calendar_mcp.scheduling`, which is pure; these models are only the
# shape it is reported in. Every timestamp is an ISO 8601 string, expressed in
# the user's preferred timezone (``preferences.timezone``) when they have set
# one, and otherwise in the calendar's own.
# ---------------------------------------------------------------------------


class FocusBlock(BaseModel):
    """One uninterrupted stretch of working time with nothing booked in it."""
    start: str = Field(..., description="Block start, ISO 8601.")
    end: str = Field(..., description="Block end, ISO 8601.")
    duration_minutes: float = Field(..., description="Length of the block in minutes.")
    date: str = Field(..., description="Local date the block starts on, YYYY-MM-DD.")
    weekday: str = Field(..., description="Local weekday the block starts on, e.g. 'Thursday'.")


class FocusTimeResult(BaseModel):
    """Result of the find_focus_time tool: when this person could actually think."""
    time_min: str = Field(..., description="Start of the searched window, ISO 8601.")
    time_max: str = Field(..., description="End of the searched window, ISO 8601.")
    timezone: str = Field(..., description="Timezone the blocks are expressed in.")
    calendar_ids: List[str] = Field(
        default_factory=list, description="Calendars whose busy time was subtracted."
    )
    hours_needed: float = Field(0.0, description="Focus hours the caller asked for.")
    total_free_hours: float = Field(0.0, description="Total focus hours available in the window.")
    satisfiable: bool = Field(
        False, description="True when total_free_hours covers hours_needed."
    )
    min_block_minutes: int = Field(
        0, description="Shortest stretch counted, from preferences.min_focus_block_minutes."
    )
    buffer_minutes: int = Field(
        0, description="Gap left around each meeting, from preferences.buffer_minutes."
    )
    count: int = Field(0, description="Number of blocks returned.")
    blocks: List[FocusBlock] = Field(
        default_factory=list, description="Usable blocks, longest first, then earliest."
    )
    message: str = Field("", description="One-line human-readable summary.")


class BlockedFocusEvent(BaseModel):
    """One focus block, as booked (or as it would be booked in a dry run)."""
    start: str = Field(..., description="Block start, ISO 8601.")
    end: str = Field(..., description="Block end, ISO 8601.")
    duration_minutes: float = Field(..., description="Length of the block in minutes.")
    summary: str = Field(..., description="Title the event was given.")
    created: bool = Field(False, description="False in a dry run: nothing was written.")
    event_id: Optional[str] = Field(None, description="ID of the created event; null in a dry run.")
    html_link: Optional[str] = Field(None, description="Link to the event in Google Calendar.")


class BlockedFocusResult(BaseModel):
    """Result of the block_focus_time tool."""
    calendar_id: str = Field(..., description="Calendar the blocks were booked on.")
    time_min: str = Field(..., description="Start of the searched window, ISO 8601.")
    time_max: str = Field(..., description="End of the searched window, ISO 8601.")
    timezone: str = Field(..., description="Timezone the blocks are expressed in.")
    dry_run: bool = Field(False, description="True when nothing was written to the calendar.")
    hours_needed: float = Field(0.0, description="Focus hours the caller asked for.")
    hours_booked: float = Field(0.0, description="Focus hours actually covered by these blocks.")
    total_free_hours: float = Field(
        0.0, description="Focus hours that were available in the window."
    )
    satisfied: bool = Field(False, description="True when hours_booked covers hours_needed.")
    count: int = Field(0, description="Number of blocks.")
    events: List[BlockedFocusEvent] = Field(
        default_factory=list, description="The blocks, earliest first."
    )
    message: str = Field("", description="One-line human-readable summary.")


class ConflictEventRef(BaseModel):
    """Enough of an event to recognise it in a conflict report and act on it."""
    account: str = Field(..., description="Account the event was read from.")
    calendar_id: str = Field(..., description="Calendar the event lives on.")
    event_id: Optional[str] = Field(None, description="Event ID; pass it to move_event or update_event.")
    summary: Optional[str] = Field(None, description="Event title.")
    start: str = Field(..., description="Event start, ISO 8601.")
    end: str = Field(..., description="Event end, ISO 8601.")
    all_day: bool = Field(False, description="True for an all-day event.")


class EventConflict(BaseModel):
    """Two events that are booked over each other."""
    overlap_minutes: float = Field(..., description="Minutes the two events share.")
    same_calendar: bool = Field(False, description="True when both events are on one calendar.")
    same_account: bool = Field(False, description="True when both events belong to one account.")
    first: ConflictEventRef = Field(..., description="The event that starts earlier.")
    second: ConflictEventRef = Field(..., description="The event that starts later.")


class TightTransition(BaseModel):
    """Two events that do not overlap but leave less than the preferred buffer."""
    gap_minutes: float = Field(..., description="Minutes between the first ending and the second starting.")
    buffer_minutes: int = Field(..., description="Gap the user's preferences ask for.")
    first: ConflictEventRef = Field(..., description="The event that ends first.")
    second: ConflictEventRef = Field(..., description="The event that starts next.")


class ConflictsResult(BaseModel):
    """Result of the detect_conflicts tool."""
    time_min: str = Field(..., description="Start of the checked window, ISO 8601.")
    time_max: str = Field(..., description="End of the checked window, ISO 8601.")
    timezone: str = Field(..., description="Timezone the times are expressed in.")
    accounts: List[str] = Field(default_factory=list, description="Accounts that were read.")
    calendar_ids: List[str] = Field(
        default_factory=list, description="Calendars that were read, as 'account:calendar_id'."
    )
    event_count: int = Field(0, description="Events considered after filtering.")
    buffer_minutes: int = Field(0, description="Buffer the tight transitions were judged against.")
    include_all_day: bool = Field(False, description="Whether all-day events were considered.")
    conflict_count: int = Field(0, description="Number of overlapping pairs.")
    conflicts: List[EventConflict] = Field(
        default_factory=list, description="Overlapping pairs, earliest first."
    )
    tight_count: int = Field(0, description="Number of too-tight transitions.")
    tight: List[TightTransition] = Field(
        default_factory=list, description="Back-to-back pairs that violate the buffer, earliest first."
    )
    skipped: List[str] = Field(
        default_factory=list,
        description="Calendars or accounts that could not be read in full, with the reason.",
    )
    message: str = Field("", description="One-line human-readable summary.")


class RescheduleSuggestion(BaseModel):
    """One proposed new time for an event, with why it scored as it did."""
    start: str = Field(..., description="Proposed start, ISO 8601.")
    end: str = Field(..., description="Proposed end, ISO 8601.")
    score: float = Field(..., description="Higher is better; 100 is a clean slot with no penalties.")
    attendee_conflicts: List[str] = Field(
        default_factory=list, description="Attendees who are busy then. Empty means everyone is free."
    )
    reasons: List[str] = Field(
        default_factory=list, description="Short phrases explaining the score."
    )


class RescheduleSuggestions(BaseModel):
    """Result of the suggest_reschedule tool. Read-only unless 'apply' was set."""
    calendar_id: str = Field(..., description="Calendar the event lives on.")
    event_id: str = Field(..., description="Event the suggestions are for.")
    summary: Optional[str] = Field(None, description="Event title.")
    current_start: Optional[str] = Field(None, description="Where the event sits now, ISO 8601.")
    current_end: Optional[str] = Field(None, description="Current end, ISO 8601.")
    duration_minutes: float = Field(0.0, description="Length of the event, preserved by every suggestion.")
    timezone: str = Field(..., description="Timezone the suggestions are expressed in.")
    attendees: List[str] = Field(
        default_factory=list, description="Attendee calendars whose availability was checked."
    )
    search_time_min: str = Field(..., description="Start of the searched window, ISO 8601.")
    search_time_max: str = Field(..., description="End of the searched window, ISO 8601.")
    count: int = Field(0, description="Number of suggestions returned.")
    suggestions: List[RescheduleSuggestion] = Field(
        default_factory=list, description="Proposed times, best first."
    )
    applied: bool = Field(False, description="True when the event was actually moved.")
    applied_event: Optional[EventInfo] = Field(
        None, description="The event after the move; null unless 'applied' is true."
    )
    message: str = Field("", description="One-line human-readable summary.")
