"""Tests for the scheduling logic and the tools built on it.

Two layers, deliberately separate:

* :mod:`calendar_mcp.scheduling` is pure, so its tests are hand-made intervals
  and plain assertions -- no server, no mocks, no clock.
* The tools (``find_focus_time``, ``block_focus_time``, ``detect_conflicts``,
  ``suggest_reschedule``) are driven through the real ``MCPServer``, with
  ``calendar_actions`` patched, exactly as ``tests/test_server.py`` does it.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from calendar_mcp import scheduling
from calendar_mcp import server as server_module
from calendar_mcp.models import (
    CalendarListEntry,
    CalendarListResponse,
    EventsResponse,
    GoogleCalendarEvent,
)

UTC = timezone.utc


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC moment in January 2026. The 1st is a Thursday, the 3rd a Saturday."""
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def span(day: int, start_hour: float, end_hour: float):
    """A ``(start, end)`` interval on one January 2026 day, in decimal hours."""
    def moment(value: float) -> datetime:
        return dt(day, int(value), int(round((value - int(value)) * 60)))

    return moment(start_hour), moment(end_hour)


WORKDAY = span(1, 9, 17)


# ==========================================================================
# Pure logic: measuring
# ==========================================================================


def test_interval_minutes_ignores_inverted_intervals():
    assert scheduling.interval_minutes(span(1, 9, 10.5)) == 90.0
    assert scheduling.interval_minutes((dt(1, 12), dt(1, 9))) == 0.0


def test_total_hours_merges_overlaps_rather_than_double_counting():
    overlapping = [span(1, 9, 11), span(1, 10, 12)]
    assert scheduling.total_hours(overlapping) == 3.0
    assert scheduling.total_hours([span(1, 9, 10), span(1, 11, 12)]) == 2.0


def test_overlap_minutes_treats_touching_intervals_as_separate():
    assert scheduling.overlap_minutes(span(1, 9, 10), span(1, 10, 11)) == 0.0
    assert scheduling.overlap_minutes(span(1, 9, 11), span(1, 10, 12)) == 60.0


# ==========================================================================
# Pure logic: focus blocks
# ==========================================================================


def test_candidate_blocks_cuts_busy_time_out_of_the_working_day():
    blocks = scheduling.candidate_blocks([WORKDAY], [span(1, 11, 12)], min_block_minutes=60)

    assert blocks == [span(1, 9, 11), span(1, 12, 17)]


def test_candidate_blocks_leaves_the_buffer_on_both_sides_of_a_meeting():
    blocks = scheduling.candidate_blocks(
        [WORKDAY], [span(1, 11, 12)], min_block_minutes=60, buffer_minutes=15
    )

    assert blocks == [span(1, 9, 10.75), span(1, 12.25, 17)]


def test_candidate_blocks_drops_stretches_shorter_than_the_minimum():
    blocks = scheduling.candidate_blocks(
        [WORKDAY], [span(1, 10, 16)], min_block_minutes=90
    )

    # 09:00-10:00 is only an hour, so only the... nothing survives: 16:00-17:00
    # is an hour too.
    assert blocks == []


def test_candidate_blocks_keeps_each_working_window_separate():
    morning, afternoon = span(1, 9, 12), span(1, 13, 17)  # lunch already removed

    blocks = scheduling.candidate_blocks([morning, afternoon], [], min_block_minutes=60)

    assert blocks == [morning, afternoon]


def test_rank_blocks_puts_the_longest_first_then_the_earliest():
    short_early, long_late, equal_later = span(1, 9, 11), span(1, 12, 17), span(2, 9, 11)

    assert scheduling.rank_blocks([short_early, long_late, equal_later]) == [
        long_late,
        short_early,
        equal_later,
    ]


def test_select_blocks_trims_the_last_block_to_what_is_still_needed():
    blocks = [span(1, 9, 11), span(1, 12, 17)]

    chosen = scheduling.select_blocks(blocks, hours_needed=6, min_block_minutes=60)

    # The five-hour block first, then one hour of the two-hour one, in date order.
    assert chosen == [span(1, 9, 10), span(1, 12, 17)]
    assert scheduling.total_hours(chosen) == 6.0


def test_select_blocks_never_trims_below_the_minimum_block():
    chosen = scheduling.select_blocks([span(1, 9, 17)], hours_needed=0.25, min_block_minutes=60)

    assert chosen == [span(1, 9, 10)]


def test_select_blocks_takes_what_it_can_when_the_week_is_too_full():
    chosen = scheduling.select_blocks([span(1, 9, 11)], hours_needed=8, min_block_minutes=60)

    assert chosen == [span(1, 9, 11)]
    assert scheduling.total_hours(chosen) == 2.0


def test_select_blocks_selects_nothing_when_nothing_is_needed():
    assert scheduling.select_blocks([span(1, 9, 17)], hours_needed=0) == []


# ==========================================================================
# Pure logic: conflicts
# ==========================================================================


def test_overlapping_pairs_reports_the_overlap_and_ignores_touching_events():
    events = [span(1, 9, 10), span(1, 10, 11), span(1, 9.5, 10.5)]

    pairs = scheduling.overlapping_pairs(events)

    assert pairs == [(0, 2, 30.0), (2, 1, 30.0)]


def test_overlapping_pairs_finds_a_meeting_nested_inside_another():
    events = [span(1, 9, 12), span(1, 10, 11)]

    assert scheduling.overlapping_pairs(events) == [(0, 1, 60.0)]


def test_overlapping_pairs_is_empty_for_a_clean_day():
    assert scheduling.overlapping_pairs([span(1, 9, 10), span(1, 11, 12)]) == []


def test_tight_pairs_reports_gaps_smaller_than_the_buffer():
    events = [span(1, 9, 10), span(1, 10.25, 11), span(1, 12, 13)]

    assert scheduling.tight_pairs(events, buffer_minutes=30) == [(0, 1, 15.0)]


def test_tight_pairs_ignores_overlaps_and_an_unset_buffer():
    overlapping = [span(1, 9, 11), span(1, 10, 12)]

    assert scheduling.tight_pairs(overlapping, buffer_minutes=30) == []
    assert scheduling.tight_pairs([span(1, 9, 10), span(1, 10, 11)], buffer_minutes=0) == []


def test_tight_pairs_counts_back_to_back_meetings_as_tight():
    events = [span(1, 9, 10), span(1, 10, 11)]

    assert scheduling.tight_pairs(events, buffer_minutes=15) == [(0, 1, 0.0)]


# ==========================================================================
# Pure logic: slots and ranking
# ==========================================================================


def test_align_up_moves_to_the_next_quarter_hour():
    assert scheduling.align_up(dt(1, 9, 7), 15) == dt(1, 9, 15)
    assert scheduling.align_up(dt(1, 9, 15), 15) == dt(1, 9, 15)
    assert scheduling.align_up(dt(1, 9, 46), 15) == dt(1, 10, 0)


def test_generate_slots_places_them_on_the_grid_inside_the_window():
    slots = scheduling.generate_slots([span(1, 9, 10)], duration_minutes=30, step_minutes=15)

    assert slots == [span(1, 9, 9.5), span(1, 9.25, 9.75), span(1, 9.5, 10)]


def test_generate_slots_stops_at_the_limit_and_refuses_a_zero_duration():
    slots = scheduling.generate_slots([WORKDAY], duration_minutes=60, step_minutes=15, limit=3)

    assert len(slots) == 3
    assert scheduling.generate_slots([WORKDAY], duration_minutes=0) == []


def test_conflicting_keys_names_only_the_busy_attendees():
    busy = {
        "free@example.com": [span(1, 15, 16)],
        "busy@example.com": [span(1, 9.5, 10)],
    }

    assert scheduling.conflicting_keys(span(1, 9, 10), busy) == ["busy@example.com"]


def test_violates_buffer_only_for_a_gap_that_is_too_small():
    assert scheduling.violates_buffer(span(1, 10, 11), [span(1, 9.75, 10)], 30) is True
    assert scheduling.violates_buffer(span(1, 10, 11), [span(1, 9, 9.5)], 30) is False
    # An overlap is a conflict, not a buffer problem.
    assert scheduling.violates_buffer(span(1, 10, 11), [span(1, 10.5, 12)], 30) is False


def test_rank_slots_prefers_no_conflicts_then_the_original_day_then_earliest():
    clashing_early = span(1, 9, 10)
    clean_late = span(1, 15, 16)
    clean_next_day = span(2, 9, 10)

    ranked = scheduling.rank_slots(
        [clashing_early, clean_late, clean_next_day],
        busy_by_key={"guest@example.com": [span(1, 9, 10)]},
        original_start=dt(1, 14),
        tzinfo=UTC,
    )

    assert [(slot.start, slot.score) for slot in ranked] == [
        (clean_late[0], 110.0),   # free, and on the day the meeting is already on
        (clean_next_day[0], 100.0),
        (clashing_early[0], 85.0),  # busy attendee (-25) but same day (+10)
    ]
    assert ranked[-1].conflicts == ("guest@example.com",)
    assert "busy: guest@example.com" in ranked[-1].reasons


def test_rank_slots_drops_slots_the_organiser_cannot_make():
    ranked = scheduling.rank_slots(
        [span(1, 9, 10), span(1, 11, 12)],
        unavailable=[span(1, 9.5, 10.5)],
    )

    assert [slot.start for slot in ranked] == [dt(1, 11)]


def test_rank_slots_penalises_a_slot_that_eats_the_buffer():
    ranked = scheduling.rank_slots(
        [span(1, 11, 12)],
        busy_by_key={"guest@example.com": [span(1, 10, 10.75)]},
        buffer_minutes=30,
    )

    assert ranked[0].score == 92.0
    assert "less than 30 min from another meeting" in ranked[0].reasons


def test_rank_slots_honours_the_limit():
    slots = scheduling.generate_slots([WORKDAY], duration_minutes=60)

    assert len(scheduling.rank_slots(slots, limit=3)) == 3


# ==========================================================================
# Tool level
# ==========================================================================


@pytest.fixture
def actions():
    """Patches calendar_actions and pins the credential provider, as test_server does."""
    provider = MagicMock(name="CredentialProvider")
    provider.get.return_value = MagicMock(name="credentials")
    fake = MagicMock(name="calendar_actions")
    fake.get_calendar_timezone.return_value = "UTC"
    fake.find_calendars.return_value = CalendarListResponse(
        items=[CalendarListEntry(etag='"1"', id="primary", summary="Me", primary=True)]
    )
    with patch.object(server_module, "credential_provider", provider), \
            patch.object(server_module, "calendar_actions", fake):
        fake.provider = provider
        yield fake


def busy_response(**calendars):
    """Builds what calendar_actions.find_availability returns."""
    return {
        calendar_id: {
            "busy": [{"start": start, "end": end} for start, end in intervals],
            "errors": [],
        }
        for calendar_id, intervals in calendars.items()
    }


def event_payload(event_id, start, end, summary="Meeting", **extra):
    payload = {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    payload.update(extra)
    return GoogleCalendarEvent(**payload)


async def structured(name, arguments):
    result = await server_module.server.call_tool(name, arguments)
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    return result.structured_content


async def test_find_focus_time_reports_the_gaps_in_the_working_day(actions):
    actions.find_availability.return_value = busy_response(primary=[span(1, 11, 12)])

    data = await structured("find_focus_time", {
        "time_min": "2026-01-01T00:00:00Z",
        "time_max": "2026-01-02T00:00:00Z",
        "hours_needed": 4,
    })

    # Default preferences: Thursday 09:00-17:00, one hour booked at 11:00.
    assert data["total_free_hours"] == 7.0
    assert data["satisfiable"] is True
    assert data["count"] == 2
    # Longest block first, and only the calendars the account has selected.
    assert [block["start"] for block in data["blocks"]] == [
        "2026-01-01T12:00:00+00:00",
        "2026-01-01T09:00:00+00:00",
    ]
    assert data["blocks"][0]["duration_minutes"] == 300.0
    assert data["blocks"][0]["weekday"] == "Thursday"
    assert data["calendar_ids"] == ["primary"]


async def test_find_focus_time_says_when_the_week_cannot_supply_the_hours(actions):
    actions.find_availability.return_value = busy_response(primary=[span(1, 10, 17)])

    data = await structured("find_focus_time", {
        "time_min": "2026-01-01T00:00:00Z",
        "time_max": "2026-01-02T00:00:00Z",
        "hours_needed": 4,
        "calendar_ids": ["primary", "work@example.com"],
    })

    assert data["satisfiable"] is False
    assert data["total_free_hours"] == 1.0
    assert "Only 1 of the 4 focus hours" in data["message"]
    assert actions.find_availability.call_args.kwargs["calendar_ids"] == [
        "primary",
        "work@example.com",
    ]


async def test_block_focus_time_dry_run_previews_without_writing(actions):
    actions.find_availability.return_value = busy_response(primary=[span(1, 11, 12)])

    data = await structured("block_focus_time", {
        "time_min": "2026-01-01T00:00:00Z",
        "time_max": "2026-01-02T00:00:00Z",
        "hours_needed": 6,
        "dry_run": True,
    })

    actions.create_event.assert_not_called()
    assert data["dry_run"] is True
    assert data["hours_booked"] == 6.0
    assert data["satisfied"] is True
    # Longest block whole, then the last block trimmed to the remaining hour.
    assert [(event["start"], event["end"]) for event in data["events"]] == [
        ("2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"),
        ("2026-01-01T12:00:00+00:00", "2026-01-01T17:00:00+00:00"),
    ]
    assert all(event["created"] is False for event in data["events"])


async def test_block_focus_time_books_the_blocks_it_picked(actions):
    actions.find_availability.return_value = busy_response(primary=[])
    actions.create_event.return_value = event_payload(
        "focus-1", dt(1, 9), dt(1, 11), summary="Deep work"
    )

    data = await structured("block_focus_time", {
        "time_min": "2026-01-01T00:00:00Z",
        "time_max": "2026-01-02T00:00:00Z",
        "hours_needed": 2,
        "title": "Deep work",
    })

    assert data["count"] == 1
    assert data["events"][0]["created"] is True
    assert data["events"][0]["event_id"] == "focus-1"
    kwargs = actions.create_event.call_args.kwargs
    assert kwargs["calendar_id"] == "primary"  # preferences.focus_calendar_id
    assert kwargs["send_notifications"] is False
    created = kwargs["event_data"]
    assert created.summary == "Deep work"
    assert created.reminders.useDefault is False


async def test_block_focus_time_refuses_when_nothing_is_free(actions):
    actions.find_availability.return_value = busy_response(primary=[span(1, 9, 17)])

    with pytest.raises(ToolError, match="No free block of at least 60 minutes"):
        await server_module.server.call_tool("block_focus_time", {
            "time_min": "2026-01-01T00:00:00Z",
            "time_max": "2026-01-02T00:00:00Z",
            "hours_needed": 2,
        })

    actions.create_event.assert_not_called()


async def test_detect_conflicts_finds_the_double_booking(actions):
    actions.find_events.return_value = EventsResponse(items=[
        event_payload("a", dt(1, 9), dt(1, 10), summary="Standup"),
        event_payload("b", dt(1, 9, 30), dt(1, 10, 30), summary="Client call"),
        event_payload("c", dt(1, 14), dt(1, 15), summary="Alone"),
    ])

    data = await structured("detect_conflicts", {
        "time_min": "2026-01-01T00:00:00Z",
        "time_max": "2026-01-02T00:00:00Z",
    })

    assert data["event_count"] == 3
    assert data["conflict_count"] == 1
    conflict = data["conflicts"][0]
    assert (conflict["first"]["summary"], conflict["second"]["summary"]) == (
        "Standup",
        "Client call",
    )
    assert conflict["overlap_minutes"] == 30.0
    assert conflict["same_calendar"] is True
    assert data["accounts"] == ["default"]
    assert data["calendar_ids"] == ["default:primary"]


async def test_detect_conflicts_ignores_declined_and_all_day_events(actions):
    actions.find_events.return_value = EventsResponse(items=[
        event_payload("a", dt(1, 9), dt(1, 10)),
        event_payload(
            "b", dt(1, 9), dt(1, 10), summary="Declined",
            attendees=[{"email": "me@example.com", "self": True, "responseStatus": "declined"}],
        ),
        GoogleCalendarEvent(
            id="c", summary="Holiday", status="confirmed",
            start={"date": "2026-01-01"}, end={"date": "2026-01-02"},
        ),
    ])

    data = await structured("detect_conflicts", {
        "time_min": "2026-01-01T00:00:00Z",
        "time_max": "2026-01-02T00:00:00Z",
    })

    assert data["event_count"] == 1
    assert data["conflict_count"] == 0
    assert "No conflicts" in data["message"]


async def test_detect_conflicts_can_be_asked_to_include_all_day_events(actions):
    actions.find_events.return_value = EventsResponse(items=[
        event_payload("a", dt(1, 9), dt(1, 10), summary="Standup"),
        GoogleCalendarEvent(
            id="c", summary="Holiday", status="confirmed",
            start={"date": "2026-01-01"}, end={"date": "2026-01-02"},
        ),
    ])

    data = await structured("detect_conflicts", {
        "time_min": "2026-01-01T00:00:00Z",
        "time_max": "2026-01-02T00:00:00Z",
        "include_all_day": True,
    })

    assert data["event_count"] == 2
    assert data["conflict_count"] == 1
    conflict = data["conflicts"][0]
    assert conflict["first"]["summary"] == "Holiday"
    assert conflict["first"]["all_day"] is True
    assert conflict["overlap_minutes"] == 60.0


async def test_suggest_reschedule_ranks_times_the_attendee_can_make(actions):
    actions.get_event.return_value = event_payload(
        "evt-1", dt(1, 14), dt(1, 15), summary="1:1",
        attendees=[
            {"email": "me@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "guest@example.com", "responseStatus": "accepted"},
        ],
    )
    actions.find_availability.return_value = busy_response(
        primary=[span(1, 14, 15)],                      # the meeting itself
        **{"guest@example.com": [span(1, 9, 11)]},
    )

    data = await structured("suggest_reschedule", {
        "event_id": "evt-1",
        "search_from": "2026-01-01T09:00:00Z",
        "search_days": 1,
        "max_suggestions": 3,
    })

    assert data["applied"] is False
    assert data["duration_minutes"] == 60.0
    assert data["attendees"] == ["guest@example.com"]
    assert data["count"] == 3
    best = data["suggestions"][0]
    assert best["start"] == "2026-01-01T11:00:00+00:00"
    assert best["attendee_conflicts"] == []
    assert "same day as the original" in best["reasons"]
    actions.move_event.assert_not_called()


async def test_suggest_reschedule_can_apply_the_top_suggestion(actions):
    actions.get_event.return_value = event_payload("evt-1", dt(1, 14), dt(1, 15))
    actions.find_availability.return_value = busy_response(primary=[span(1, 14, 15)])
    actions.move_event.return_value = event_payload("evt-1", dt(1, 9), dt(1, 10))

    data = await structured("suggest_reschedule", {
        "event_id": "evt-1",
        "search_from": "2026-01-01T09:00:00Z",
        "search_days": 1,
        "apply": True,
    })

    assert data["applied"] is True
    assert data["applied_event"]["id"] == "evt-1"
    kwargs = actions.move_event.call_args.kwargs
    assert kwargs["new_start"] == dt(1, 9)
    assert kwargs["new_end"] == dt(1, 10)
    assert "Moved" in data["message"]


async def test_suggest_reschedule_rejects_an_all_day_event(actions):
    actions.get_event.return_value = GoogleCalendarEvent(
        id="evt-1", summary="Holiday", status="confirmed",
        start={"date": "2026-01-01"}, end={"date": "2026-01-02"},
    )

    with pytest.raises(ToolError, match="needs a timed event"):
        await server_module.server.call_tool("suggest_reschedule", {"event_id": "evt-1"})


async def test_schedule_mutual_falls_back_to_its_own_search_when_lunch_is_set(actions):
    """A lunch break cannot be expressed as one clock range, so the tool places the slot."""
    from calendar_mcp import preferences as preferences_module

    prefs = preferences_module.Preferences(lunch=("12:00", "13:00"), buffer_minutes=15)
    actions.find_availability.return_value = busy_response(
        primary=[span(1, 9, 11.5)],
        **{"guest@example.com": []},
    )
    actions.create_event.return_value = event_payload("new-1", dt(1, 13), dt(1, 14))

    with patch.object(preferences_module, "load", return_value=prefs):
        data = await structured("schedule_mutual", {
            "attendee_calendar_ids": ["guest@example.com"],
            "time_min": "2026-01-01T09:00:00Z",
            "time_max": "2026-01-01T17:00:00Z",
            "duration_minutes": 60,
            "summary": "Sync",
        })

    actions.find_mutual_availability_and_schedule.assert_not_called()
    created = actions.create_event.call_args.kwargs["event_data"]
    # 11:30 is free but the 15-minute buffer and the 12:00 lunch leave no full
    # hour before lunch, so the first workable slot is straight after it.
    assert created.start.dateTime == dt(1, 13)
    assert created.end.dateTime == dt(1, 14)
    assert created.attendees == ["guest@example.com"]
    assert "Booked 'Sync'" in data["message"]


async def test_schedule_mutual_keeps_using_google_search_for_plain_preferences(actions):
    actions.find_mutual_availability_and_schedule.return_value = event_payload(
        "new-2", dt(1, 9), dt(1, 9, 30), summary="Chat"
    )

    await structured("schedule_mutual", {
        "attendee_calendar_ids": ["guest@example.com"],
        "time_min": "2026-01-01T09:00:00Z",
        "time_max": "2026-01-01T17:00:00Z",
        "duration_minutes": 30,
        "summary": "Chat",
    })

    # Default preferences are a plain 09:00-17:00 weekday, so they are passed
    # straight to the existing search rather than duplicating it here.
    kwargs = actions.find_mutual_availability_and_schedule.call_args.kwargs
    assert kwargs["working_hours_start"].hour == 9
    assert kwargs["working_hours_end"].hour == 17
    actions.create_event.assert_not_called()


async def test_schedule_mutual_can_ignore_preferences_entirely(actions):
    actions.find_mutual_availability_and_schedule.return_value = event_payload(
        "new-3", dt(3, 9), dt(3, 9, 30), summary="Weekend"
    )

    await structured("schedule_mutual", {
        "attendee_calendar_ids": ["guest@example.com"],
        "time_min": "2026-01-03T00:00:00Z",   # a Saturday: not a working day
        "time_max": "2026-01-03T23:00:00Z",
        "duration_minutes": 30,
        "summary": "Weekend",
        "respect_preferences": False,
    })

    kwargs = actions.find_mutual_availability_and_schedule.call_args.kwargs
    assert kwargs["working_hours_start"] is None
    assert kwargs["working_hours_end"] is None
