"""Unit tests for src/analysis.py.

No network and no Google credentials: ``calendar_actions.find_events`` is
patched with a stub returning hand-made responses.

Note: ``analysis.py`` accepts both the raw ISO strings returned by the API and
the ``datetime``/``date`` objects the Pydantic models in ``src/models.py``
coerce them to. The raw-string form is used for the logic tests here; the
model-backed form is covered at the bottom of this module.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import analysis
from src.models import EventsResponse

UTC = timezone.utc
CREDS = MagicMock(name="credentials")


def _dt(**kwargs):
    """An EventDateTime-shaped object holding *raw strings*, as the API returns."""
    return SimpleNamespace(dateTime=kwargs.get("dateTime"), date=kwargs.get("date"), timeZone=None)


def _event(id, summary, start, end, recurrence=None):
    return SimpleNamespace(id=id, summary=summary, start=start, end=end, recurrence=recurrence)


def _stub_find_events(items):
    """A find_events replacement accepting any kwargs and returning *items*."""
    return MagicMock(return_value=SimpleNamespace(items=list(items)))


# --- project_recurring_events -------------------------------------------------

def _daily_master():
    return _event(
        "evt-daily", "Standup",
        _dt(dateTime="2026-01-01T09:00:00+00:00"),
        _dt(dateTime="2026-01-01T09:15:00+00:00"),
        recurrence=["RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20260103T090000Z"],
    )


def _weekly_master():
    return _event(
        "evt-weekly", "Retro",
        _dt(dateTime="2026-01-01T14:00:00+00:00"),
        _dt(dateTime="2026-01-01T15:00:00+00:00"),
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=3"],
    )


def _non_recurring():
    return _event(
        "evt-single", "One off",
        _dt(dateTime="2026-01-02T11:00:00+00:00"),
        _dt(dateTime="2026-01-02T12:00:00+00:00"),
    )


def _project(items, **kwargs):
    with patch.object(analysis.calendar_actions, "find_events", _stub_find_events(items)):
        return analysis.project_recurring_events(
            credentials=CREDS,
            time_min=kwargs.pop("time_min", datetime(2026, 1, 1, tzinfo=UTC)),
            time_max=kwargs.pop("time_max", datetime(2026, 1, 10, tzinfo=UTC)),
            **kwargs,
        )


def test_project_recurring_daily_rrule_honours_exdate():
    occurrences = _project([_daily_master(), _non_recurring()])

    # DAILY;COUNT=5 from Jan 1, minus the Jan 3 EXDATE. The non-recurring
    # event is ignored entirely.
    assert [o.occurrence_start for o in occurrences] == [
        datetime(2026, 1, 1, 9, tzinfo=UTC),
        datetime(2026, 1, 2, 9, tzinfo=UTC),
        datetime(2026, 1, 4, 9, tzinfo=UTC),
        datetime(2026, 1, 5, 9, tzinfo=UTC),
    ]
    assert {o.original_event_id for o in occurrences} == {"evt-daily"}
    assert {o.original_summary for o in occurrences} == {"Standup"}
    # The master event's duration is preserved on every occurrence.
    assert occurrences[0].occurrence_end == datetime(2026, 1, 1, 9, 15, tzinfo=UTC)


def test_project_recurring_weekly_rrule_clipped_to_window():
    occurrences = _project([_weekly_master()], time_max=datetime(2026, 1, 12, tzinfo=UTC))
    assert [o.occurrence_start for o in occurrences] == [
        datetime(2026, 1, 1, 14, tzinfo=UTC),
        datetime(2026, 1, 8, 14, tzinfo=UTC),
    ]


def test_project_recurring_all_day_event():
    all_day = _event(
        "evt-allday", "Sprint day",
        _dt(date="2026-01-01"), _dt(date="2026-01-02"),
        recurrence=["RRULE:FREQ=DAILY;COUNT=3"],
    )
    occurrences = _project([all_day])
    # All-day occurrences start at midnight, adopt the window's timezone and
    # last a whole day.
    assert [o.occurrence_start for o in occurrences] == [
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        datetime(2026, 1, 3, tzinfo=UTC),
    ]
    assert occurrences[0].occurrence_end == datetime(2026, 1, 2, tzinfo=UTC)


def test_project_recurring_returns_empty_when_no_events():
    assert _project([]) == []


def test_project_recurring_skips_event_without_rrule():
    bad = _event(
        "evt-norule", "No rule",
        _dt(dateTime="2026-01-01T09:00:00+00:00"),
        _dt(dateTime="2026-01-01T10:00:00+00:00"),
        recurrence=["EXDATE:20260103T090000Z"],
    )
    assert _project([bad]) == []


# --- analyze_busyness ---------------------------------------------------------

def _analyze(items, **kwargs):
    with patch.object(analysis.calendar_actions, "find_events", _stub_find_events(items)):
        return analysis.analyze_busyness(
            credentials=CREDS,
            time_min=kwargs.pop("time_min", datetime(2026, 1, 1, tzinfo=UTC)),
            time_max=kwargs.pop("time_max", datetime(2026, 1, 10, tzinfo=UTC)),
            **kwargs,
        )


TIMED = _event("e1", "Long meeting",
               _dt(dateTime="2026-01-01T09:00:00+00:00"), _dt(dateTime="2026-01-01T10:30:00+00:00"))
TIMED_2 = _event("e2", "Short sync",
                 _dt(dateTime="2026-01-01T13:00:00+00:00"), _dt(dateTime="2026-01-01T13:30:00+00:00"))
OUTSIDE = _event("e3", "Way later",
                 _dt(dateTime="2026-02-01T09:00:00+00:00"), _dt(dateTime="2026-02-01T10:00:00+00:00"))
ALL_DAY = _event("e4", "Holiday", _dt(date="2026-01-02"), _dt(date="2026-01-03"))
MULTI_DAY = _event("e5", "Conference", _dt(date="2026-01-04"), _dt(date="2026-01-06"))


def test_analyze_busyness_no_events():
    assert _analyze([]) == {}


def test_analyze_busyness_counts_and_sums_timed_events():
    assert _analyze([TIMED, TIMED_2, OUTSIDE]) == {
        date(2026, 1, 1): {"event_count": 2, "total_duration_minutes": 120.0},
    }


def test_analyze_busyness_all_day_and_multi_day_events():
    result = _analyze([TIMED, ALL_DAY, MULTI_DAY])
    # All-day and multi-day events are counted on their start date only and
    # contribute no duration (they have no dateTime).
    assert result == {
        date(2026, 1, 1): {"event_count": 1, "total_duration_minutes": 90.0},
        date(2026, 1, 2): {"event_count": 1, "total_duration_minutes": 0.0},
        date(2026, 1, 4): {"event_count": 1, "total_duration_minutes": 0.0},
    }
    # Result is sorted chronologically by date.
    assert list(result) == sorted(result)


def test_analyze_busyness_is_keyed_by_date_objects():
    assert all(isinstance(k, date) for k in _analyze([TIMED]))


# --- Pydantic-model inputs and real find_events signature ---------------------------------------------------------------

MODEL_EVENT = {
    "id": "e1",
    "summary": "Long meeting",
    "start": {"dateTime": "2026-01-01T09:00:00+00:00"},
    "end": {"dateTime": "2026-01-01T10:30:00+00:00"},
}


def test_analysis_accepts_pydantic_event_models():
    response = EventsResponse(items=[MODEL_EVENT])
    with patch.object(analysis.calendar_actions, "find_events", MagicMock(return_value=response)):
        result = analysis.analyze_busyness(
            credentials=CREDS,
            time_min=datetime(2026, 1, 1, tzinfo=UTC),
            time_max=datetime(2026, 1, 10, tzinfo=UTC),
        )
    assert result == {date(2026, 1, 1): {"event_count": 1, "total_duration_minutes": 90.0}}


def test_project_recurring_matches_real_find_events_signature():
    with patch.object(analysis.calendar_actions, "find_events", autospec=True) as mocked:
        mocked.return_value = EventsResponse(items=[])
        analysis.project_recurring_events(
            credentials=CREDS,
            time_min=datetime(2026, 1, 1, tzinfo=UTC),
            time_max=datetime(2026, 1, 10, tzinfo=UTC),
            event_query="Standup",
        )
