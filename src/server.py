import logging
import uvicorn
import sys
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from datetime import datetime, date, time
from typing import Optional, List, Dict, Any
import json
from dateutil import parser # Import dateutil parser

# Configure logging first to capture any startup errors
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add the parent directory to the path to ensure imports work in all environments
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
    logger.info(f"Added {parent_dir} to Python path")

from fastapi import FastAPI, HTTPException, Body, Query, Path, Depends
from fastapi.routing import APIRoute
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# Import functions and models directly using absolute imports
try:
    # Use absolute imports for consistency
    from src.auth import get_credentials
    import src.calendar_actions as calendar_actions
    from src.models import (
        GoogleCalendarEvent,
        EventsResponse,
        EventCreateRequest,
        QuickAddEventRequest,
        EventUpdateRequest,
        AddAttendeeRequest,
        CalendarListResponse,
        CalendarListEntry,
        # New models for advanced actions
        CheckAttendeeStatusRequest, CheckAttendeeStatusResponse,
        FreeBusyRequest, FreeBusyResponse,
        ScheduleMutualRequest,
        ProjectRecurringRequest, ProjectRecurringResponse, ProjectedEventOccurrenceModel,
        AnalyzeBusynessRequest, AnalyzeBusynessResponse, DailyBusynessStats,
        # Specific models needed for freeBusy conversion
        CalendarBusyInfo, TimePeriod, FreeBusyError
    )
    from src.analysis import ProjectedEventOccurrence
    logger.info("Successfully imported modules")
except ImportError as e:
    logger.error(f"Could not import modules: {e}")
    # Continue to allow partial server functionality

# --- Global State / Initialization ---
# Store credentials globally or pass them around
# For simplicity, let's get them once on startup
# In a production scenario, consider more robust credential management
global_credentials: Optional[Credentials] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Attempt to get credentials on server startup."""
    global global_credentials
    logger.info("Server starting up. Attempting to authenticate with Google...")
    try:
        global_credentials = get_credentials()
        if not global_credentials or not global_credentials.valid:
            # Log error but allow server to start; endpoints requiring auth will fail until fixed.
            logger.error("Failed to obtain valid Google credentials on startup. Endpoints requiring auth will be unavailable.")
        else:
            logger.info("Successfully obtained Google credentials.")
    except Exception as e:
        logger.error(f"An error occurred during startup authentication: {e}. Endpoints requiring auth will be unavailable.", exc_info=True)
        # Set credentials to None to indicate failure
        global_credentials = None
    yield

app = FastAPI(
    title="Google Calendar MCP Server",
    description="MCP server for interacting with Google Calendar API.",
    version="0.1.0",
    lifespan=lifespan
)

# --- Dependency for Credentials ---
def get_current_credentials() -> Credentials:
    """Dependency to provide valid credentials to endpoints. Attempts refresh if invalid."""
    global global_credentials

    if not global_credentials:
        logger.warning("Credentials not available (failed during startup?). Attempting to re-fetch.")
        try:
            global_credentials = get_credentials()
            if not global_credentials:
                 raise HTTPException(
                    status_code=503, 
                    detail="Google API credentials are not available. Initial fetch failed."
                )
        except Exception as e:
            logger.error(f"Failed to re-fetch credentials: {e}", exc_info=True)
            raise HTTPException(
                status_code=503, 
                detail=f"Google API credentials unavailable. Failed to re-fetch: {e}"
            )
    
    # Check if valid, try refreshing if expired or invalid
    if not global_credentials.valid:
        logger.warning("Credentials are invalid or expired. Attempting refresh...")
        try:
            global_credentials.refresh(Request()) # Requires: from google.auth.transport.requests import Request
            if not global_credentials.valid:
                logger.error("Credential refresh succeeded but credentials still invalid.")
                raise HTTPException(
                    status_code=503, 
                    detail="Google API credentials invalid after refresh attempt."
                )
            logger.info("Credentials refreshed successfully within dependency.")
        except Exception as e:
            logger.error(f"Failed to refresh credentials within dependency: {e}", exc_info=True)
            # If refresh fails, try a full re-fetch as a last resort
            logger.warning("Refresh failed. Attempting a full re-fetch of credentials...")
            try:
                global_credentials = get_credentials()
                if not global_credentials or not global_credentials.valid:
                    raise HTTPException(
                        status_code=503,
                        detail="Google API credentials unavailable after failed refresh and re-fetch."
                    )
                logger.info("Credentials re-fetched successfully after failed refresh.")
            except Exception as inner_e:
                logger.error(f"Failed to re-fetch credentials after failed refresh: {inner_e}", exc_info=True)
                raise HTTPException(
                    status_code=503, 
                    detail=f"Google API credentials unavailable. Refresh and re-fetch failed: {inner_e}"
                )

    return global_credentials

# --- MCP Offerings Endpoint --- 

def clean_schema_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively replace $ref with the actual schema definition name."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref_path = schema["$ref"]
            # Extract the schema name (e.g., '#/components/schemas/MyModel' -> 'MyModel')
            schema_name = ref_path.split('/')[-1]
            return {"type": "schema_ref", "schema_name": schema_name} # Replace ref with a marker
        return {k: clean_schema_refs(v) for k, v in schema.items()}
    elif isinstance(schema, list):
        return [clean_schema_refs(item) for item in schema]
    return schema

def map_openapi_type_to_mcp(openapi_type: str, format: Optional[str] = None) -> str:
    """Maps OpenAPI types to basic MCP types."""
    # Basic mapping, can be expanded
    if openapi_type == "string":
        if format == "date-time":
            return "datetime"
        elif format == "date":
            return "date"
        elif format == "email":
            return "email"
        # Add other string formats if needed
        return "string"
    elif openapi_type == "integer":
        return "integer"
    elif openapi_type == "number":
        return "number" # Or float?
    elif openapi_type == "boolean":
        return "boolean"
    elif openapi_type == "array":
        return "array"
    elif openapi_type == "object":
        return "object"
    return "any" # Default fallback

@app.get("/services/offerings", tags=["MCP"], operation_id="list_mcp_offerings")
def list_mcp_offerings():
    """MCP endpoint to list available tools (functions)."""
    offerings = []
    openapi_schema = app.openapi()
    schemas = openapi_schema.get("components", {}).get("schemas", {})

    for path, path_item in openapi_schema.get("paths", {}).items():
        # Skip MCP, docs, health endpoints
        if path.startswith("/services") or path in ["/docs", "/redoc", "/openapi.json", "/health"]:
            continue

        for method, operation in path_item.items():
            if method not in ["get", "post", "patch", "delete", "put"]:
                continue # Skip non-standard methods like parameters

            tool_id = operation.get("operationId") or f"{method}_{path.replace('/', '_').strip('_')}"
            summary = operation.get("summary", "No summary available")
            description = operation.get("description") or summary # Use summary if no description

            parameters = []

            # Process path and query parameters
            for param in operation.get("parameters", []):
                param_schema = param.get("schema", {})
                parameters.append({
                    "name": param.get("name"),
                    "description": param.get("description", ""),
                    "type": map_openapi_type_to_mcp(param_schema.get("type")),
                    "required": param.get("required", False)
                })

            # Process request body parameters
            request_body = operation.get("requestBody")
            if request_body:
                content = request_body.get("content", {})
                # Assume application/json
                json_content = content.get("application/json", {})
                body_schema_ref = json_content.get("schema", {}).get("$ref")
                if body_schema_ref:
                    schema_name = body_schema_ref.split('/')[-1]
                    body_schema = schemas.get(schema_name, {})
                    if body_schema.get("type") == "object" and "properties" in body_schema:
                        for prop_name, prop_details in body_schema.get("properties", {}).items():
                            is_required = prop_name in body_schema.get("required", [])
                            # Use alias if present, otherwise the property name
                            field_name = prop_details.get("alias", prop_name)
                            parameters.append({
                                "name": field_name,
                                "description": prop_details.get("description") or prop_details.get("title", ""),
                                "type": map_openapi_type_to_mcp(prop_details.get("type"), prop_details.get("format")),
                                "required": is_required
                                # TODO: Handle nested objects/arrays more thoroughly if needed
                            })
                    else:
                         # Handle cases where the body is not a direct object schema (e.g., simple type)
                         parameters.append({
                            "name": "request_body", # Generic name
                            "description": request_body.get("description", "Request body"),
                            "type": map_openapi_type_to_mcp(body_schema.get("type")), # Type of the schema itself
                            "required": request_body.get("required", True)
                        })


            # Note: This simple extraction might not capture all nuances of complex parameters.
            # Return type extraction could be added similarly by inspecting 'responses'.

            offerings.append({
                "offering_id": tool_id,  # Changed from tool_id to offering_id for MCP format
                "name": summary, # Often used as function name
                "description": description,
                "parameters": parameters
            })

    return {"offerings": offerings}

@app.get("/services/api_key", tags=["MCP"], operation_id="get_api_key")
def get_api_key():
    """MCP endpoint to get API key - not required but part of MCP protocol."""
    return {"api_key": "not-required"}

# --- Management Endpoint ---
@app.get("/health", tags=["Management"], operation_id="health_check")
def health_check():
    """Basic health check endpoint."""
    auth_status = "authenticated" if global_credentials and global_credentials.valid else "authentication_failed_or_pending"
    return {"status": "ok", "authentication": auth_status}

# --- CalendarList Endpoints ---
@app.get(
    "/calendars",
    response_model=CalendarListResponse,
    tags=["Calendars"],
    summary="List Calendars",
    operation_id="list_calendars"
)
def list_calendars_endpoint(
    min_access_role: Optional[str] = Query(None, description="Minimum access role ('reader', 'writer', 'owner')."),
    creds: Credentials = Depends(get_current_credentials)
):
    """Lists the calendars on the user's calendar list."""
    logger.info(f"Endpoint 'list_calendars' called. Params: min_access_role='{min_access_role}'")
    result = calendar_actions.find_calendars(credentials=creds, min_access_role=min_access_role)
    if result is None:
        logger.error("Action 'find_calendars' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail="Failed to retrieve calendar list from Google API.")
    logger.info(f"Endpoint 'list_calendars' completed successfully. Returning {len(result.items)} calendars.")
    return result

class CreateCalendarRequest(BaseModel):
    summary: str

@app.post(
    "/calendars",
    response_model=CalendarListEntry,
    status_code=201, # Created
    tags=["Calendars"],
    summary="Create Calendar",
    operation_id="create_calendar"
)
def create_calendar_endpoint(
    request: CreateCalendarRequest,
    creds: Credentials = Depends(get_current_credentials)
):
    """Creates a new secondary calendar."""
    logger.info(f"Endpoint 'create_calendar' called. Summary: '{request.summary}'")
    result = calendar_actions.create_calendar(credentials=creds, summary=request.summary)
    if result is None:
        logger.error(f"Action 'create_calendar' for summary '{request.summary}' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail="Failed to create calendar via Google API.")
    logger.info(f"Endpoint 'create_calendar' completed. Calendar ID: {result.id}")
    return result

# --- Events Endpoints ---
@app.get(
    "/calendars/{calendar_id}/events",
    response_model=EventsResponse,
    tags=["Events"],
    summary="Find Events",
    operation_id="find_events"
)
def find_events_endpoint(
    calendar_id: str = Path(..., description="Calendar identifier (e.g., 'primary', email address, or calendar ID)."),
    time_min_str: Optional[str] = Query(None, alias="time_min", description="Start time (inclusive, RFC3339 format string)."),
    time_max_str: Optional[str] = Query(None, alias="time_max", description="End time (exclusive, RFC3339 format string)."),
    query: Optional[str] = Query(None, alias="q", description="Free text search query."),
    max_results: int = Query(50, ge=1, le=2500, description="Maximum results per page."),
    single_events: bool = Query(True, description="Expand recurring events."),
    order_by: str = Query('startTime', description="Order results by ('startTime' or 'updated')."),
    creds: Credentials = Depends(get_current_credentials)
):
    """Finds events in a specified calendar."""
    logger.info(f"Endpoint 'find_events' called for calendar '{calendar_id}'.")
    logger.debug(f"Raw Params: time_min_str='{time_min_str}', time_max_str='{time_max_str}', q='{query}', max_results={max_results}, single_events={single_events}, order_by='{order_by}'")

    # Manually parse time strings using dateutil.parser
    time_min_dt: Optional[datetime] = None
    time_max_dt: Optional[datetime] = None
    try:
        if time_min_str:
            time_min_dt = parser.isoparse(time_min_str)
        if time_max_str:
            time_max_dt = parser.isoparse(time_max_str)
    except ValueError as e:
        logger.error(f"Failed to parse time strings: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid time format provided: {e}")

    # Now call the action function with parsed datetime objects
    result = calendar_actions.find_events(
        credentials=creds,
        calendar_id=calendar_id,
        time_min=time_min_dt, # Pass parsed datetime
        time_max=time_max_dt, # Pass parsed datetime
        query=query,
        max_results=max_results,
        single_events=single_events,
        order_by=order_by
    )
    if result is None:
        # Distinguish between API error and just no events?
        # For now, assume None means API error.
        logger.error(f"Action 'find_events' for calendar '{calendar_id}' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail="Failed to retrieve events from Google API.")
    logger.info(f"Endpoint 'find_events' for calendar '{calendar_id}' completed. Found {len(result.items)} events.")
    return result

@app.post(
    "/calendars/{calendar_id}/events",
    response_model=GoogleCalendarEvent,
    status_code=201,
    tags=["Events"],
    summary="Create Detailed Event",
    operation_id="create_event"
)
def create_event_endpoint(
    event_data: EventCreateRequest,
    calendar_id: str = Path(..., description="Calendar identifier."),
    send_notifications: bool = Query(True, description="Send notifications to attendees."),
    creds: Credentials = Depends(get_current_credentials)
):
    """Creates a new event with detailed information."""
    logger.info(f"Endpoint 'create_event' called for calendar '{calendar_id}'. Summary: '{event_data.summary}'")
    logger.debug(f"Event data: {event_data.dict(exclude_unset=True)}")
    result = calendar_actions.create_event(
        credentials=creds,
        event_data=event_data,
        calendar_id=calendar_id,
        send_notifications=send_notifications
    )
    if result is None:
        logger.error(f"Action 'create_event' for calendar '{calendar_id}', summary '{event_data.summary}' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail="Failed to create event via Google API.")
    logger.info(f"Endpoint 'create_event' completed. Event ID: {result.id}")
    return result

@app.post(
    "/calendars/{calendar_id}/events/quickAdd",
    response_model=GoogleCalendarEvent,
    status_code=201,
    tags=["Events"],
    summary="Quick Add Event",
    operation_id="quick_add_event"
)
def quick_add_event_endpoint(
    request_data: QuickAddEventRequest,
    calendar_id: str = Path(..., description="Calendar identifier."),
    send_notifications: bool = Query(False, description="Send notifications to attendees."),
    creds: Credentials = Depends(get_current_credentials)
):
    """Creates an event from a simple text string."""
    logger.info(f"Endpoint 'quick_add_event' called for calendar '{calendar_id}'. Text: '{request_data.text}'")
    result = calendar_actions.quick_add_event(
        credentials=creds,
        text=request_data.text,
        calendar_id=calendar_id,
        send_notifications=send_notifications
    )
    if result is None:
        # Consider 400 if text was likely unparseable? Hard to know.
        logger.error(f"Action 'quick_add_event' for calendar '{calendar_id}', text '{request_data.text}' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail="Failed to quick-add event via Google API.")
    logger.info(f"Endpoint 'quick_add_event' completed. Event ID: {result.id}")
    return result

@app.patch(
    "/calendars/{calendar_id}/events/{event_id}",
    response_model=GoogleCalendarEvent,
    tags=["Events"],
    summary="Update Event (Patch)",
    operation_id="update_event"
)
def update_event_endpoint(
    update_data: EventUpdateRequest,
    calendar_id: str = Path(..., description="Calendar identifier."),
    event_id: str = Path(..., description="Event identifier."),
    send_notifications: bool = Query(True, description="Send notifications to attendees."),
    creds: Credentials = Depends(get_current_credentials)
):
    """Updates specified fields of an existing event."""
    logger.info(f"Endpoint 'update_event' called for event '{event_id}' in calendar '{calendar_id}'.")
    logger.debug(f"Update data: {update_data.dict(exclude_unset=True)}")
    result = calendar_actions.update_event(
        credentials=creds,
        event_id=event_id,
        update_data=update_data,
        calendar_id=calendar_id,
        send_notifications=send_notifications
    )
    if result is None:
        # update_event handles 404 logging, but we might want to return 404 here
        # Need a way for the action function to signal the error type
        # For now, assume 500 for any None return
        # Alternative: Raise custom exceptions from actions
        logger.error(f"Action 'update_event' for event '{event_id}' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail=f"Failed to update event '{event_id}'. Check server logs.")
    logger.info(f"Endpoint 'update_event' completed for event '{event_id}'.")
    return result

@app.delete(
    "/calendars/{calendar_id}/events/{event_id}",
    status_code=204, # No Content
    tags=["Events"],
    summary="Delete Event",
    operation_id="delete_event"
)
def delete_event_endpoint(
    calendar_id: str = Path(..., description="Calendar identifier."),
    event_id: str = Path(..., description="Event identifier."),
    send_notifications: bool = Query(True, description="Send notifications to attendees."),
    creds: Credentials = Depends(get_current_credentials)
):
    """Deletes an event."""
    logger.info(f"Endpoint 'delete_event' called for event '{event_id}' in calendar '{calendar_id}'.")
    success = calendar_actions.delete_event(
        credentials=creds,
        event_id=event_id,
        calendar_id=calendar_id,
        send_notifications=send_notifications
    )
    if not success:
        # delete_event handles 404 logging
        logger.error(f"Action 'delete_event' for event '{event_id}' returned False. Raising HTTPException.")
        raise HTTPException(status_code=500, detail=f"Failed to delete event '{event_id}'. It might not exist or an API error occurred.")
    # No body needed for 204 response
    logger.info(f"Endpoint 'delete_event' completed successfully for event '{event_id}'.")
    return None

@app.post(
    "/calendars/{calendar_id}/events/{event_id}/attendees",
    response_model=GoogleCalendarEvent,
    tags=["Events"],
    summary="Add Attendee(s)",
    operation_id="add_attendee"
)
def add_attendee_endpoint(
    request_data: AddAttendeeRequest,
    calendar_id: str = Path(..., description="Calendar identifier."),
    event_id: str = Path(..., description="Event identifier."),
    send_notifications: bool = Query(True, description="Send notifications to attendees."),
    creds: Credentials = Depends(get_current_credentials)
):
    """Adds one or more attendees to an existing event.
       Note: This retrieves the event, adds the new emails to the existing list, and patches the event.
    """
    logger.info(f"Endpoint 'add_attendee' called for event '{event_id}'. Attendees: {request_data.attendee_emails}")
    result = calendar_actions.add_attendee(
        credentials=creds,
        event_id=event_id,
        attendee_emails=request_data.attendee_emails,
        calendar_id=calendar_id,
        send_notifications=send_notifications
    )
    if result is None:
        logger.error(f"Action 'add_attendee' for event '{event_id}' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail=f"Failed to add attendees to event '{event_id}'. Check logs.")
    logger.info(f"Endpoint 'add_attendee' completed for event '{event_id}'.")
    return result

# --- Advanced Scheduling & Analysis Endpoints ---

@app.post(
    "/events/check_attendee_status",
    response_model=CheckAttendeeStatusResponse,
    tags=["Advanced Scheduling"],
    summary="Check Attendee Response Status",
    operation_id="check_attendee_status"
)
def check_attendee_status_endpoint(
    request: CheckAttendeeStatusRequest,
    creds: Credentials = Depends(get_current_credentials)
):
    """Checks the response status ('accepted', 'declined', etc.) for attendees of a specific event."""
    logger.info(f"Endpoint 'check_attendee_status' called for event '{request.event_id}'. Calendar: '{request.calendar_id}'. Attendees: {request.attendee_emails or 'All'}")
    status_dict = calendar_actions.check_attendee_status(
        credentials=creds,
        event_id=request.event_id,
        calendar_id=request.calendar_id,
        attendee_emails=request.attendee_emails
    )
    if status_dict is None:
        # Could be 404 if event not found, but action logs this.
        logger.error(f"Action 'check_attendee_status' for event '{request.event_id}' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail=f"Failed to check attendee status for event '{request.event_id}'. Event might not exist or API error.")
    logger.info(f"Endpoint 'check_attendee_status' completed for event '{request.event_id}'. Found status for {len(status_dict)} attendees.")
    return CheckAttendeeStatusResponse(status_map=status_dict)

@app.post(
    "/freeBusy",
    response_model=FreeBusyResponse,
    tags=["Advanced Scheduling"],
    summary="Query Free/Busy Information",
    operation_id="query_free_busy"
)
def query_free_busy_endpoint(
    request: FreeBusyRequest,
    creds: Credentials = Depends(get_current_credentials)
):
    """Queries the free/busy information for a list of calendars over a time period."""
    calendar_ids = [item.id for item in request.items]
    logger.info(f"Endpoint 'query_free_busy' called. Calendars: {calendar_ids}")
    logger.debug(f"Time range: {request.time_min} to {request.time_max}")

    # Call the action function (which now returns the complex dict)
    busy_info_dict = calendar_actions.find_availability(
        credentials=creds,
        time_min=request.time_min,
        time_max=request.time_max,
        calendar_ids=calendar_ids
    )

    if busy_info_dict is None:
        logger.error("Action 'find_availability' returned None. Raising HTTPException.")
        raise HTTPException(status_code=500, detail="Failed to query free/busy information via Google API.")

    # Convert the result from find_availability back into the FreeBusyResponse model structure
    response_calendars: Dict[str, CalendarBusyInfo] = {}
    for cal_id, data in busy_info_dict.items():
        response_calendars[cal_id] = CalendarBusyInfo(
            busy=[TimePeriod(start=p['start'], end=p['end']) for p in data.get('busy', [])],
            errors=[FreeBusyError(**err) for err in data.get('errors', [])] # Assuming error dict matches model
        )

    # Construct the final response model
    # Note: Google API requires timeMin/timeMax in the request but also returns them in the response
    return FreeBusyResponse(
        time_min=request.time_min, # Echo request params as per Google API response structure
        time_max=request.time_max,
        calendars=response_calendars
    )

@app.post(
    "/schedule_mutual",
    response_model=GoogleCalendarEvent,
    status_code=201, # Successfully created
    tags=["Advanced Scheduling"],
    summary="Find Mutual Availability and Schedule",
    operation_id="schedule_mutual"
)
def schedule_mutual_endpoint(
    request: ScheduleMutualRequest,
    creds: Credentials = Depends(get_current_credentials)
):
    """Finds the first available time slot for multiple attendees and schedules the provided event details."""
    logger.info(f"Endpoint 'schedule_mutual' called. Attendees: {request.attendee_calendar_ids}. Duration: {request.duration_minutes} mins.")
    logger.debug(f"Time range: {request.time_min} to {request.time_max}. Organizer: {request.organizer_calendar_id}. Event Summary: {request.event_details.summary}")
    # Parse working hours strings into time objects
    working_hours_start = None
    working_hours_end = None
    try:
        if request.working_hours_start_str:
            working_hours_start = datetime.strptime(request.working_hours_start_str, '%H:%M').time()
        if request.working_hours_end_str:
            working_hours_end = datetime.strptime(request.working_hours_end_str, '%H:%M').time()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid working hours format. Use HH:MM.")

    created_event = calendar_actions.find_mutual_availability_and_schedule(
        credentials=creds,
        attendee_calendar_ids=request.attendee_calendar_ids,
        time_min=request.time_min,
        time_max=request.time_max,
        duration_minutes=request.duration_minutes,
        event_details=request.event_details,
        organizer_calendar_id=request.organizer_calendar_id,
        working_hours_start=working_hours_start,
        working_hours_end=working_hours_end,
        send_notifications=request.send_notifications
    )

    if created_event is None:
        # Could be no slot found, or failed to create event after finding slot.
        # Action function logs the reason.
        logger.error("Action 'find_mutual_availability_and_schedule' returned None. Raising HTTPException.")
        raise HTTPException(status_code=409, detail="Could not schedule event. No suitable time slot found or event creation failed.") # 409 Conflict maybe?
    logger.info(f"Endpoint 'schedule_mutual' completed successfully. Event ID: {created_event.id}")
    return created_event

@app.post(
    "/project_recurring",
    response_model=ProjectRecurringResponse,
    tags=["Analysis"],
    summary="Project Recurring Event Occurrences",
    operation_id="project_recurring"
)
def project_recurring_endpoint(
    request: ProjectRecurringRequest,
    creds: Credentials = Depends(get_current_credentials)
):
    """Finds recurring events and projects their future occurrences within a time window."""
    logger.info(f"Endpoint 'project_recurring' called. Calendar: '{request.calendar_id}'. Query: '{request.event_query}'")
    logger.debug(f"Time range: {request.time_min} to {request.time_max}")
    # Note: calendar_actions.get_projected_recurring_events returns List[ProjectedEventOccurrence]
    # We need to convert this to List[ProjectedEventOccurrenceModel] for the response.
    occurrences: List[ProjectedEventOccurrence] = calendar_actions.get_projected_recurring_events(
        credentials=creds,
        time_min=request.time_min,
        time_max=request.time_max,
        calendar_id=request.calendar_id,
        event_query=request.event_query
    )

    # Convert ProjectedEventOccurrence (from analysis) to ProjectedEventOccurrenceModel (from models)
    response_occurrences = [
        ProjectedEventOccurrenceModel(**occ.__dict__) for occ in occurrences
    ]

    logger.info(f"Endpoint 'project_recurring' completed. Found {len(response_occurrences)} projected occurrences.")
    return ProjectRecurringResponse(projected_occurrences=response_occurrences)

@app.post(
    "/analyze_busyness",
    response_model=AnalyzeBusynessResponse,
    tags=["Analysis"],
    summary="Analyze Daily Event Count and Duration",
    operation_id="analyze_busyness"
)
def analyze_busyness_endpoint(
    request: AnalyzeBusynessRequest,
    creds: Credentials = Depends(get_current_credentials)
):
    """Analyzes event count and total duration per day within a specified time window."""
    logger.info(f"Endpoint 'analyze_busyness' called. Calendar: '{request.calendar_id}'")
    logger.debug(f"Time range: {request.time_min} to {request.time_max}")
    # We need a wrapper in calendar_actions for analyze_busyness from analysis.py
    # Let's add one now.
    busyness_dict = calendar_actions.get_busyness_analysis( # Call the wrapper function
        credentials=creds,
        time_min=request.time_min,
        time_max=request.time_max,
        calendar_id=request.calendar_id
    )

    if busyness_dict is None: # Wrapper returns None on error
         logger.error("Action 'get_busyness_analysis' returned None. Raising HTTPException.")
         raise HTTPException(status_code=500, detail="Failed to analyze busyness.")

    # Convert date keys to strings (YYYY-MM-DD) for JSON compatibility
    response_data = {
        dt.strftime('%Y-%m-%d'): DailyBusynessStats(**stats)
        for dt, stats in busyness_dict.items()
    }

    return AnalyzeBusynessResponse(busyness_by_date=response_data)

# Add other endpoints as needed

# --- Main Execution ---
if __name__ == "__main__":
    logger.info("Starting Google Calendar MCP Server...")
    # Note: Startup logic runs automatically with uvicorn via the lifespan handler
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000"))
    )
