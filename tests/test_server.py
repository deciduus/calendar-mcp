"""In-process tests for the MCP tool layer in calendar_mcp/server.py.

The tools are driven through the real ``MCPServer`` (``server.call_tool``), so
argument validation, the pydantic output schema and the structured content the
client would receive are all exercised. Everything below the tool layer is
mocked: the credential provider hands out a MagicMock, and the
``calendar_actions`` functions are patched, so no network, token or Google
client is involved.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError
from mcp.server.mcpserver.exceptions import ToolError

from calendar_mcp import server as server_module
from calendar_mcp.models import (
    CalendarListEntry,
    CalendarListResponse,
    EventsResponse,
    GoogleCalendarEvent,
)

UTC = timezone.utc

EXPECTED_TOOLS = {
    "list_calendars",
    "create_calendar",
    "find_events",
    "check_attendee_status",
    "query_free_busy",
    "analyze_busyness",
    "project_recurring_events",
    "create_event",
    "quick_add_event",
    "update_event",
    "move_event",
    "add_attendee",
    "respond_to_event",
    "schedule_mutual",
    "delete_event",
    "list_accounts",
    "get_preferences",
    "set_preferences",
    "time_audit",
    "find_focus_time",
    "block_focus_time",
    "detect_conflicts",
    "suggest_reschedule",
}

READ_ONLY_TOOLS = {
    "list_calendars",
    "find_events",
    "check_attendee_status",
    "query_free_busy",
    "analyze_busyness",
    "project_recurring_events",
    "list_accounts",
    "get_preferences",
    "time_audit",
    "find_focus_time",
    "detect_conflicts",
}

EVENT_PAYLOAD = {
    "id": "evt-1",
    "summary": "Team sync",
    "description": "Weekly",
    "location": "Room 1",
    "status": "confirmed",
    "htmlLink": "https://calendar.google.com/event?eid=evt-1",
    "organizer": {"email": "me@example.com"},
    "start": {"dateTime": "2026-01-01T09:00:00+00:00", "timeZone": "UTC"},
    "end": {"dateTime": "2026-01-01T10:00:00+00:00", "timeZone": "UTC"},
    "attendees": [
        {"email": "me@example.com", "self": True, "responseStatus": "accepted"},
        {"email": "guest@example.com", "responseStatus": "needsAction", "optional": True},
    ],
}


def _event(**overrides):
    payload = json.loads(json.dumps(EVENT_PAYLOAD))
    payload.update(overrides)
    return GoogleCalendarEvent(**payload)


def _http_error(status, message="Not Found"):
    payload = json.dumps({"error": {"code": status, "message": message}}).encode("utf-8")
    return HttpError(SimpleNamespace(status=status, reason=message), payload)


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def actions():
    """Patches the whole calendar_actions module the server calls into.

    Also pins the credential provider to a MagicMock and makes the timezone
    lookup deterministic, so naive timestamps resolve to UTC.
    """
    provider = MagicMock(name="CredentialProvider")
    provider.get.return_value = MagicMock(name="credentials")
    fake = MagicMock(name="calendar_actions")
    fake.get_calendar_timezone.return_value = "UTC"
    with patch.object(server_module, "credential_provider", provider), \
            patch.object(server_module, "calendar_actions", fake):
        fake.provider = provider
        yield fake


async def call(name, arguments=None):
    """Calls a tool through the MCP server and returns the CallToolResult."""
    return await server_module.server.call_tool(name, arguments or {})


async def structured(name, arguments=None):
    result = await call(name, arguments)
    assert result.is_error is False
    assert result.structured_content is not None
    return result.structured_content


# --- tool registry ------------------------------------------------------------


async def test_list_tools_exposes_exactly_the_expected_set():
    tools = await server_module.server.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert len(tools) == len(EXPECTED_TOOLS) == 23


async def test_every_tool_has_a_description_and_output_schema():
    for tool in await server_module.server.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert tool.output_schema, f"{tool.name} has no structured output schema"


async def test_ctx_is_not_part_of_any_input_schema():
    for tool in await server_module.server.list_tools():
        assert "ctx" not in (tool.input_schema.get("properties") or {}), tool.name


async def test_tool_annotations_classify_reads_writes_and_deletes():
    tools = {t.name: t for t in await server_module.server.list_tools()}

    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name

    for name in EXPECTED_TOOLS - READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name

    # Exactly one tool is destructive.
    destructive = {n for n, t in tools.items() if t.annotations.destructive_hint}
    assert destructive == {"delete_event"}


async def test_server_identity_and_instructions():
    assert server_module.server.name == "calendar-mcp"
    assert server_module.SERVER_VERSION == "1.1.0"
    assert "calendar_id" in server_module.INSTRUCTIONS


# --- list_calendars / create_calendar -----------------------------------------


def _calendar_entry(**kwargs):
    base = {"etag": "e", "id": "primary", "summary": "Me", "accessRole": "owner", "primary": True}
    base.update(kwargs)
    return CalendarListEntry(**base)


async def test_list_calendars_returns_structured_calendars(actions):
    actions.find_calendars.return_value = CalendarListResponse(
        items=[
            _calendar_entry(),
            _calendar_entry(id="work@example.com", summary="Work", primary=False,
                            timeZone="Europe/Berlin", accessRole="writer"),
        ]
    )

    data = await structured("list_calendars", {"min_access_role": "writer"})

    assert data["count"] == 2
    assert [c["id"] for c in data["calendars"]] == ["primary", "work@example.com"]
    assert data["calendars"][1]["time_zone"] == "Europe/Berlin"
    assert actions.find_calendars.call_args.kwargs["min_access_role"] == "writer"


async def test_list_calendars_prefers_the_summary_override(actions):
    actions.find_calendars.return_value = CalendarListResponse(
        items=[_calendar_entry(summary="Original", summaryOverride="My name for it")]
    )

    data = await structured("list_calendars")
    assert data["calendars"][0]["summary"] == "My name for it"


async def test_list_calendars_reports_auth_failure_as_a_tool_error(actions):
    from calendar_mcp.auth import AuthError

    actions.find_calendars.side_effect = AuthError("No valid token. Run 'calendar-mcp auth'.")

    with pytest.raises(ToolError, match="calendar-mcp auth"):
        await call("list_calendars")


async def test_list_calendars_renders_google_http_errors(actions):
    actions.find_calendars.side_effect = _http_error(403, "Insufficient permission")

    with pytest.raises(ToolError, match="403.*Insufficient permission"):
        await call("list_calendars")


async def test_create_calendar_returns_the_new_calendar(actions):
    actions.create_calendar.return_value = _calendar_entry(
        id="new-cal", summary="Client work", primary=False
    )

    data = await structured("create_calendar", {"summary": "Client work"})

    assert data == {
        "id": "new-cal",
        "summary": "Client work",
        "description": None,
        "time_zone": None,
        "access_role": "owner",
        "primary": False,
    }


# --- find_events --------------------------------------------------------------


async def test_find_events_returns_flattened_events(actions):
    actions.find_events.return_value = EventsResponse(
        timeZone="Europe/Berlin", items=[EVENT_PAYLOAD]
    )

    data = await structured(
        "find_events",
        {
            "calendar_id": "work@example.com",
            "time_min": "2026-01-01T00:00:00Z",
            "time_max": "2026-01-02T00:00:00Z",
            "query": "sync",
        },
    )

    assert data["calendar_id"] == "work@example.com"
    assert data["count"] == 1
    assert data["time_zone"] == "Europe/Berlin"
    event = data["events"][0]
    assert event["id"] == "evt-1"
    assert event["all_day"] is False
    assert event["start"] == "2026-01-01T09:00:00+00:00"
    assert event["organizer_email"] == "me@example.com"
    assert [a["email"] for a in event["attendees"]] == ["me@example.com", "guest@example.com"]
    assert event["attendees"][0]["is_self"] is True
    assert event["attendees"][1]["optional"] is True

    kwargs = actions.find_events.call_args.kwargs
    assert kwargs["calendar_id"] == "work@example.com"
    assert kwargs["query"] == "sync"
    assert kwargs["time_min"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert kwargs["time_max"] == datetime(2026, 1, 2, tzinfo=UTC)


async def test_find_events_marks_all_day_events(actions):
    actions.find_events.return_value = EventsResponse(
        items=[{"id": "d1", "summary": "Holiday",
                "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}}]
    )

    data = await structured("find_events")
    assert data["events"][0]["all_day"] is True
    assert data["events"][0]["start"] == "2026-01-01"


async def test_find_events_interprets_naive_times_in_the_calendar_timezone(actions):
    actions.get_calendar_timezone.return_value = "Europe/Berlin"
    actions.find_events.return_value = EventsResponse(items=[])

    await structured("find_events", {"time_min": "2026-01-01T09:00:00"})

    time_min = actions.find_events.call_args.kwargs["time_min"]
    assert time_min.tzinfo is not None
    # 09:00 Berlin in January is 08:00 UTC.
    assert time_min.astimezone(UTC) == datetime(2026, 1, 1, 8, tzinfo=UTC)


async def test_find_events_caches_the_calendar_timezone_lookup(actions):
    actions.get_calendar_timezone.return_value = "UTC"
    actions.find_events.return_value = EventsResponse(items=[])

    await structured("find_events", {"time_min": "2026-01-01T09:00:00"})
    await structured("find_events", {"time_min": "2026-01-02T09:00:00"})

    assert actions.get_calendar_timezone.call_count == 1


async def test_the_calendar_timezone_cache_does_not_leak_across_accounts(actions):
    """'primary' is a different calendar, in a different zone, per account."""
    zones = {"work": "America/New_York", "personal": "Europe/Berlin"}

    def creds_for(account=None):
        creds = MagicMock(name=f"credentials-{account}")
        creds._calendar_mcp_account = account
        return creds

    actions.provider.get.side_effect = creds_for
    actions.get_calendar_timezone.side_effect = (
        lambda creds, calendar_id: zones[creds._calendar_mcp_account]
    )
    actions.find_events.return_value = EventsResponse(items=[])

    await structured("find_events", {"time_min": "2026-01-01T09:00:00", "account": "work"})
    work_min = actions.find_events.call_args.kwargs["time_min"]

    await structured("find_events", {"time_min": "2026-01-01T09:00:00", "account": "personal"})
    personal_min = actions.find_events.call_args.kwargs["time_min"]

    # 09:00 New York is 14:00 UTC; 09:00 Berlin is 08:00 UTC.
    assert work_min.astimezone(UTC) == datetime(2026, 1, 1, 14, tzinfo=UTC)
    assert personal_min.astimezone(UTC) == datetime(2026, 1, 1, 8, tzinfo=UTC)


async def test_find_events_rejects_an_unparseable_timestamp(actions):
    with pytest.raises(ToolError, match="ISO 8601"):
        await call("find_events", {"time_min": "next tuesday-ish!!"})
    actions.find_events.assert_not_called()


async def test_find_events_errors_when_the_action_returns_nothing(actions):
    actions.find_events.return_value = None

    with pytest.raises(ToolError, match="did not return a usable result"):
        await call("find_events")


# --- reading: attendees, free/busy, busyness, projection ----------------------


async def test_check_attendee_status_flattens_the_status_map(actions):
    actions.check_attendee_status.return_value = {
        "me@example.com": "accepted",
        "guest@example.com": "needsAction",
    }

    data = await structured("check_attendee_status", {"event_id": "evt-1"})

    assert data["event_id"] == "evt-1"
    assert data["count"] == 2
    assert {a["email"]: a["response_status"] for a in data["attendees"]} == {
        "me@example.com": "accepted",
        "guest@example.com": "needsAction",
    }


async def test_query_free_busy_returns_iso_intervals(actions):
    actions.find_availability.return_value = {
        "a@example.com": {
            "busy": [
                {
                    "start": datetime(2026, 1, 1, 9, tzinfo=UTC),
                    "end": datetime(2026, 1, 1, 10, tzinfo=UTC),
                }
            ],
            "errors": [],
        },
        "b@example.com": {"busy": [], "errors": [{"reason": "notFound"}]},
    }

    data = await structured(
        "query_free_busy",
        {
            "calendar_ids": ["a@example.com", "b@example.com"],
            "time_min": "2026-01-01T00:00:00Z",
            "time_max": "2026-01-02T00:00:00Z",
        },
    )

    by_id = {c["calendar_id"]: c for c in data["calendars"]}
    assert by_id["a@example.com"]["busy"] == [
        {"start": "2026-01-01T09:00:00+00:00", "end": "2026-01-01T10:00:00+00:00"}
    ]
    assert by_id["b@example.com"]["errors"] == ["notFound"]


async def test_analyze_busyness_totals_days(actions):
    from datetime import date

    actions.get_busyness_analysis.return_value = {
        date(2026, 1, 1): {"event_count": 2, "total_duration_minutes": 120.0},
        date(2026, 1, 2): {"event_count": 1, "total_duration_minutes": 30.0},
    }

    data = await structured(
        "analyze_busyness",
        {"time_min": "2026-01-01T00:00:00Z", "time_max": "2026-01-03T00:00:00Z"},
    )

    assert [d["date"] for d in data["days"]] == ["2026-01-01", "2026-01-02"]
    assert data["total_events"] == 3
    assert data["total_duration_minutes"] == 150.0


async def test_project_recurring_events_returns_iso_occurrences(actions):
    actions.get_projected_recurring_events.return_value = [
        SimpleNamespace(
            original_event_id="evt-daily",
            original_summary="Standup",
            occurrence_start=datetime(2026, 1, 1, 9, tzinfo=UTC),
            occurrence_end=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
        )
    ]

    data = await structured(
        "project_recurring_events",
        {
            "time_min": "2026-01-01T00:00:00Z",
            "time_max": "2026-02-01T00:00:00Z",
            "event_query": "Standup",
        },
    )

    assert data["count"] == 1
    assert data["occurrences"][0] == {
        "event_id": "evt-daily",
        "summary": "Standup",
        "start": "2026-01-01T09:00:00+00:00",
        "end": "2026-01-01T09:15:00+00:00",
    }


# --- writing ------------------------------------------------------------------


async def test_create_event_builds_the_request_and_returns_the_event(actions):
    actions.create_event.return_value = _event()

    data = await structured(
        "create_event",
        {
            "summary": "Team sync",
            "start_time": "2026-01-01T09:00:00Z",
            "end_time": "2026-01-01T10:00:00Z",
            "description": "Weekly",
            "location": "Room 1",
            "attendee_emails": ["guest@example.com"],
            "send_notifications": False,
        },
    )

    assert data["event"]["id"] == "evt-1"
    assert "Created 'Team sync'" in data["message"]

    kwargs = actions.create_event.call_args.kwargs
    assert kwargs["send_notifications"] is False
    request = kwargs["event_data"]
    assert request.summary == "Team sync"
    assert request.start.dateTime == datetime(2026, 1, 1, 9, tzinfo=UTC)
    assert request.end.dateTime == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert request.attendees == ["guest@example.com"]


async def test_create_event_requires_a_summary(actions):
    with pytest.raises(ToolError, match="summary is required"):
        await call("create_event", {
            "start_time": "2026-01-01T09:00:00Z", "end_time": "2026-01-01T10:00:00Z"
        })
    actions.create_event.assert_not_called()


async def test_create_event_rejects_an_end_before_the_start(actions):
    with pytest.raises(ToolError, match="end_time must be after start_time"):
        await call("create_event", {
            "summary": "Backwards",
            "start_time": "2026-01-01T10:00:00Z",
            "end_time": "2026-01-01T09:00:00Z",
        })
    actions.create_event.assert_not_called()


async def test_quick_add_event_passes_the_text_through(actions):
    actions.quick_add_event.return_value = _event(summary="Dentist")

    data = await structured("quick_add_event", {"text": "Dentist Thursday 9am"})

    assert actions.quick_add_event.call_args.kwargs["text"] == "Dentist Thursday 9am"
    assert data["event"]["summary"] == "Dentist"
    assert "Dentist Thursday 9am" in data["message"]


async def test_quick_add_event_rejects_blank_text(actions):
    with pytest.raises(ToolError, match="text is required"):
        await call("quick_add_event", {"text": "   "})


async def test_update_event_sends_only_the_supplied_fields(actions):
    actions.update_event.return_value = _event()

    data = await structured("update_event", {"event_id": "evt-1", "summary": "Renamed"})

    assert data["calendar_id"] == "primary"
    update = actions.update_event.call_args.kwargs["update_data"]
    assert update.summary == "Renamed"
    assert update.start is None and update.end is None and update.location is None


async def test_update_event_requires_an_event_id(actions):
    with pytest.raises(ToolError, match="event_id is required"):
        await call("update_event", {"summary": "Renamed"})


async def test_update_event_requires_something_to_change(actions):
    with pytest.raises(ToolError, match="Nothing to update"):
        await call("update_event", {"event_id": "evt-1"})
    actions.update_event.assert_not_called()


async def test_move_event_passes_new_start_only(actions):
    actions.move_event.return_value = _event()

    data = await structured("move_event", {
        "event_id": "evt-1", "new_start": "2026-01-01T15:00:00Z"
    })

    kwargs = actions.move_event.call_args.kwargs
    assert kwargs["new_start"] == datetime(2026, 1, 1, 15, tzinfo=UTC)
    assert kwargs["new_end"] is None
    assert data["calendar_id"] == "primary"


async def test_move_event_reports_the_destination_calendar(actions):
    actions.move_event.return_value = _event()

    data = await structured("move_event", {
        "event_id": "evt-1", "destination_calendar_id": "work@example.com"
    })

    assert data["calendar_id"] == "work@example.com"
    assert "work@example.com" in data["message"]


async def test_move_event_requires_a_change(actions):
    with pytest.raises(ToolError, match="at least one of new_start"):
        await call("move_event", {"event_id": "evt-1"})
    actions.move_event.assert_not_called()


async def test_add_attendee_forwards_the_addresses(actions):
    actions.add_attendee.return_value = _event()

    data = await structured("add_attendee", {
        "event_id": "evt-1", "attendee_emails": ["new@example.com"]
    })

    assert actions.add_attendee.call_args.kwargs["attendee_emails"] == ["new@example.com"]
    assert "new@example.com" in data["message"]


async def test_add_attendee_requires_at_least_one_address(actions):
    with pytest.raises(ToolError, match="at least one address"):
        await call("add_attendee", {"event_id": "evt-1", "attendee_emails": []})


async def test_respond_to_event_forwards_the_status(actions):
    actions.respond_to_event.return_value = _event()

    data = await structured("respond_to_event", {
        "event_id": "evt-1", "response_status": "declined", "comment": "Conflict"
    })

    kwargs = actions.respond_to_event.call_args.kwargs
    assert kwargs["response_status"] == "declined"
    assert kwargs["comment"] == "Conflict"
    assert "declined" in data["message"]


async def test_respond_to_event_surfaces_the_validation_error(actions):
    actions.respond_to_event.side_effect = ValueError(
        "response_status must be one of accepted, declined, tentative, needsAction; got 'maybe'."
    )

    with pytest.raises(ToolError, match="must be one of"):
        await call("respond_to_event", {"event_id": "evt-1", "response_status": "maybe"})


async def test_schedule_mutual_books_the_first_free_slot(actions):
    actions.find_mutual_availability_and_schedule.return_value = _event()

    data = await structured("schedule_mutual", {
        "attendee_calendar_ids": ["guest@example.com"],
        "time_min": "2026-01-01T09:00:00Z",
        "time_max": "2026-01-01T17:00:00Z",
        "duration_minutes": 30,
        "summary": "Chat",
        "working_hours_start": "09:00",
        "working_hours_end": "17:00",
    })

    kwargs = actions.find_mutual_availability_and_schedule.call_args.kwargs
    assert kwargs["duration_minutes"] == 30
    assert kwargs["working_hours_start"].hour == 9
    assert kwargs["working_hours_end"].hour == 17
    assert "Booked 'Chat'" in data["message"]


async def test_schedule_mutual_explains_when_no_slot_exists(actions):
    actions.find_mutual_availability_and_schedule.return_value = None

    with pytest.raises(ToolError, match="No 30-minute slot is free"):
        await call("schedule_mutual", {
            "attendee_calendar_ids": ["guest@example.com"],
            "time_min": "2026-01-01T09:00:00Z",
            "time_max": "2026-01-01T17:00:00Z",
            "duration_minutes": 30,
            "summary": "Chat",
        })


async def test_schedule_mutual_rejects_a_window_shorter_than_the_meeting(actions):
    with pytest.raises(ToolError, match="shorter than duration_minutes"):
        await call("schedule_mutual", {
            "attendee_calendar_ids": ["guest@example.com"],
            "time_min": "2026-01-01T09:00:00Z",
            "time_max": "2026-01-01T09:10:00Z",
            "duration_minutes": 30,
            "summary": "Chat",
        })


async def test_schedule_mutual_rejects_a_bad_working_hours_string(actions):
    with pytest.raises(ToolError, match="HH:MM"):
        await call("schedule_mutual", {
            "attendee_calendar_ids": ["guest@example.com"],
            "time_min": "2026-01-01T09:00:00Z",
            "time_max": "2026-01-01T17:00:00Z",
            "duration_minutes": 30,
            "summary": "Chat",
            "working_hours_start": "nine o'clock",
        })


# --- delete_event and its elicitation -----------------------------------------


async def test_delete_event_without_a_client_context_deletes(actions):
    actions.delete_event.return_value = True

    data = await structured("delete_event", {"event_id": "evt-1"})

    assert data["deleted"] is True
    # No client to ask, so no user confirmation was recorded.
    assert data["confirmed_by_user"] is None
    assert actions.delete_event.call_args.kwargs["event_id"] == "evt-1"


async def test_delete_event_requires_an_event_id(actions):
    with pytest.raises(ToolError, match="event_id is required"):
        await call("delete_event", {"event_id": ""})


def _elicit_ctx(action, confirm=None):
    """A minimal Context stand-in whose elicit() returns a canned answer."""
    ctx = MagicMock(name="ctx")
    ctx.request_context.lifespan_context = None

    async def elicit(_message, _schema):
        return SimpleNamespace(
            action=action,
            data=SimpleNamespace(confirm=confirm) if confirm is not None else None,
        )

    ctx.elicit = elicit
    return ctx


async def test_delete_event_proceeds_when_the_user_confirms(actions):
    actions.delete_event.return_value = True

    result = await server_module.delete_event(
        event_id="evt-1", ctx=_elicit_ctx("accept", confirm=True)
    )

    assert result.deleted is True
    assert result.confirmed_by_user is True
    actions.delete_event.assert_called_once()


async def test_delete_event_declined_confirmation_deletes_nothing(actions):
    result = await server_module.delete_event(
        event_id="evt-1", ctx=_elicit_ctx("accept", confirm=False)
    )

    assert result.deleted is False
    assert result.confirmed_by_user is False
    assert "cancelled" in result.message.lower()
    actions.delete_event.assert_not_called()


async def test_delete_event_cancelled_elicitation_deletes_nothing(actions):
    result = await server_module.delete_event(event_id="evt-1", ctx=_elicit_ctx("cancel"))

    assert result.deleted is False
    actions.delete_event.assert_not_called()


async def test_delete_event_proceeds_when_the_client_cannot_elicit(actions):
    actions.delete_event.return_value = True
    ctx = MagicMock(name="ctx")
    ctx.request_context.lifespan_context = None

    async def elicit(*_args, **_kwargs):
        raise RuntimeError("client does not support elicitation")

    ctx.elicit = elicit

    result = await server_module.delete_event(event_id="evt-1", ctx=ctx)

    assert result.deleted is True
    assert result.confirmed_by_user is None
    actions.delete_event.assert_called_once()


# --- helpers ------------------------------------------------------------------


def test_http_error_message_includes_status_detail_and_hint():
    message = server_module._http_error_message(_http_error(401, "Invalid Credentials"))
    assert "401" in message
    assert "Invalid Credentials" in message
    assert "calendar-mcp auth" in message


def test_http_error_message_hints_at_ids_on_404():
    message = server_module._http_error_message(_http_error(404, "Not Found"))
    assert "calendar_id" in message and "event_id" in message


def test_parse_datetime_returns_none_for_empty_values():
    assert server_module.parse_datetime(None, "time_min") is None
    assert server_module.parse_datetime("", "time_min") is None


def test_parse_datetime_keeps_an_explicit_offset():
    parsed = server_module.parse_datetime("2026-01-01T09:00:00-05:00", "time_min")
    assert parsed == datetime(2026, 1, 1, 14, tzinfo=UTC)


def test_parse_datetime_makes_naive_values_aware():
    parsed = server_module.parse_datetime("2026-01-01T09:00:00", "time_min")
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_clock_accepts_hh_mm():
    assert server_module._parse_clock("09:30", "working_hours_start").minute == 30
    assert server_module._parse_clock(None, "working_hours_start") is None


def test_provider_falls_back_to_the_global_provider():
    assert server_module._provider(None) is server_module.credential_provider
