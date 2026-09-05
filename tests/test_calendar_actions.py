"""Tests for calendar_mcp/calendar_actions.py.

Pure helpers are exercised directly; the API-calling functions run against a
fake googleapiclient service (a MagicMock mimicking the
``service.events().list().execute()`` chain) with ``_get_calendar_service``
patched, so no network and no credentials are needed.
"""
import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from calendar_mcp import calendar_actions
from calendar_mcp.models import EventCreateRequest, EventDateTime, EventUpdateRequest

UTC = timezone.utc
CREDS = MagicMock(name="credentials")


def _iv(start_hour, end_hour, day=1):
    return {
        "start": datetime(2026, 1, day, start_hour, tzinfo=UTC),
        "end": datetime(2026, 1, day, end_hour, tzinfo=UTC),
    }


def _http_error(status, message="Not Found"):
    """A googleapiclient HttpError shaped like the real thing."""
    payload = json.dumps({"error": {"code": status, "message": message}}).encode("utf-8")
    return HttpError(SimpleNamespace(status=status, reason=message), payload)


# --- _merge_intervals ---------------------------------------------------------

def test_merge_intervals_empty():
    assert calendar_actions._merge_intervals([]) == []


def test_merge_intervals_disjoint_intervals_are_sorted_not_merged():
    merged = calendar_actions._merge_intervals([_iv(14, 15), _iv(9, 10)])
    assert merged == [_iv(9, 10), _iv(14, 15)]


def test_merge_intervals_overlapping_and_adjacent():
    merged = calendar_actions._merge_intervals([_iv(9, 11), _iv(10, 12), _iv(12, 13), _iv(15, 16)])
    # 9-11 and 10-12 overlap, 12-13 is adjacent -> single 9-13 block.
    assert merged == [_iv(9, 13), _iv(15, 16)]


def test_merge_intervals_fully_contained_interval():
    assert calendar_actions._merge_intervals([_iv(9, 17), _iv(11, 12)]) == [_iv(9, 17)]


# --- _find_first_available_slot ----------------------------------------------
# The implementation clamps the search to "now", so these tests use a window
# comfortably in the future relative to any run date.

FUTURE = datetime.now(UTC) + timedelta(days=30)
DAY_START = FUTURE.replace(hour=8, minute=0, second=0, microsecond=0)


def _slot(hours_from_start, minutes=0):
    return DAY_START + timedelta(hours=hours_from_start, minutes=minutes)


def test_find_first_available_slot_no_busy_returns_window_start():
    result = calendar_actions._find_first_available_slot(
        time_min=DAY_START,
        time_max=DAY_START + timedelta(hours=8),
        duration=timedelta(minutes=30),
        busy_intervals=[],
    )
    assert result == (DAY_START, DAY_START + timedelta(minutes=30))


def test_find_first_available_slot_skips_busy_intervals():
    busy = [
        {"start": DAY_START, "end": _slot(2)},
        {"start": _slot(2), "end": _slot(3)},
    ]
    result = calendar_actions._find_first_available_slot(
        time_min=DAY_START,
        time_max=DAY_START + timedelta(hours=8),
        duration=timedelta(hours=1),
        busy_intervals=busy,
    )
    assert result == (_slot(3), _slot(4))


def test_find_first_available_slot_returns_none_when_fully_booked():
    busy = [{"start": DAY_START, "end": DAY_START + timedelta(hours=8)}]
    assert calendar_actions._find_first_available_slot(
        time_min=DAY_START,
        time_max=DAY_START + timedelta(hours=8),
        duration=timedelta(minutes=30),
        busy_intervals=busy,
    ) is None


def test_find_first_available_slot_returns_none_when_duration_exceeds_window():
    assert calendar_actions._find_first_available_slot(
        time_min=DAY_START,
        time_max=DAY_START + timedelta(minutes=20),
        duration=timedelta(hours=1),
        busy_intervals=[],
    ) is None


def test_find_first_available_slot_respects_working_hours():
    # Window starts at 08:00 UTC but working hours only open at 09:00 UTC.
    result = calendar_actions._find_first_available_slot(
        time_min=DAY_START,
        time_max=DAY_START + timedelta(hours=10),
        duration=timedelta(hours=1),
        busy_intervals=[],
        working_hours_start=time(9, 0),
        working_hours_end=time(17, 0),
    )
    assert result is not None
    slot_start, slot_end = result
    assert time(9, 0) <= slot_start.time()
    assert slot_end.time() <= time(17, 0)


# --- _event_duration ----------------------------------------------------------

def test_event_duration_of_timed_event():
    raw = {
        "start": {"dateTime": "2026-01-01T09:00:00+00:00"},
        "end": {"dateTime": "2026-01-01T10:30:00+00:00"},
    }
    assert calendar_actions._event_duration(raw) == timedelta(minutes=90)


def test_event_duration_is_none_for_all_day_event():
    raw = {"start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}}
    assert calendar_actions._event_duration(raw) is None


def test_event_duration_is_none_for_unparseable_times():
    raw = {"start": {"dateTime": "not-a-time"}, "end": {"dateTime": "also-not"}}
    assert calendar_actions._event_duration(raw) is None


# --- fake service -------------------------------------------------------------

EVENT_PAYLOAD = {
    "id": "abc123",
    "summary": "Team sync",
    "start": {"dateTime": "2026-01-01T09:00:00+00:00"},
    "end": {"dateTime": "2026-01-01T10:00:00+00:00"},
}

EVENTS_PAYLOAD = {
    "kind": "calendar#events",
    "summary": "Test calendar",
    "timeZone": "Europe/Berlin",
    "items": [EVENT_PAYLOAD],
}


@pytest.fixture
def fake_service():
    """A MagicMock mimicking googleapiclient's fluent service object."""
    service = MagicMock(name="service")
    with patch.object(calendar_actions, "_get_calendar_service", return_value=service):
        yield service


def _events(fake_service):
    """The ``service.events()`` mock (the same object on every call)."""
    return fake_service.events.return_value


# --- find_events --------------------------------------------------------------

def test_find_events_returns_parsed_response(fake_service):
    _events(fake_service).list.return_value.execute.return_value = EVENTS_PAYLOAD

    response = calendar_actions.find_events(CREDS, calendar_id="primary")

    assert response is not None
    assert response.timeZone == "Europe/Berlin"
    assert [e.id for e in response.items] == ["abc123"]


def test_find_events_passes_expected_list_parameters(fake_service):
    _events(fake_service).list.return_value.execute.return_value = EVENTS_PAYLOAD

    calendar_actions.find_events(
        CREDS,
        calendar_id="work@example.com",
        time_min=datetime(2026, 1, 1, 9, tzinfo=UTC),
        time_max=datetime(2026, 1, 2, 9, tzinfo=UTC),
        query="sync",
        max_results=10,
    )

    kwargs = _events(fake_service).list.call_args.kwargs
    assert kwargs["calendarId"] == "work@example.com"
    assert kwargs["timeMin"] == "2026-01-01T09:00:00+00:00"
    assert kwargs["timeMax"] == "2026-01-02T09:00:00+00:00"
    assert kwargs["q"] == "sync"
    assert kwargs["maxResults"] == 10
    assert kwargs["singleEvents"] is True
    assert kwargs["orderBy"] == "startTime"


def test_find_events_omits_unset_optional_parameters(fake_service):
    _events(fake_service).list.return_value.execute.return_value = EVENTS_PAYLOAD

    calendar_actions.find_events(CREDS)

    kwargs = _events(fake_service).list.call_args.kwargs
    for absent in ("timeMin", "timeMax", "q", "iCalUID", "eventTypes"):
        assert absent not in kwargs


def test_find_events_appends_z_to_naive_datetimes(fake_service):
    _events(fake_service).list.return_value.execute.return_value = EVENTS_PAYLOAD

    calendar_actions.find_events(CREDS, time_min=datetime(2026, 1, 1, 9))

    assert _events(fake_service).list.call_args.kwargs["timeMin"] == "2026-01-01T09:00:00Z"


def test_find_events_reraises_http_error(fake_service):
    _events(fake_service).list.return_value.execute.side_effect = _http_error(403, "Forbidden")

    with pytest.raises(HttpError):
        calendar_actions.find_events(CREDS)


def test_find_events_returns_none_on_unexpected_error(fake_service):
    _events(fake_service).list.return_value.execute.side_effect = RuntimeError("boom")

    assert calendar_actions.find_events(CREDS) is None


# --- create_event -------------------------------------------------------------

def test_create_event_builds_body_and_parses_response(fake_service):
    _events(fake_service).insert.return_value.execute.return_value = EVENT_PAYLOAD

    request = EventCreateRequest(
        summary="Team sync",
        start=EventDateTime(dateTime=datetime(2026, 1, 1, 9, tzinfo=UTC)),
        end=EventDateTime(dateTime=datetime(2026, 1, 1, 10, tzinfo=UTC)),
        description="Weekly",
        location="Room 1",
        attendees=["a@example.com", "b@example.com"],
    )
    created = calendar_actions.create_event(
        CREDS, event_data=request, calendar_id="primary", send_notifications=False
    )

    assert created is not None and created.id == "abc123"
    kwargs = _events(fake_service).insert.call_args.kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["sendNotifications"] is False
    assert kwargs["body"] == {
        "start": {"dateTime": "2026-01-01T09:00:00+00:00"},
        "end": {"dateTime": "2026-01-01T10:00:00+00:00"},
        "summary": "Team sync",
        "description": "Weekly",
        "location": "Room 1",
        "attendees": [{"email": "a@example.com"}, {"email": "b@example.com"}],
    }


def test_create_event_appends_z_to_naive_times(fake_service):
    _events(fake_service).insert.return_value.execute.return_value = EVENT_PAYLOAD

    request = EventCreateRequest(
        summary="Naive",
        start=EventDateTime(dateTime=datetime(2026, 1, 1, 9)),
        end=EventDateTime(dateTime=datetime(2026, 1, 1, 10)),
    )
    calendar_actions.create_event(CREDS, event_data=request)

    body = _events(fake_service).insert.call_args.kwargs["body"]
    assert body["start"]["dateTime"] == "2026-01-01T09:00:00Z"
    assert body["end"]["dateTime"] == "2026-01-01T10:00:00Z"


def test_create_event_supports_all_day_dates(fake_service):
    all_day = {"id": "d1", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}}
    _events(fake_service).insert.return_value.execute.return_value = all_day

    request = EventCreateRequest(
        summary="Holiday",
        start=EventDateTime(date="2026-01-01"),
        end=EventDateTime(date="2026-01-02"),
    )
    calendar_actions.create_event(CREDS, event_data=request)

    body = _events(fake_service).insert.call_args.kwargs["body"]
    assert body["start"] == {"date": "2026-01-01"}
    assert body["end"] == {"date": "2026-01-02"}


def test_create_event_keeps_explicit_timezone(fake_service):
    _events(fake_service).insert.return_value.execute.return_value = EVENT_PAYLOAD

    request = EventCreateRequest(
        summary="Zoned",
        start=EventDateTime(dateTime=datetime(2026, 1, 1, 9, tzinfo=UTC), timeZone="Europe/Berlin"),
        end=EventDateTime(dateTime=datetime(2026, 1, 1, 10, tzinfo=UTC), timeZone="Europe/Berlin"),
    )
    calendar_actions.create_event(CREDS, event_data=request)

    body = _events(fake_service).insert.call_args.kwargs["body"]
    assert body["start"]["timeZone"] == "Europe/Berlin"


def test_create_event_reraises_http_error(fake_service):
    _events(fake_service).insert.return_value.execute.side_effect = _http_error(400, "Bad Request")

    request = EventCreateRequest(
        summary="Doomed",
        start=EventDateTime(dateTime=datetime(2026, 1, 1, 9, tzinfo=UTC)),
        end=EventDateTime(dateTime=datetime(2026, 1, 1, 10, tzinfo=UTC)),
    )
    with pytest.raises(HttpError):
        calendar_actions.create_event(CREDS, event_data=request)


# --- update_event -------------------------------------------------------------

def test_update_event_patches_only_supplied_fields(fake_service):
    _events(fake_service).patch.return_value.execute.return_value = EVENT_PAYLOAD

    update = EventUpdateRequest(
        summary="Renamed",
        start=EventDateTime(dateTime=datetime(2026, 1, 1, 11, tzinfo=UTC)),
    )
    updated = calendar_actions.update_event(
        CREDS, event_id="abc123", update_data=update, calendar_id="primary"
    )

    assert updated is not None and updated.id == "abc123"
    kwargs = _events(fake_service).patch.call_args.kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["eventId"] == "abc123"
    assert kwargs["sendNotifications"] is True
    assert kwargs["body"] == {
        "summary": "Renamed",
        "start": {"dateTime": "2026-01-01T11:00:00+00:00"},
    }
    # No end was supplied, so the patch must not touch it.
    assert "end" not in kwargs["body"]


def test_update_event_with_nothing_to_change_fetches_the_event(fake_service):
    _events(fake_service).get.return_value.execute.return_value = EVENT_PAYLOAD

    result = calendar_actions.update_event(
        CREDS, event_id="abc123", update_data=EventUpdateRequest()
    )

    assert result is not None and result.id == "abc123"
    _events(fake_service).patch.assert_not_called()


def test_update_event_reraises_http_error(fake_service):
    _events(fake_service).patch.return_value.execute.side_effect = _http_error(404)

    with pytest.raises(HttpError):
        calendar_actions.update_event(
            CREDS, event_id="missing", update_data=EventUpdateRequest(summary="x")
        )


# --- move_event ---------------------------------------------------------------

def test_move_event_preserves_duration_when_only_start_given(fake_service):
    _events(fake_service).get.return_value.execute.return_value = EVENT_PAYLOAD
    _events(fake_service).patch.return_value.execute.return_value = EVENT_PAYLOAD

    calendar_actions.move_event(
        CREDS,
        event_id="abc123",
        new_start=datetime(2026, 1, 1, 15, tzinfo=UTC),
    )

    body = _events(fake_service).patch.call_args.kwargs["body"]
    # The original event ran 09:00-10:00, so the new end is start + 1h.
    assert body["start"]["dateTime"] == "2026-01-01T15:00:00+00:00"
    assert body["end"]["dateTime"] == "2026-01-01T16:00:00+00:00"


def test_move_event_preserves_original_time_zone_hints(fake_service):
    zoned = dict(EVENT_PAYLOAD)
    zoned["start"] = {"dateTime": "2026-01-01T09:00:00+01:00", "timeZone": "Europe/Berlin"}
    zoned["end"] = {"dateTime": "2026-01-01T10:00:00+01:00", "timeZone": "Europe/Berlin"}
    _events(fake_service).get.return_value.execute.return_value = zoned
    _events(fake_service).patch.return_value.execute.return_value = EVENT_PAYLOAD

    calendar_actions.move_event(
        CREDS, event_id="abc123", new_start=datetime(2026, 1, 2, 9, tzinfo=UTC)
    )

    body = _events(fake_service).patch.call_args.kwargs["body"]
    assert body["start"]["timeZone"] == "Europe/Berlin"
    assert body["end"]["timeZone"] == "Europe/Berlin"


def test_move_event_uses_both_new_times_when_given(fake_service):
    _events(fake_service).get.return_value.execute.return_value = EVENT_PAYLOAD
    _events(fake_service).patch.return_value.execute.return_value = EVENT_PAYLOAD

    calendar_actions.move_event(
        CREDS,
        event_id="abc123",
        new_start=datetime(2026, 1, 3, 9, tzinfo=UTC),
        new_end=datetime(2026, 1, 3, 9, 30, tzinfo=UTC),
    )

    body = _events(fake_service).patch.call_args.kwargs["body"]
    assert body["end"]["dateTime"] == "2026-01-03T09:30:00+00:00"


def test_move_event_across_calendars_calls_move_then_patches_destination(fake_service):
    moved_payload = dict(EVENT_PAYLOAD, id="moved-id")
    _events(fake_service).get.return_value.execute.return_value = EVENT_PAYLOAD
    _events(fake_service).move.return_value.execute.return_value = moved_payload
    _events(fake_service).patch.return_value.execute.return_value = moved_payload

    calendar_actions.move_event(
        CREDS,
        event_id="abc123",
        calendar_id="primary",
        destination_calendar_id="other@example.com",
        new_start=datetime(2026, 1, 4, 9, tzinfo=UTC),
    )

    move_kwargs = _events(fake_service).move.call_args.kwargs
    assert move_kwargs["calendarId"] == "primary"
    assert move_kwargs["eventId"] == "abc123"
    assert move_kwargs["destination"] == "other@example.com"

    # The re-timing patch must target the destination calendar and the new ID.
    patch_kwargs = _events(fake_service).patch.call_args.kwargs
    assert patch_kwargs["calendarId"] == "other@example.com"
    assert patch_kwargs["eventId"] == "moved-id"


def test_move_event_across_calendars_without_new_times_does_not_patch(fake_service):
    _events(fake_service).get.return_value.execute.return_value = EVENT_PAYLOAD
    _events(fake_service).move.return_value.execute.return_value = EVENT_PAYLOAD

    result = calendar_actions.move_event(
        CREDS, event_id="abc123", destination_calendar_id="other@example.com"
    )

    assert result is not None and result.id == "abc123"
    _events(fake_service).patch.assert_not_called()


def test_move_event_requires_a_change():
    with pytest.raises(ValueError):
        calendar_actions.move_event(CREDS, event_id="abc123")


def test_move_event_reraises_http_error(fake_service):
    _events(fake_service).get.return_value.execute.side_effect = _http_error(404)

    with pytest.raises(HttpError):
        calendar_actions.move_event(
            CREDS, event_id="missing", new_start=datetime(2026, 1, 1, 9, tzinfo=UTC)
        )


# --- respond_to_event ---------------------------------------------------------

INVITATION = dict(
    EVENT_PAYLOAD,
    attendees=[
        {"email": "someone@example.com", "responseStatus": "accepted"},
        {"email": "me@example.com", "self": True, "responseStatus": "needsAction"},
    ],
)


def test_respond_to_event_patches_only_the_self_attendee(fake_service):
    _events(fake_service).get.return_value.execute.return_value = json.loads(json.dumps(INVITATION))
    _events(fake_service).patch.return_value.execute.return_value = EVENT_PAYLOAD

    calendar_actions.respond_to_event(
        CREDS, event_id="abc123", response_status="accepted", comment="See you"
    )

    body = _events(fake_service).patch.call_args.kwargs["body"]
    assert body["attendees"][0]["responseStatus"] == "accepted"  # untouched
    assert "comment" not in body["attendees"][0]
    assert body["attendees"][1]["responseStatus"] == "accepted"
    assert body["attendees"][1]["comment"] == "See you"


def test_respond_to_event_matches_attendee_by_calendar_id(fake_service):
    without_self = dict(
        EVENT_PAYLOAD,
        attendees=[{"email": "Work@Example.com", "responseStatus": "needsAction"}],
    )
    _events(fake_service).get.return_value.execute.return_value = without_self
    _events(fake_service).patch.return_value.execute.return_value = EVENT_PAYLOAD

    calendar_actions.respond_to_event(
        CREDS, event_id="abc123", response_status="declined", calendar_id="work@example.com"
    )

    body = _events(fake_service).patch.call_args.kwargs["body"]
    assert body["attendees"][0]["responseStatus"] == "declined"


def test_respond_to_event_rejects_unknown_status():
    with pytest.raises(ValueError):
        calendar_actions.respond_to_event(CREDS, event_id="abc123", response_status="maybe")


def test_respond_to_event_rejects_event_the_user_is_not_invited_to(fake_service):
    _events(fake_service).get.return_value.execute.return_value = EVENT_PAYLOAD

    with pytest.raises(ValueError):
        calendar_actions.respond_to_event(
            CREDS, event_id="abc123", response_status="accepted"
        )


def test_respond_to_event_reraises_http_error(fake_service):
    _events(fake_service).get.return_value.execute.side_effect = _http_error(404)

    with pytest.raises(HttpError):
        calendar_actions.respond_to_event(
            CREDS, event_id="missing", response_status="accepted"
        )


# --- get_event / get_calendar_timezone ----------------------------------------

def test_get_event_returns_parsed_event(fake_service):
    _events(fake_service).get.return_value.execute.return_value = EVENT_PAYLOAD

    event = calendar_actions.get_event(CREDS, event_id="abc123", calendar_id="primary")

    assert event is not None and event.summary == "Team sync"
    assert _events(fake_service).get.call_args.kwargs == {
        "calendarId": "primary",
        "eventId": "abc123",
    }


def test_get_event_reraises_http_error(fake_service):
    _events(fake_service).get.return_value.execute.side_effect = _http_error(404)

    with pytest.raises(HttpError):
        calendar_actions.get_event(CREDS, event_id="missing")


def test_get_calendar_timezone_reads_the_calendar_settings(fake_service):
    fake_service.calendars.return_value.get.return_value.execute.return_value = {
        "id": "primary",
        "timeZone": "Europe/Berlin",
    }

    assert calendar_actions.get_calendar_timezone(CREDS, "primary") == "Europe/Berlin"


def test_get_calendar_timezone_returns_none_on_error(fake_service):
    fake_service.calendars.return_value.get.return_value.execute.side_effect = _http_error(403)

    # This one deliberately swallows errors so callers can fall back.
    assert calendar_actions.get_calendar_timezone(CREDS, "primary") is None


# --- find_availability --------------------------------------------------------

def test_find_availability_parses_busy_intervals(fake_service):
    fake_service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "a@example.com": {
                "busy": [{"start": "2026-01-01T09:00:00Z", "end": "2026-01-01T10:00:00Z"}],
            },
            "b@example.com": {"busy": [], "errors": [{"reason": "notFound"}]},
        }
    }

    result = calendar_actions.find_availability(
        CREDS,
        time_min=datetime(2026, 1, 1, tzinfo=UTC),
        time_max=datetime(2026, 1, 2, tzinfo=UTC),
        calendar_ids=["a@example.com", "b@example.com"],
    )

    assert result["a@example.com"]["busy"] == [
        {
            "start": datetime(2026, 1, 1, 9, tzinfo=UTC),
            "end": datetime(2026, 1, 1, 10, tzinfo=UTC),
        }
    ]
    assert result["b@example.com"]["errors"] == [{"reason": "notFound"}]

    body = fake_service.freebusy.return_value.query.call_args.kwargs["body"]
    assert body["items"] == [{"id": "a@example.com"}, {"id": "b@example.com"}]


def test_find_availability_with_no_calendars_short_circuits(fake_service):
    assert calendar_actions.find_availability(
        CREDS,
        time_min=datetime(2026, 1, 1, tzinfo=UTC),
        time_max=datetime(2026, 1, 2, tzinfo=UTC),
        calendar_ids=[],
    ) == {}
    fake_service.freebusy.assert_not_called()
