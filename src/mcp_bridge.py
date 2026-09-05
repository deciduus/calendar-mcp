import asyncio
import os
import requests
import json
import logging
import threading
import time
from typing import Optional, List, Dict, Any
from datetime import datetime
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Configure logging
logger = logging.getLogger(__name__)

# run_server.py already calls load_dotenv() before importing this module, but do
# it here too so the bridge works when imported standalone (e.g. tests).
load_dotenv()

# Base URL for the FastAPI server
# HOST is the server's *bind* address (may be 0.0.0.0, which is not dialable), so
# the bridge always dials a real address: MCP_API_HOST if set, else loopback.
API_HOST = os.getenv("MCP_API_HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
BASE_URL = f"http://{API_HOST}:{PORT}"

# --- Readiness gate -------------------------------------------------------
# An MCP client can launch run_server.py and call a tool before uvicorn has
# finished binding. Tools wait here (lazily, never at import or MCP init, so the
# MCP handshake still answers instantly) until GET /health responds.
_ready = False
_ready_lock = threading.Lock()


def wait_for_api(timeout: int = 30) -> bool:
    """Blocks until the FastAPI server answers GET /health, or the timeout passes.

    Returns True once the API is up. The successful result is cached, so the
    polling only ever happens on the first tool call.
    """
    global _ready
    # The lock only guards the cached flag; polling happens outside it so
    # concurrent callers don't serialise into back-to-back timeouts.
    with _ready_lock:
        if _ready:
            return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{BASE_URL}/health", timeout=2)
            if response.status_code == 200:
                with _ready_lock:
                    _ready = True
                logger.info(f"FastAPI server ready at {BASE_URL}")
                return True
        except requests.RequestException:
            pass
        with _ready_lock:
            if _ready:  # another caller's poll succeeded meanwhile
                return True
        time.sleep(0.25)

    logger.error(f"Timed out after {timeout}s waiting for {BASE_URL}/health")
    return False


async def _call(method: str, path: str, expected_status: int = 200,
                success_result: Any = None, **kwargs) -> str:
    """Waits for the API, performs the request, and returns a JSON string.

    Returns json.dumps of the response body (or of ``success_result`` when
    given, for endpoints with an empty body), or of an ``{"error": ...}`` object.
    """
    # time.sleep inside the poll would block the event loop, so run it off-thread.
    if not await asyncio.to_thread(wait_for_api):
        error_msg = (
            f"Error: Calendar API at {BASE_URL} did not become ready in time. "
            "Is the FastAPI server running?"
        )
        logger.error(error_msg)
        return json.dumps({"error": error_msg})

    try:
        response = requests.request(method, f"{BASE_URL}{path}", **kwargs)
        if response.status_code != expected_status:
            error_msg = f"Error: {response.status_code} - {response.text}"
            logger.error(error_msg)
            return json.dumps({"error": error_msg})

        if success_result is not None:
            return json.dumps(success_result)
        # Ensure we're returning clean JSON
        return json.dumps(response.json(), indent=2)
    except Exception as e:
        error_msg = f"An error occurred: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return json.dumps({"error": error_msg})


def create_mcp_server():
    """Creates and configures the MCP server with tools that map to the FastAPI endpoints."""
    mcp = FastMCP("calendar-mcp")

    @mcp.tool()
    async def list_calendars(min_access_role: str = None) -> str:
        """Lists the calendars on the user's calendar list.

        Args:
            min_access_role: Minimum access role ('reader', 'writer', 'owner').
        """
        params = {}
        if min_access_role:
            params["min_access_role"] = min_access_role

        return await _call("GET", "/calendars", 200, params=params)

    @mcp.tool()
    async def find_events(calendar_id: str, time_min: str = None,
                         time_max: str = None, query: str = None,
                         max_results: int = 50) -> str:
        """Find events in a specified calendar.

        Args:
            calendar_id: Calendar identifier (e.g., 'primary', email address, or calendar ID).
            time_min: Start time (inclusive, ISO format).
            time_max: End time (exclusive, ISO format).
            query: Free text search query.
            max_results: Maximum number of events to return (default 50).
        """
        params = {"max_results": max_results}
        if time_min:
            params["time_min"] = time_min
        if time_max:
            params["time_max"] = time_max
        if query:
            params["q"] = query

        return await _call("GET", f"/calendars/{calendar_id}/events", 200, params=params)

    @mcp.tool()
    async def create_event(calendar_id: str, summary: str, start_time: str,
                          end_time: str, description: str = None,
                          location: str = None,
                          attendee_emails: List[str] = None) -> str:
        """Creates a new event with detailed information.

        Args:
            calendar_id: Calendar identifier.
            summary: Title of the event.
            start_time: Start time in ISO format (YYYY-MM-DDTHH:MM:SS).
            end_time: End time in ISO format (YYYY-MM-DDTHH:MM:SS).
            description: Optional description for the event.
            location: Optional location for the event.
            attendee_emails: Optional list of attendee email addresses.
        """
        data = {
            "summary": summary,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time}
        }

        if description:
            data["description"] = description
        if location:
            data["location"] = location
        if attendee_emails:
            data["attendees"] = attendee_emails

        return await _call("POST", f"/calendars/{calendar_id}/events", 201, json=data)

    @mcp.tool()
    async def quick_add_event(calendar_id: str, text: str) -> str:
        """Creates an event based on a simple text string using Google's natural language parser.

        Args:
            calendar_id: Calendar identifier.
            text: The text description of the event (e.g., "Meeting with John tomorrow at 2pm").
        """
        data = {"text": text}
        return await _call(
            "POST", f"/calendars/{calendar_id}/events/quickAdd", 201, json=data
        )

    @mcp.tool()
    async def update_event(calendar_id: str, event_id: str, summary: str = None,
                          start_time: str = None, end_time: str = None,
                          description: str = None, location: str = None) -> str:
        """Updates an existing event.

        Args:
            calendar_id: Calendar identifier.
            event_id: Event identifier.
            summary: New title for the event.
            start_time: New start time in ISO 8601 format (e.g., 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DDTHH:MM:SS+HH:MM').
            end_time: New end time in ISO 8601 format (e.g., 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DDTHH:MM:SS+HH:MM').
            description: New description for the event.
            location: New location for the event.
        """
        data = {}
        if summary:
            data["summary"] = summary
        if start_time:
            data["start"] = {"dateTime": start_time}
        if end_time:
            data["end"] = {"dateTime": end_time}
        if description:
            data["description"] = description
        if location:
            data["location"] = location

        return await _call(
            "PATCH", f"/calendars/{calendar_id}/events/{event_id}", 200, json=data
        )

    @mcp.tool()
    async def delete_event(calendar_id: str, event_id: str) -> str:
        """Deletes an event.

        Args:
            calendar_id: Calendar identifier.
            event_id: Event identifier.
        """
        return await _call(
            "DELETE", f"/calendars/{calendar_id}/events/{event_id}", 204,
            success_result={"success": "Event successfully deleted."}
        )

    @mcp.tool()
    async def add_attendee(calendar_id: str, event_id: str, attendee_emails: List[str]) -> str:
        """Adds one or more attendees to an existing event.

        Args:
            calendar_id: Calendar identifier.
            event_id: Event identifier.
            attendee_emails: List of email addresses to add as attendees.
        """
        data = {"attendee_emails": attendee_emails}
        return await _call(
            "POST", f"/calendars/{calendar_id}/events/{event_id}/attendees", 200, json=data
        )

    @mcp.tool()
    async def check_attendee_status(event_id: str, calendar_id: str = "primary",
                                   attendee_emails: List[str] = None) -> str:
        """Checks the response status for attendees of a specific event.

        Args:
            event_id: Event identifier.
            calendar_id: Calendar identifier (default: primary).
            attendee_emails: Optional list of specific attendees to check.
        """
        data = {
            "event_id": event_id,
            "calendar_id": calendar_id
        }
        if attendee_emails:
            data["attendee_emails"] = attendee_emails

        return await _call("POST", "/events/check_attendee_status", 200, json=data)

    @mcp.tool()
    async def query_free_busy(calendar_ids: List[str], time_min: str, time_max: str) -> str:
        """Queries the free/busy information for a list of calendars over a time period.

        Args:
            calendar_ids: List of calendar identifiers to query.
            time_min: Start of the time range (ISO format).
            time_max: End of the time range (ISO format).
        """
        data = {
            "time_min": time_min,
            "time_max": time_max,
            "items": [{"id": cal_id} for cal_id in calendar_ids]
        }
        return await _call("POST", "/freeBusy", 200, json=data)

    @mcp.tool()
    async def schedule_mutual(attendee_calendar_ids: List[str], time_min: str,
                             time_max: str, duration_minutes: int,
                             summary: str, description: str = None) -> str:
        """Finds the first available time slot for multiple attendees and schedules an event.

        Args:
            attendee_calendar_ids: List of calendar IDs for attendees.
            time_min: Start of the search window (ISO format).
            time_max: End of the search window (ISO format).
            duration_minutes: Required duration of the event in minutes.
            summary: Title for the event.
            description: Optional description for the event.
        """
        data = {
            "attendee_calendar_ids": attendee_calendar_ids,
            "time_min": time_min,
            "time_max": time_max,
            "duration_minutes": duration_minutes,
            "event_details": {
                "summary": summary,
                "start": {"date": "1970-01-01"},
                "end": {"date": "1970-01-01"}
            }
        }
        if description:
            data["event_details"]["description"] = description

        return await _call("POST", "/schedule_mutual", 201, json=data)

    @mcp.tool()
    async def analyze_busyness(time_min: str, time_max: str, calendar_id: str = "primary") -> str:
        """Analyzes event count and total duration per day within a specified time window.

        Args:
            time_min: Start of the analysis window (ISO format).
            time_max: End of the analysis window (ISO format).
            calendar_id: Calendar identifier (default: primary).
        """
        data = {
            "time_min": time_min,
            "time_max": time_max,
            "calendar_id": calendar_id
        }
        return await _call("POST", "/analyze_busyness", 200, json=data)

    @mcp.tool()
    async def create_calendar(summary: str) -> str:
        """Creates a new secondary calendar.

        Args:
            summary: The title for the new calendar.
        """
        data = {"summary": summary}
        return await _call("POST", "/calendars", 201, json=data)

    return mcp
