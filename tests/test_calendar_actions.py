"""Tests for src/calendar_actions.py.

Pure helpers are exercised directly; the API-calling functions run against a
fake googleapiclient service (a MagicMock mimicking the
``service.events().list().execute()`` chain) with ``_get_calendar_service``
patched, so no network and no credentials are needed.
"""
from datetime import datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src import calendar_actions
from src.models import EventCreateRequest, EventDateTime

UTC = timezone.utc
CREDS = MagicMock(name="credentials")


def _iv(start_hour, end_hour, day=1):
    return {
        "start": datetime(2026, 1, day, start_hour, tzinfo=UTC),
        "end": datetime(2026, 1, day, end_hour, tzinfo=UTC),
    }


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


# --- find_events --------------------------------------------------------------

EVENTS_PAYLOAD = {
    "kind": "calendar#events",
    "summary": "Test calendar",
    "items": [
        {
            "id": "abc123",
            "summary": "Team sync",
            "start": {"dateTime": "2026-01-01T09:00:00+00:00"},
            "end": {"dateTime": "2026-01-01T10:00:00+00:00"},
        }
    ],
}


@pytest.fixture
def fake_service():
    """A MagicMock mimicking googleapiclient's fluent service object."""
    service = MagicMock(name="service")
    with patch.object(calendar_actions, "_get_calendar_service", return_value=service) as getter:
        service.get_calendar_service = getter
        yield service


def test_find_events_returns_parsed_response(fake_service):
    fake_service.events.return_value.list.return_value.execute.return_value = EVENTS_PAYLOAD

    result = calendar_actions.find_events(
        credentials=CREDS,
        calendar_id="primary",
        time_min=datetime(2026, 1, 1, tzinfo=UTC),
        time_max=datetime(2026, 1, 2, tzinfo=UTC),
        query="sync",
        max_results=10,
    )

    assert result is not None
    assert len(result.items) == 1
    assert result.items[0].id == "abc123"
    assert result.items[0].summary == "Team sync"

    kwargs = fake_service.events.return_value.list.call_args.kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["q"] == "sync"
    assert kwargs["maxResults"] == 10
    assert kwargs["singleEvents"] is True
    assert kwargs["timeMin"] == "2026-01-01T00:00:00+00:00"
    assert kwargs["timeMax"] == "2026-01-02T00:00:00+00:00"


def test_find_events_omits_unset_optional_parameters(fake_service):
    fake_service.events.return_value.list.return_value.execute.return_value = {"items": []}

    calendar_actions.find_events(credentials=CREDS)

    kwargs = fake_service.events.return_value.list.call_args.kwargs
    assert "timeMin" not in kwargs
    assert "timeMax" not in kwargs
    assert "q" not in kwargs
    assert "iCalUID" not in kwargs


def test_find_events_appends_z_to_naive_datetimes(fake_service):
    fake_service.events.return_value.list.return_value.execute.return_value = {"items": []}

    calendar_actions.find_events(credentials=CREDS, time_min=datetime(2026, 1, 1, 9, 0))

    assert fake_service.events.return_value.list.call_args.kwargs["timeMin"] == "2026-01-01T09:00:00Z"


def test_find_events_returns_none_on_api_error(fake_service):
    fake_service.events.return_value.list.return_value.execute.side_effect = RuntimeError("boom")
    assert calendar_actions.find_events(credentials=CREDS) is None


# --- create_event -------------------------------------------------------------

CREATED_PAYLOAD = {
    "id": "new-event-1",
    "summary": "Design review",
    "start": {"dateTime": "2026-03-02T15:00:00+00:00"},
    "end": {"dateTime": "2026-03-02T16:00:00+00:00"},
}


def _event_request(**overrides):
    data = dict(
        summary="Design review",
        start=EventDateTime(dateTime=datetime(2026, 3, 2, 15, 0, tzinfo=UTC)),
        end=EventDateTime(dateTime=datetime(2026, 3, 2, 16, 0, tzinfo=UTC)),
        description="Quarterly design review",
        location="Room 2",
    )
    data.update(overrides)
    return EventCreateRequest(**data)


def test_create_event_builds_body_and_parses_response(fake_service):
    fake_service.events.return_value.insert.return_value.execute.return_value = CREATED_PAYLOAD

    created = calendar_actions.create_event(
        credentials=CREDS,
        event_data=_event_request(attendees=["a@example.com", "b@example.com"]),
        calendar_id="primary",
        send_notifications=False,
    )

    assert created is not None
    assert created.id == "new-event-1"
    assert created.summary == "Design review"

    kwargs = fake_service.events.return_value.insert.call_args.kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["sendNotifications"] is False
    body = kwargs["body"]
    assert body["summary"] == "Design review"
    assert body["description"] == "Quarterly design review"
    assert body["location"] == "Room 2"
    assert body["start"] == {"dateTime": "2026-03-02T15:00:00+00:00"}
    assert body["end"] == {"dateTime": "2026-03-02T16:00:00+00:00"}
    assert body["attendees"] == [{"email": "a@example.com"}, {"email": "b@example.com"}]


def test_create_event_supports_all_day_dates(fake_service):
    fake_service.events.return_value.insert.return_value.execute.return_value = CREATED_PAYLOAD

    calendar_actions.create_event(
        credentials=CREDS,
        event_data=_event_request(
            start=EventDateTime(date="2026-03-02"),
            end=EventDateTime(date="2026-03-03"),
        ),
    )

    body = fake_service.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"] == {"date": "2026-03-02"}
    assert body["end"] == {"date": "2026-03-03"}


def test_create_event_returns_none_on_api_error(fake_service):
    fake_service.events.return_value.insert.return_value.execute.side_effect = RuntimeError("boom")
    assert calendar_actions.create_event(credentials=CREDS, event_data=_event_request()) is None
