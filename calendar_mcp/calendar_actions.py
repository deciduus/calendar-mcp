import logging
from datetime import datetime, date, timedelta, time, timezone
from typing import Optional, List, Dict, Any, Tuple, TYPE_CHECKING
from dateutil import parser # For robust datetime parsing
import json

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from .models import (
    GoogleCalendarEvent,
    EventsResponse,
    EventCreateRequest,
    EventDateTime,
    EventAttendee,
    EventUpdateRequest,
    CalendarListResponse,
    CalendarListEntry
)

# ``analysis`` imports this module back, so it is imported lazily inside the two
# wrapper functions that need it rather than at module scope.
if TYPE_CHECKING:  # pragma: no cover - typing only
    from .analysis import ProjectedEventOccurrence

logger = logging.getLogger(__name__)

# --- Helper Function to Build Service ---

def _get_calendar_service(credentials: Credentials):
    """Builds the Google Calendar API service client."""
    try:
        service = build('calendar', 'v3', credentials=credentials)
        logger.debug("Google Calendar service client created successfully.")
        return service
    except Exception as e:
        logger.error(f"Failed to build Google Calendar service: {e}", exc_info=True)
        raise  # Re-raise the exception to be handled by the caller

# --- Calendar Action Functions ---

def find_events(
    credentials: Credentials,
    calendar_id: str = 'primary',
    time_min: Optional[datetime] = None,
    time_max: Optional[datetime] = None,
    query: Optional[str] = None,
    max_results: int = 50,
    single_events: bool = True, # Expand recurring events into instances
    order_by: str = 'startTime', # Order by start time
    iCalUID: Optional[str] = None, # Filter by iCalendar UID
    sharedExtendedProperty: Optional[str] = None, # Filter by shared extended properties (key=value or key)
    privateExtendedProperty: Optional[str] = None, # Filter by private extended properties (key=value or key)
    showDeleted: bool = False, # Show deleted events
    eventTypes: Optional[List[str]] = None # Filter by event types (e.g., ['default', 'focusTime'])
) -> Optional[EventsResponse]:
    """Finds events in a specified calendar based on various criteria.

    Args:
        credentials: Valid Google OAuth2 credentials.
        calendar_id: Calendar identifier (e.g., 'primary', email address, or calendar ID).
        time_min: Start of the time range (inclusive). If None, no lower bound.
        time_max: End of the time range (exclusive). If None, no upper bound.
        query: Free text search query.
        max_results: Maximum number of events to return.
        single_events: Whether to expand recurring events into single instances.
        order_by: The order of the events returned ('startTime' or 'updated').
        iCalUID: Specific iCalendar UID to filter by.
        sharedExtendedProperty: Filter by shared extended properties. Format "key=value" or "key".
        privateExtendedProperty: Filter by private extended properties. Format "key=value" or "key".
        showDeleted: Whether to include deleted events in the results.
        eventTypes: List of event types to return (e.g., ['default', 'focusTime', 'outOfOffice']).

    Returns:
        An EventsResponse object containing the list of events, or None if an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    # Format datetime objects to RFC3339 string format required by the API
    time_min_str = time_min.isoformat() + 'Z' if time_min and time_min.tzinfo is None else (time_min.isoformat() if time_min else None)
    time_max_str = time_max.isoformat() + 'Z' if time_max and time_max.tzinfo is None else (time_max.isoformat() if time_max else None)

    # Build the arguments dictionary dynamically to avoid passing None values for optional params
    list_kwargs = {
        'calendarId': calendar_id,
        'timeMin': time_min_str,
        'timeMax': time_max_str,
        'q': query,
        'maxResults': max_results,
        'singleEvents': single_events,
        'orderBy': order_by,
        'showDeleted': showDeleted,
        # Conditionally add parameters if they are provided
        **(({'iCalUID': iCalUID}) if iCalUID else {}),
        **(({'sharedExtendedProperty': sharedExtendedProperty}) if sharedExtendedProperty else {}),
        **(({'privateExtendedProperty': privateExtendedProperty}) if privateExtendedProperty else {}),
        **(({'eventTypes': eventTypes}) if eventTypes else {}),
    }
    # Filter out None values from list_kwargs to avoid API errors for empty optional params
    list_kwargs = {k: v for k, v in list_kwargs.items() if v is not None}

    logger.info(
        f"Fetching events from calendar '{calendar_id}' with parameters: {list_kwargs}"
        # f"time_min='{time_min_str}', time_max='{time_max_str}', query='{query}', "
        # f"max_results={max_results}, single_events={single_events}, order_by='{order_by}', "
        # f"iCalUID='{iCalUID}', sharedExtendedProperty='{sharedExtendedProperty}', "
        # f"privateExtendedProperty='{privateExtendedProperty}', showDeleted={showDeleted}, "
        # f"eventTypes={eventTypes}"
    )

    try:
        events_result = service.events().list(**list_kwargs).execute()

        logger.info(f"Found {len(events_result.get('items', []))} events.")

        # Parse the result using Pydantic models for validation and structure
        events_response = EventsResponse(**events_result)
        return events_response

    except HttpError as error:
        logger.error(f"An API error occurred while finding events: {error}", exc_info=True)
        # Add more detailed logging if possible
        try:
            error_content = error.content.decode('utf-8')
            logger.error(f"Google API error details (find_events): {error.resp.status} - {error_content}")
        except Exception:
            logger.error(f"Google API error details (find_events): {error.resp.status} - Could not decode error content.")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while finding events: {e}", exc_info=True)
        return None

def create_event(
    credentials: Credentials,
    event_data: EventCreateRequest, # Use the Pydantic model for input validation
    calendar_id: str = 'primary',
    send_notifications: bool = True # Whether to send notifications to attendees
) -> Optional[GoogleCalendarEvent]:
    """Creates a new event in the specified calendar.

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_data: An EventCreateRequest object containing event details.
        calendar_id: Calendar identifier.
        send_notifications: Whether to send notifications about the creation to attendees.

    Returns:
        A GoogleCalendarEvent object representing the created event, or None if an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    # --- Manually Construct Event Body --- 
    # Ensure datetime objects are formatted as strings for JSON serialization
    event_body: Dict[str, Any] = {}

    def format_datetime_field(dt_obj: datetime) -> str:
        if dt_obj.tzinfo is None:
            # Assume UTC if timezone is naive
            return dt_obj.isoformat() + 'Z'
        else:
            return dt_obj.isoformat()

    # Required fields
    if not event_data.start or not event_data.end:
        logger.error("Event creation failed: Start and End times are required.")
        return None # Or raise an error
    
    event_body['start'] = {}
    if event_data.start.dateTime:
        event_body['start']['dateTime'] = format_datetime_field(event_data.start.dateTime)
        if event_data.start.timeZone:
            event_body['start']['timeZone'] = event_data.start.timeZone
    elif event_data.start.date:
        event_body['start']['date'] = str(event_data.start.date) # Ensure date is string
    else:
        logger.error("Event creation failed: Start time requires either dateTime or date.")
        return None

    event_body['end'] = {}
    if event_data.end.dateTime:
        event_body['end']['dateTime'] = format_datetime_field(event_data.end.dateTime)
        if event_data.end.timeZone:
            event_body['end']['timeZone'] = event_data.end.timeZone
    elif event_data.end.date:
        event_body['end']['date'] = str(event_data.end.date) # Ensure date is string
    else:
        logger.error("Event creation failed: End time requires either dateTime or date.")
        return None
        
    # Optional fields
    if event_data.summary:
        event_body['summary'] = event_data.summary
    if event_data.description:
        event_body['description'] = event_data.description
    if event_data.location:
        event_body['location'] = event_data.location
    if event_data.attendees:
        event_body['attendees'] = [{'email': email} for email in event_data.attendees]
    if event_data.recurrence:
        event_body['recurrence'] = event_data.recurrence
    if event_data.reminders:
        # Pydantic should handle nested model serialization correctly here if reminders is a model
        # If it needs specific formatting, adjust here. Assuming .dict() is okay for reminders.
        event_body['reminders'] = event_data.reminders.dict(by_alias=True, exclude_unset=True)
    # Add other optional fields from EventCreateRequest if needed (e.g., colorId, transparency, etc.)

    # --- End Manual Construction ---

    logger.info(f"Creating event in calendar '{calendar_id}': {event_body.get('summary', '[No Summary]')}")
    # Use json.dumps for accurate debug logging of what will be serialized
    try:
        event_body_json = json.dumps(event_body, indent=2) # Pretty print for readability
        logger.debug(f"Event body for creation (JSON):\n{event_body_json}")
    except TypeError as json_err:
        # This should not happen now, but good to have a fallback log
        logger.error(f"Could not serialize event_body for debug log: {json_err}")
        logger.debug(f"Event body for creation (raw dict): {event_body}")

    try:
        created_event = service.events().insert(
            calendarId=calendar_id,
            body=event_body,
            sendNotifications=send_notifications
        ).execute()

        logger.info(f"Successfully created event with ID: {created_event.get('id')}")

        # Parse the created event using Pydantic model
        parsed_event = GoogleCalendarEvent(**created_event)
        return parsed_event

    except HttpError as error:
        # Log the actual error response content for debugging
        error_content = "Unknown error content"
        try:
            error_content = error.content.decode('utf-8')
        except Exception:
            pass # Keep default message if decoding fails
        logger.error(f"Google API error while creating event: {error.resp.status} - {error_content}", exc_info=True) 
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while creating event: {e}", exc_info=True)
        return None

def quick_add_event(
    credentials: Credentials,
    text: str,
    calendar_id: str = 'primary',
    send_notifications: bool = False # Default to False for quick add?
) -> Optional[GoogleCalendarEvent]:
    """Creates an event based on a simple text string using Google's parser.

    Args:
        credentials: Valid Google OAuth2 credentials.
        text: The text description of the event (e.g., "Tennis at 5pm tomorrow").
        calendar_id: Calendar identifier.
        send_notifications: Whether to send notifications.

    Returns:
        A GoogleCalendarEvent object representing the created event, or None if an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    logger.info(f"Quick adding event to calendar '{calendar_id}' with text: \"{text}\"")

    try:
        created_event = service.events().quickAdd(
            calendarId=calendar_id,
            text=text,
            sendNotifications=send_notifications
        ).execute()

        logger.info(f"Successfully quick-added event with ID: {created_event.get('id')}")

        # Parse the created event using Pydantic model
        parsed_event = GoogleCalendarEvent(**created_event)
        return parsed_event

    except HttpError as error:
        logger.error(f"An API error occurred during quick add: {error}", exc_info=True)
        # Add more detailed logging
        try:
            error_content = error.content.decode('utf-8')
            logger.error(f"Google API error details (quick_add): {error.resp.status} - {error_content}")
        except Exception:
            logger.error(f"Google API error details (quick_add): {error.resp.status} - Could not decode error content.")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred during quick add: {e}", exc_info=True)
        return None

def update_event(
    credentials: Credentials,
    event_id: str,
    update_data: EventUpdateRequest, # Use Pydantic model for partial update data
    calendar_id: str = 'primary',
    send_notifications: bool = True # Whether to send notifications
) -> Optional[GoogleCalendarEvent]:
    """Updates an existing event using patch semantics (only specified fields are changed).

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_id: The ID of the event to update.
        update_data: An EventUpdateRequest object containing fields to update.
        calendar_id: Calendar identifier.
        send_notifications: Whether to send update notifications to attendees.

    Returns:
        A GoogleCalendarEvent object representing the updated event, or None if an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    # Manually construct the update body dictionary
    update_body: Dict[str, Any] = {}

    # Helper function (can be reused or defined locally)
    def format_datetime_field(dt_obj: datetime) -> str:
        if dt_obj.tzinfo is None:
            return dt_obj.isoformat() + 'Z'
        else:
            return dt_obj.isoformat()

    # Populate update_body only with fields present in update_data
    if update_data.summary is not None:
        update_body['summary'] = update_data.summary
    if update_data.description is not None:
        update_body['description'] = update_data.description
    if update_data.location is not None:
        update_body['location'] = update_data.location

    # Handle start time - need to format if present
    if update_data.start is not None:
        start_details = {}
        if update_data.start.dateTime:
            start_details['dateTime'] = format_datetime_field(update_data.start.dateTime)
            if update_data.start.timeZone:
                start_details['timeZone'] = update_data.start.timeZone
        elif update_data.start.date:
            start_details['date'] = str(update_data.start.date)
        # Add check if neither date nor dateTime provided in start? Unlikely via model validation.
        if start_details: # Only add 'start' if we have valid sub-fields
            update_body['start'] = start_details

    # Handle end time - need to format if present
    if update_data.end is not None:
        end_details = {}
        if update_data.end.dateTime:
            end_details['dateTime'] = format_datetime_field(update_data.end.dateTime)
            if update_data.end.timeZone:
                end_details['timeZone'] = update_data.end.timeZone
        elif update_data.end.date:
            end_details['date'] = str(update_data.end.date)
        # Add check if neither date nor dateTime provided in end?
        if end_details: # Only add 'end' if we have valid sub-fields
            update_body['end'] = end_details
            
    # Handle attendees - PATCH replaces the attendee list
    if update_data.attendees is not None:
        # Convert EventAttendee models back to simple dicts for API
        update_body['attendees'] = [
            attendee.dict(by_alias=True, exclude_unset=True) 
            for attendee in update_data.attendees
        ]
    # Add other updatable fields from EventUpdateRequest if needed

    if not update_body:
        logger.warning(f"Update called for event {event_id} with no fields to update.")
        # Optionally, retrieve and return the existing event data?
        # For now, return None or raise an error, as no update was performed.
        # Let's retrieve the existing event to provide some feedback.
        try:
            existing_event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
            return GoogleCalendarEvent(**existing_event)
        except HttpError as e:
            logger.error(f"Failed to retrieve event {event_id} after empty update request: {e}")
            raise

    logger.info(f"Updating event '{event_id}' in calendar '{calendar_id}'.")
    logger.debug(f"Update body for patch: {update_body}")

    try:
        updated_event = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=update_body,
            sendNotifications=send_notifications
        ).execute()

        logger.info(f"Successfully updated event '{event_id}'.")

        # Parse the updated event using Pydantic model
        parsed_event = GoogleCalendarEvent(**updated_event)
        return parsed_event

    except HttpError as error:
        # Handle common errors like 404 Not Found
        if error.resp.status == 404:
            logger.error(f"Event '{event_id}' not found in calendar '{calendar_id}'.")
        else:
            # Log detailed error content
            error_content = "Unknown error content"
            try:
                error_content = error.content.decode('utf-8')
            except Exception:
                pass
            logger.error(f"Google API error while updating event '{event_id}': {error.resp.status} - {error_content}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while updating event '{event_id}': {e}", exc_info=True)
        return None

def delete_event(
    credentials: Credentials,
    event_id: str,
    calendar_id: str = 'primary',
    send_notifications: bool = True # Whether to send notifications
) -> bool:
    """Deletes an event.

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_id: The ID of the event to delete.
        calendar_id: Calendar identifier.
        send_notifications: Whether to send deletion notifications to attendees.

    Returns:
        True if the event was deleted successfully, False otherwise.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return False

    logger.info(f"Attempting to delete event '{event_id}' from calendar '{calendar_id}'.")

    try:
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendNotifications=send_notifications
        ).execute()
        # Delete returns no content on success (204)
        logger.info(f"Successfully deleted event '{event_id}'.")
        return True

    except HttpError as error:
        # Handle common errors like 404 Not Found or 410 Gone
        if error.resp.status in [404, 410]:
            logger.error(f"Event '{event_id}' not found or already deleted in calendar '{calendar_id}'. Cannot delete.")
        else:
            # Log detailed error content
            error_content = "Unknown error content"
            try:
                error_content = error.content.decode('utf-8')
            except Exception:
                pass
            logger.error(f"Google API error while deleting event '{event_id}': {error.resp.status} - {error_content}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while deleting event '{event_id}': {e}", exc_info=True)
        return False

def add_attendee(
    credentials: Credentials,
    event_id: str,
    attendee_emails: List[str], # Simple list of emails to add
    calendar_id: str = 'primary',
    send_notifications: bool = True
) -> Optional[GoogleCalendarEvent]:
    """Adds one or more attendees to an existing event.

    Note: This replaces the entire attendee list in the event.

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_id: The ID of the event to modify.
        attendee_emails: A list of email addresses to add/set as attendees.
        calendar_id: Calendar identifier.
        send_notifications: Whether to send update notifications.

    Returns:
        The updated GoogleCalendarEvent object, or None if an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    logger.info(f"Attempting to add attendees {attendee_emails} to event '{event_id}' in calendar '{calendar_id}'.")

    # 1. Get the existing event
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        logger.debug(f"Retrieved existing event '{event_id}' for adding attendees.")
    except HttpError as error:
        if error.resp.status == 404:
            logger.error(f"Event '{event_id}' not found in calendar '{calendar_id}'. Cannot add attendees.")
        else:
            # Log detailed error content when retrieving event
            error_content = "Unknown error content"
            try:
                error_content = error.content.decode('utf-8')
            except Exception:
                pass
            logger.error(f"Google API error retrieving event '{event_id}' for adding attendees: {error.resp.status} - {error_content}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error retrieving event '{event_id}': {e}", exc_info=True)
        return None

    # 2. Modify the attendee list
    # Get current attendees, ensuring it's a list
    current_attendees = event.get('attendees', [])
    if not isinstance(current_attendees, list):
        current_attendees = [] # Ensure it's a list if API returns something unexpected

    # Create a set of current attendee emails for efficient lookup
    current_emails = {attendee.get('email') for attendee in current_attendees if attendee.get('email')}

    # Prepare the list of new attendee objects to add
    new_attendees_to_add = [
        {'email': email} for email in attendee_emails if email not in current_emails
    ]

    if not new_attendees_to_add:
        logger.warning(f"All provided attendees {attendee_emails} are already in event '{event_id}'. No update needed.")
        # Return the current event data as no changes were made
        return GoogleCalendarEvent(**event)

    # Combine current and new attendees
    updated_attendee_list = current_attendees + new_attendees_to_add

    # 3. Prepare the patch body
    patch_body = {
        'attendees': updated_attendee_list
    }

    # 4. Patch the event
    logger.debug(f"Patching event '{event_id}' with updated attendees: {patch_body}")
    try:
        updated_event = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=patch_body,
            sendNotifications=send_notifications
        ).execute()

        logger.info(f"Successfully added attendees to event '{event_id}'.")

        # Parse the updated event using Pydantic model
        parsed_event = GoogleCalendarEvent(**updated_event)
        return parsed_event

    except HttpError as error:
        # Log detailed error content when patching event
        error_content = "Unknown error content"
        try:
            error_content = error.content.decode('utf-8')
        except Exception:
            pass
        logger.error(f"Google API error occurred while patching event '{event_id}' with new attendees: {error.resp.status} - {error_content}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while patching event '{event_id}' with new attendees: {e}", exc_info=True)
        return None

def find_calendars(
    credentials: Credentials,
    min_access_role: Optional[str] = None # e.g., 'reader', 'writer', 'owner'
) -> Optional[CalendarListResponse]:
    """Lists the calendars on the user's calendar list.

    Args:
        credentials: Valid Google OAuth2 credentials.
        min_access_role: The minimum access role for the user in the returned calendars.

    Returns:
        A CalendarListResponse object containing the list of calendars, or None if an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    logger.info(f"Fetching calendar list. Min access role: {min_access_role}")

    # Paginate? For simplicity, get first page for now.
    # Add maxResults if needed.
    try:
        calendar_list = service.calendarList().list(
            minAccessRole=min_access_role
        ).execute()

        logger.info(f"Found {len(calendar_list.get('items', []))} calendars in the list.")

        # Parse the result using Pydantic model
        parsed_list = CalendarListResponse(**calendar_list)
        return parsed_list

    except HttpError as error:
        logger.error(f"An API error occurred while fetching calendar list: {error}", exc_info=True)
        # Add more detailed logging if possible
        try:
            error_content = error.content.decode('utf-8')
            logger.error(f"Google API error details (find_calendars): {error.resp.status} - {error_content}")
        except Exception:
            logger.error(f"Google API error details (find_calendars): {error.resp.status} - Could not decode error content.")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching calendar list: {e}", exc_info=True)
        return None

def create_calendar(
    credentials: Credentials,
    summary: str # The title of the new calendar
) -> Optional[CalendarListEntry]: # Returns the created calendar (similar to CalendarListEntry)
    """Creates a new secondary calendar.

    Args:
        credentials: Valid Google OAuth2 credentials.
        summary: The title for the new calendar.

    Returns:
        A CalendarListEntry object representing the created calendar, or None if an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    logger.info(f"Attempting to create a new calendar with summary: '{summary}'")

    calendar_body = {
        'summary': summary
        # Add other fields like description or timeZone if needed
    }

    try:
        created_calendar = service.calendars().insert(body=calendar_body).execute()
        logger.info(f"Successfully created calendar with ID: {created_calendar.get('id')}")

        # The response is a Calendar resource, parse it using CalendarListEntry model
        # (Structure is identical for relevant fields)
        parsed_calendar = CalendarListEntry(**created_calendar)
        return parsed_calendar

    except HttpError as error:
        logger.error(f"An API error occurred while creating calendar '{summary}': {error}", exc_info=True)
        # Log detailed error content
        error_content = "Unknown error content"
        try:
            error_content = error.content.decode('utf-8')
        except Exception:
            pass
        logger.error(f"Google API error occurred while creating calendar '{summary}': {error.resp.status} - {error_content}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while creating calendar '{summary}': {e}", exc_info=True)
        return None

def check_attendee_status(
    credentials: Credentials,
    event_id: str,
    calendar_id: str = 'primary',
    attendee_emails: Optional[List[str]] = None
) -> Optional[Dict[str, str]]:
    """Checks the response status of attendees for a specific event.

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_id: The ID of the event to check.
        calendar_id: Calendar identifier.
        attendee_emails: Optional list of specific attendee emails to check.
                         If None, checks status for all attendees.

    Returns:
        A dictionary mapping attendee emails to their response status
        (e.g., 'accepted', 'declined', 'tentative', 'needsAction'),
        or None if the event is not found or an error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    logger.info(f"Checking attendee status for event '{event_id}' in calendar '{calendar_id}'. Target emails: {attendee_emails or 'All'}")

    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        logger.debug(f"Retrieved event '{event_id}' for status check.")

    except HttpError as error:
        if error.resp.status == 404:
            logger.error(f"Event '{event_id}' not found in calendar '{calendar_id}'. Cannot check status.")
        else:
            # Log detailed error content
            error_content = "Unknown error content"
            try:
                error_content = error.content.decode('utf-8')
            except Exception:
                pass
            logger.error(f"Google API error retrieving event '{event_id}' for status check: {error.resp.status} - {error_content}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error retrieving event '{event_id}': {e}", exc_info=True)
        return None

    attendees = event.get('attendees', [])
    if not attendees:
        logger.info(f"Event '{event_id}' has no attendees.")
        return {}

    status_map: Dict[str, str] = {}
    target_emails_set = set(attendee_emails) if attendee_emails is not None else None

    for attendee in attendees:
        email = attendee.get('email')
        status = attendee.get('responseStatus')
        if not email or not status:
            continue # Skip attendees without email or status

        # If specific emails were requested, check if this attendee is one of them
        if target_emails_set is not None:
            if email in target_emails_set:
                status_map[email] = status
        else:
            # Otherwise, include all attendees
            status_map[email] = status

    logger.info(f"Attendee statuses retrieved for event '{event_id}': {len(status_map)} attendees found.")
    return status_map

def find_availability(
    credentials: Credentials,
    time_min: datetime,
    time_max: datetime,
    calendar_ids: List[str]
) -> Optional[Dict[str, Dict[str, Any]]]: # Return Dict mapping calendar_id to {'busy': List[Dict], 'errors': List[Dict]} ?
    """Finds free/busy information for a list of calendars.

    Args:
        credentials: Valid Google OAuth2 credentials.
        time_min: Start of the time range (inclusive, timezone-aware recommended).
        time_max: End of the time range (exclusive, timezone-aware recommended).
        calendar_ids: A list of calendar identifiers (email or ID) to query.

    Returns:
        A dictionary mapping each calendar ID to its free/busy information.
        The value for each calendar ID is a dictionary containing:
        - 'busy': A list of busy time intervals [{'start': datetime, 'end': datetime}].
        - 'errors': A list of errors encountered for that specific calendar (from API).
        Returns None if a major API error occurs.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    if not calendar_ids:
        logger.warning("find_availability called with empty calendar_ids list.")
        return {}

    # Ensure time_min and time_max are in RFC3339 format
    # Add 'Z' for UTC if timezone is naive, otherwise format appropriately
    time_min_str = time_min.isoformat() + ('Z' if time_min.tzinfo is None else '')
    time_max_str = time_max.isoformat() + ('Z' if time_max.tzinfo is None else '')

    request_body = {
        "timeMin": time_min_str,
        "timeMax": time_max_str,
        "items": [{"id": cal_id} for cal_id in calendar_ids]
        # Optional: Add groupExpansionMax, calendarExpansionMax if needed
    }

    logger.info(f"Querying free/busy information for calendars: {calendar_ids} between {time_min_str} and {time_max_str}")
    logger.debug(f"Free/busy request body: {request_body}")

    try:
        freebusy_result = service.freebusy().query(body=request_body).execute()
        logger.debug(f"Free/busy raw response: {freebusy_result}")

        # Process the response into a more usable format
        processed_results: Dict[str, Dict[str, Any]] = {}
        calendars_data = freebusy_result.get('calendars', {})

        for cal_id, data in calendars_data.items():
            busy_intervals = []
            for interval in data.get('busy', []):
                try:
                    # Parse RFC3339 strings back to datetime objects
                    start_dt = parser.isoparse(interval.get('start'))
                    end_dt = parser.isoparse(interval.get('end'))
                    busy_intervals.append({'start': start_dt, 'end': end_dt})
                except (TypeError, ValueError) as parse_error:
                    logger.warning(f"Could not parse busy interval for {cal_id}: {interval}. Error: {parse_error}")
                    # Optionally add this interval with raw strings or skip it

            processed_results[cal_id] = {
                'busy': busy_intervals,
                'errors': data.get('errors', []) # Keep API errors as is
            }

        logger.info(f"Successfully retrieved free/busy information for {len(processed_results)} calendars.")
        return processed_results

    except HttpError as error:
        logger.error(f"An API error occurred during free/busy query: {error}", exc_info=True)
        # Log detailed error content
        error_content = "Unknown error content"
        try:
            error_content = error.content.decode('utf-8')
        except Exception:
            pass
        logger.error(f"Google API error occurred during free/busy query: {error.resp.status} - {error_content}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred during free/busy query: {e}", exc_info=True)
        return None

def _merge_intervals(intervals: List[Dict[str, datetime]]) -> List[Dict[str, datetime]]:
    """Merges overlapping or adjacent time intervals."""
    if not intervals:
        return []

    # Sort intervals by start time
    sorted_intervals = sorted(intervals, key=lambda x: x['start'])

    merged = [sorted_intervals[0]]

    for current in sorted_intervals[1:]:
        last_merged = merged[-1]
        # If current interval overlaps or is adjacent to the last merged one
        if current['start'] <= last_merged['end']:
            # Merge by extending the end time if current ends later
            merged[-1]['end'] = max(last_merged['end'], current['end'])
        else:
            # No overlap, add the current interval as a new one
            merged.append(current)

    return merged

def _find_first_available_slot(
    time_min: datetime,
    time_max: datetime,
    duration: timedelta,
    busy_intervals: List[Dict[str, datetime]],
    working_hours_start: Optional[time] = None,
    working_hours_end: Optional[time] = None,
) -> Optional[Tuple[datetime, datetime]]:
    """Finds the first available time slot of a given duration within a range, considering busy times and working hours.
       Ensures the search starts from the current time if time_min is in the past.
    """
    logger.info("--- Entering _find_first_available_slot ---")
    logger.debug(f"Initial inputs: time_min={time_min}, time_max={time_max}, duration={duration}")

    # --- Ensure Timezones (use UTC for consistency) ---
    try:
        logger.debug(f"Original time_min tz: {time_min.tzinfo}, time_max tz: {time_max.tzinfo}")
        time_min_utc = time_min.astimezone(timezone.utc) if time_min.tzinfo else time_min.replace(tzinfo=timezone.utc)
        time_max_utc = time_max.astimezone(timezone.utc) if time_max.tzinfo else time_max.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        logger.debug(f"Normalized to UTC: time_min={time_min_utc}, time_max={time_max_utc}, now={now_utc}")
    except Exception as tz_err:
        logger.error(f"Error normalizing timezones to UTC: {tz_err}")
        # Fallback, though less ideal
        time_min_utc = time_min
        time_max_utc = time_max
        now_utc = datetime.now()
        # Ensure comparison is possible if fallback occurred
        if time_min_utc.tzinfo is None and now_utc.tzinfo is not None:
            now_utc = now_utc.replace(tzinfo=None)
        elif time_min_utc.tzinfo is not None and now_utc.tzinfo is None:
             logger.warning("Cannot reliably compare naive now() with timezone-aware time_min in fallback.")
             # Consider returning None or raising error here

    # Determine effective start time (MUST be in the future)
    effective_start = max(time_min_utc, now_utc)
    # Log at INFO level for visibility
    logger.info(f"Search range: {time_min_utc} to {time_max_utc}. Current time: {now_utc}. Effective start for search: {effective_start}")

    # Adjust merged busy intervals to be UTC as well for correct comparison
    busy_intervals_utc = []
    for interval in busy_intervals:
        try:
            start_utc = interval['start'].astimezone(timezone.utc) if interval['start'].tzinfo else interval['start'].replace(tzinfo=timezone.utc)
            end_utc = interval['end'].astimezone(timezone.utc) if interval['end'].tzinfo else interval['end'].replace(tzinfo=timezone.utc)
            busy_intervals_utc.append({'start': start_utc, 'end': end_utc})
        except Exception as busy_tz_err:
             logger.warning(f"Could not normalize busy interval {interval} to UTC: {busy_tz_err}")
             # Skip this interval or handle error appropriately

    # Sort busy intervals (important for gap logic)
    busy_intervals_utc.sort(key=lambda x: x['start'])

    # --- Refactored Slot Finding Logic ---
    current_search_time = effective_start
    logger.info(f"Search pointer initialized to: {current_search_time}")

    # --- Working Hours Check (Placeholder - implement timezone logic if needed) ---
    def is_within_working_hours(slot_start: datetime, slot_end: datetime) -> bool:
        if not working_hours_start or not working_hours_end:
            return True # No working hours constraint
        # WARNING: This naive comparison assumes slot times are in local time
        # matching working_hours_start/end. Proper implementation requires
        # converting slot_start/end back to the relevant local timezone first.
        # For now, proceeding with naive check or ignoring if WH not set.
        try:
            # Attempt comparison assuming compatible types/timezones
             return (working_hours_start <= slot_start.time() and
                     slot_end.time() <= working_hours_end and
                     slot_start.date() == slot_end.date())
        except TypeError as te:
            logger.warning(f"Could not compare working hours due to timezone mismatch or type error: {te}. Ignoring working hours constraint for this slot.")
            return True # Default to True if comparison fails
    # --- End Working Hours Check ---

    while current_search_time < time_max_utc:
        potential_end_time = current_search_time + duration

        # Check if slot goes beyond the overall search window
        if potential_end_time > time_max_utc:
            logger.info(f"Potential slot end {potential_end_time} exceeds time_max {time_max_utc}. Search finished.")
            break # Stop searching

        # Check for overlap with any busy interval
        overlap_found = False
        for busy in busy_intervals_utc:
            # Check if the potential slot overlaps with this busy interval
            # Overlap definition: (SlotStart < BusyEnd) and (SlotEnd > BusyStart)
            if current_search_time < busy['end'] and potential_end_time > busy['start']:
                # Overlap found. Move search time to the end of this busy interval.
                logger.debug(f"Potential slot {current_search_time} - {potential_end_time} overlaps with busy {busy['start']} - {busy['end']}. Jumping search time.")
                current_search_time = busy['end']
                overlap_found = True
                break # Break inner loop, restart outer while loop check
        
        if overlap_found:
            continue # Restart the while loop with the adjusted current_search_time

        # If we reach here, the slot [current_search_time, potential_end_time] is free
        # Check working hours
        # TODO: Implement proper timezone conversion for working hours check if needed
        if is_within_working_hours(current_search_time, potential_end_time):
            logger.info(f"Found available slot: {current_search_time} - {potential_end_time}")
            return current_search_time, potential_end_time
        else:
            # Slot is free but outside working hours. Move search time forward.
            # A simple jump might be to the start of the next working hour block, 
            # but for now, just advance slightly to check the next possible interval.
            # Advancing by duration ensures we check the next non-overlapping slot.
            logger.debug(f"Slot {current_search_time} - {potential_end_time} rejected due to working hours. Advancing search time.")
            current_search_time += timedelta(minutes=15) # Or duration? Let's try 15 min increment.
            
    # Looped through entire window without finding a suitable slot
    logger.info("No suitable available slot found within the time window.")
    return None

def find_mutual_availability_and_schedule(
    credentials: Credentials,
    attendee_calendar_ids: List[str],
    time_min: datetime,
    time_max: datetime,
    duration_minutes: int,
    event_details: EventCreateRequest, # Pre-filled event details (summary, desc, etc.) excluding times
    organizer_calendar_id: str = 'primary',
    working_hours_start: Optional[time] = None, # e.g., time(9, 0)
    working_hours_end: Optional[time] = None,   # e.g., time(17, 0)
    send_notifications: bool = True
) -> Optional[GoogleCalendarEvent]:
    """Finds the first mutually available time slot for attendees and schedules an event.

    Args:
        credentials: Valid Google OAuth2 credentials.
        attendee_calendar_ids: List of calendar IDs (emails) for attendees.
        time_min: Start of the search window (timezone-aware recommended).
        time_max: End of the search window (timezone-aware recommended).
        duration_minutes: Required duration of the event in minutes.
        event_details: Pydantic model instance with event details (summary, description,
                       location, etc.) but WITHOUT start/end times. Attendees can be pre-filled
                       or will be added from attendee_calendar_ids.
        organizer_calendar_id: Calendar ID where the event will be created.
        working_hours_start: Optional start time for daily working hours constraint.
        working_hours_end: Optional end time for daily working hours constraint.
        send_notifications: Whether to send notifications for the created event.

    Returns:
        The created GoogleCalendarEvent object if a slot is found and scheduling succeeds,
        otherwise None.
    """
    service = _get_calendar_service(credentials)
    if not service:
        return None

    logger.info(f"Attempting to find mutual availability and schedule for: {attendee_calendar_ids}")
    logger.info(f"Search window: {time_min} to {time_max}, Duration: {duration_minutes} mins")

    # 1. Find availability for all attendees
    availability_data = find_availability(
        credentials=credentials,
        time_min=time_min,
        time_max=time_max,
        calendar_ids=attendee_calendar_ids
    )

    if availability_data is None:
        logger.error("Failed to retrieve availability data.")
        return None

    # 2. Aggregate and merge all busy intervals
    all_busy_intervals: List[Dict[str, datetime]] = []
    for cal_id, data in availability_data.items():
        if data.get('errors'):
            logger.warning(f"Encountered errors fetching availability for {cal_id}: {data['errors']}")
            # Decide how to handle errors: fail, proceed without this calendar, etc.
            # For now, let's log a warning and proceed, potentially scheduling over their busy time.
            # A stricter approach would be to return None here.
        all_busy_intervals.extend(data.get('busy', []))

    merged_busy = _merge_intervals(all_busy_intervals)
    logger.debug(f"Merged busy intervals: {merged_busy}")

    # 3. Find the first available slot
    duration = timedelta(minutes=duration_minutes)
    available_slot = _find_first_available_slot(
        time_min=time_min,
        time_max=time_max,
        duration=duration,
        busy_intervals=merged_busy,
        working_hours_start=working_hours_start,
        working_hours_end=working_hours_end
    )

    if not available_slot:
        logger.warning("No mutually available time slot found meeting the criteria.")
        return None

    slot_start, slot_end = available_slot
    logger.info(f"Found available slot: {slot_start} - {slot_end}")

    # 4. Prepare full event data
    # Create a copy to avoid modifying the original input
    final_event_data = event_details.copy(deep=True)

    final_event_data.start = EventDateTime(dateTime=slot_start)
    final_event_data.end = EventDateTime(dateTime=slot_end)

    # Ensure all required attendees are in the event data
    existing_attendees = {att.email for att in final_event_data.attendees} if final_event_data.attendees else set()
    for email in attendee_calendar_ids:
        # Skip adding 'primary' as an attendee email
        if email == 'primary': 
            continue
            
        if email not in existing_attendees:
            if final_event_data.attendees is None:
                final_event_data.attendees = []
            # Assuming EventCreateRequest uses a simple list of emails for input
            # If it expects EventAttendee models, adjust accordingly.
            # Based on create_event, it expects a list of emails which it converts.
            final_event_data.attendees.append(email)
            existing_attendees.add(email) # Keep track

    logger.debug(f"Final event data for creation: {final_event_data.dict(by_alias=True)}")

    # 5. Create the event
    created_event = create_event(
        credentials=credentials,
        event_data=final_event_data,
        calendar_id=organizer_calendar_id,
        send_notifications=send_notifications
    )

    if created_event:
        logger.info(f"Successfully scheduled event '{created_event.summary}' (ID: {created_event.id}) at {slot_start}")
    else:
        logger.error("Failed to create the event after finding an available slot.")

    return created_event

# --- Analysis Wrappers ---

def get_projected_recurring_events(
    credentials: Credentials,
    time_min: datetime,
    time_max: datetime,
    calendar_id: str = 'primary',
    event_query: Optional[str] = None
) -> List["ProjectedEventOccurrence"]:
    """Wrapper function to find recurring events and project their occurrences.

    This calls the core logic in the analysis module.

    Args:
        credentials: Valid Google OAuth2 credentials.
        time_min: Start of the projection window (timezone-aware recommended).
        time_max: End of the projection window (timezone-aware recommended).
        calendar_id: The calendar to search within.
        event_query: Optional text query to filter master recurring events (e.g., "Birthday").

    Returns:
        A list of ProjectedEventOccurrence objects representing calculated occurrences.
    """
    from .analysis import project_recurring_events  # local: analysis imports this module

    logger.info(f"Action: get_projected_recurring_events called for calendar '{calendar_id}'")
    return project_recurring_events(
        credentials=credentials,
        time_min=time_min,
        time_max=time_max,
        calendar_id=calendar_id,
        event_query=event_query
    )

def get_busyness_analysis(
    credentials: Credentials,
    time_min: datetime,
    time_max: datetime,
    calendar_id: str = 'primary',
) -> Optional[Dict[date, Dict[str, Any]]]:
    """Wrapper function to analyze daily event busyness.

    This calls the core logic in the analysis module.

    Args:
        credentials: Valid Google OAuth2 credentials.
        time_min: Start of the analysis window (timezone-aware recommended).
        time_max: End of the analysis window (timezone-aware recommended).
        calendar_id: The calendar to analyze.

    Returns:
        A dictionary mapping each date to its busyness stats, or None on error.
    """
    from .analysis import analyze_busyness  # local: analysis imports this module

    logger.info(f"Action: get_busyness_analysis called for calendar '{calendar_id}'")
    try:
        return analyze_busyness(
            credentials=credentials,
            time_min=time_min,
            time_max=time_max,
            calendar_id=calendar_id,
        )
    except Exception as e:
        # Log the specific error from the analysis function
        logger.error(f"Error during busyness analysis execution: {e}", exc_info=True)
        return None # Return None to signal error to the server endpoint

# --- Add other action functions below (create_calendar) --- 

# --- Event mutation helpers added for the v1 MCP server ---

def get_event(
    credentials: Credentials,
    event_id: str,
    calendar_id: str = 'primary',
) -> Optional[GoogleCalendarEvent]:
    """Fetches a single event by ID.

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_id: The ID of the event to fetch.
        calendar_id: Calendar identifier.

    Returns:
        A GoogleCalendarEvent, or None if the response could not be parsed.

    Raises:
        HttpError: If the Google Calendar API rejects the request.
    """
    service = _get_calendar_service(credentials)
    try:
        raw = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return GoogleCalendarEvent(**raw)
    except HttpError as error:
        logger.error(
            f"Google API error retrieving event '{event_id}' from '{calendar_id}': "
            f"{getattr(error.resp, 'status', '?')}",
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(f"Unexpected error retrieving event '{event_id}': {e}", exc_info=True)
        return None


def _event_duration(raw_event: Dict[str, Any]) -> Optional[timedelta]:
    """Returns the wall-clock duration of a raw API event, if both ends are timed."""
    start = (raw_event.get('start') or {}).get('dateTime')
    end = (raw_event.get('end') or {}).get('dateTime')
    if not start or not end:
        return None
    try:
        return parser.isoparse(end) - parser.isoparse(start)
    except (ValueError, TypeError):
        return None


def move_event(
    credentials: Credentials,
    event_id: str,
    calendar_id: str = 'primary',
    new_start: Optional[datetime] = None,
    new_end: Optional[datetime] = None,
    destination_calendar_id: Optional[str] = None,
    send_notifications: bool = True,
) -> Optional[GoogleCalendarEvent]:
    """Reschedules an event and/or moves it to a different calendar.

    If only ``new_start`` is supplied the original duration is preserved.
    If ``destination_calendar_id`` is supplied the event is first moved with
    ``events().move()`` and then re-timed (when new times were given).

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_id: The ID of the event to move.
        calendar_id: Calendar the event currently lives on.
        new_start: New start time (timezone-aware recommended).
        new_end: New end time. Optional when ``new_start`` is given.
        destination_calendar_id: Calendar to move the event to.
        send_notifications: Whether to notify attendees.

    Returns:
        The updated GoogleCalendarEvent, or None if the response could not be parsed.

    Raises:
        HttpError: If the Google Calendar API rejects the request.
        ValueError: If neither a new time nor a destination calendar is supplied.
    """
    if new_start is None and new_end is None and not destination_calendar_id:
        raise ValueError(
            "move_event requires at least one of new_start, new_end or destination_calendar_id."
        )

    service = _get_calendar_service(credentials)

    try:
        existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        source_calendar_id = calendar_id
        if destination_calendar_id and destination_calendar_id != calendar_id:
            logger.info(
                f"Moving event '{event_id}' from '{calendar_id}' to '{destination_calendar_id}'."
            )
            existing = service.events().move(
                calendarId=calendar_id,
                eventId=event_id,
                destination=destination_calendar_id,
                sendNotifications=send_notifications,
            ).execute()
            source_calendar_id = destination_calendar_id
            event_id = existing.get('id', event_id)

        if new_start is None and new_end is None:
            return GoogleCalendarEvent(**existing)

        # Preserve the original timeZone hints where the caller did not override them.
        start_tz = (existing.get('start') or {}).get('timeZone')
        end_tz = (existing.get('end') or {}).get('timeZone')

        resolved_start = new_start
        resolved_end = new_end
        if resolved_start is not None and resolved_end is None:
            duration = _event_duration(existing)
            if duration is not None:
                resolved_end = resolved_start + duration
            else:
                logger.warning(
                    f"Event '{event_id}' has no timed duration to preserve; "
                    "leaving the end time untouched."
                )

        body: Dict[str, Any] = {}
        if resolved_start is not None:
            body['start'] = {'dateTime': resolved_start.isoformat()}
            if start_tz:
                body['start']['timeZone'] = start_tz
        if resolved_end is not None:
            body['end'] = {'dateTime': resolved_end.isoformat()}
            if end_tz:
                body['end']['timeZone'] = end_tz

        logger.info(f"Re-timing event '{event_id}' on calendar '{source_calendar_id}'.")
        updated = service.events().patch(
            calendarId=source_calendar_id,
            eventId=event_id,
            body=body,
            sendNotifications=send_notifications,
        ).execute()
        return GoogleCalendarEvent(**updated)

    except HttpError as error:
        logger.error(
            f"Google API error while moving event '{event_id}': "
            f"{getattr(error.resp, 'status', '?')}",
            exc_info=True,
        )
        raise
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error while moving event '{event_id}': {e}", exc_info=True)
        return None


RESPONSE_STATUSES = ('accepted', 'declined', 'tentative', 'needsAction')


def respond_to_event(
    credentials: Credentials,
    event_id: str,
    response_status: str,
    calendar_id: str = 'primary',
    comment: Optional[str] = None,
    send_notifications: bool = True,
) -> Optional[GoogleCalendarEvent]:
    """Sets the calendar owner's RSVP (``responseStatus``) on an invitation.

    Args:
        credentials: Valid Google OAuth2 credentials.
        event_id: The ID of the event to respond to.
        response_status: One of 'accepted', 'declined', 'tentative', 'needsAction'.
        calendar_id: Calendar identifier the invitation lives on.
        comment: Optional RSVP comment sent to the organizer.
        send_notifications: Whether to notify the organizer.

    Returns:
        The updated GoogleCalendarEvent, or None if the response could not be parsed.

    Raises:
        HttpError: If the Google Calendar API rejects the request.
        ValueError: If response_status is invalid or the user is not an attendee.
    """
    if response_status not in RESPONSE_STATUSES:
        raise ValueError(
            f"response_status must be one of {', '.join(RESPONSE_STATUSES)}; got '{response_status}'."
        )

    service = _get_calendar_service(credentials)

    try:
        existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        attendees = existing.get('attendees') or []

        self_index = next(
            (i for i, a in enumerate(attendees) if a.get('self')),
            None,
        )
        if self_index is None and calendar_id != 'primary':
            self_index = next(
                (i for i, a in enumerate(attendees)
                 if (a.get('email') or '').lower() == calendar_id.lower()),
                None,
            )
        if self_index is None:
            raise ValueError(
                f"Event '{event_id}' has no attendee entry for this calendar, so there is "
                "nothing to RSVP to. Only invitations can be responded to."
            )

        attendees[self_index]['responseStatus'] = response_status
        if comment is not None:
            attendees[self_index]['comment'] = comment

        logger.info(f"Setting RSVP for event '{event_id}' to '{response_status}'.")
        updated = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={'attendees': attendees},
            sendNotifications=send_notifications,
        ).execute()
        return GoogleCalendarEvent(**updated)

    except HttpError as error:
        logger.error(
            f"Google API error while responding to event '{event_id}': "
            f"{getattr(error.resp, 'status', '?')}",
            exc_info=True,
        )
        raise
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error while responding to event '{event_id}': {e}", exc_info=True)
        return None


def get_calendar_timezone(
    credentials: Credentials,
    calendar_id: str = 'primary',
) -> Optional[str]:
    """Returns the IANA timezone of a calendar (e.g. 'Europe/Berlin').

    Used to interpret naive datetimes the caller supplied without an offset.
    Returns None when the calendar cannot be read, so callers can fall back.
    """
    service = _get_calendar_service(credentials)
    try:
        settings = service.calendars().get(calendarId=calendar_id).execute()
        return settings.get('timeZone')
    except Exception as e:
        logger.warning(f"Could not read the timezone of calendar '{calendar_id}': {e}")
        return None
