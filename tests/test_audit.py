"""Tests for the time audit: pure maths in calendar_mcp.audit, plus the tool.

Every event here is hand-made. Nothing in this file touches the network, and
the only Google-shaped thing is ``calendar_actions.find_events``, which the tool
test replaces with a stub.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from calendar_mcp import audit as audit_logic
from calendar_mcp import server as server_module
from calendar_mcp.audit import AuditEvent, build_audit, event_from_google, size_bucket
from calendar_mcp.preferences import Preferences

TZ = ZoneInfo("UTC")

# A Monday, so the default Mon-Fri 09:00-17:00 working hours apply.
MONDAY = datetime(2026, 3, 2, 0, 0, tzinfo=TZ)
WEEK = (MONDAY, MONDAY + timedelta(days=7))
MONDAY_ONLY = (MONDAY, MONDAY + timedelta(days=1))


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A timestamp ``day_offset`` days after Monday, at ``hour:minute`` UTC."""
    return MONDAY + timedelta(days=day_offset, hours=hour, minutes=minute)


def meeting(
    day_offset: int,
    start_hour: float,
    end_hour: float,
    attendees=(),
    **kwargs,
) -> AuditEvent:
    """A one-off meeting on ``day_offset`` between the two wall-clock hours."""
    start = MONDAY + timedelta(days=day_offset, hours=start_hour)
    end = MONDAY + timedelta(days=day_offset, hours=end_hour)
    kwargs.setdefault("summary", "Meeting")
    kwargs.setdefault("self_email", "me@corp.com")
    return AuditEvent(start=start, end=end, attendees=list(attendees), **kwargs)


def prefs(**overrides) -> Preferences:
    """Default preferences (Mon-Fri 09:00-17:00 UTC) with overrides applied."""
    base = {"timezone": "UTC"}
    base.update(overrides)
    return Preferences(**base)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected",
    [(0, "solo"), (1, "solo"), (2, "1:1"), (3, "small"), (4, "small"), (5, "large"), (30, "large")],
)
def test_size_bucket_boundaries(count, expected):
    assert size_bucket(count) == expected


def test_split_email_domain():
    assert audit_logic.split_email_domain("Alice@Corp.com") == "corp.com"
    assert audit_logic.split_email_domain("not-an-address") == ""


def test_others_drops_self_and_duplicates():
    event = meeting(0, 9, 10, ["me@corp.com", "Alice@corp.com", "alice@corp.com", ""])
    assert event.others() == ["alice@corp.com"]


# ---------------------------------------------------------------------------
# Normalisation from the Google shape
# ---------------------------------------------------------------------------


def test_event_from_google_reads_a_timed_event():
    raw = {
        "summary": "Sync",
        "start": {"dateTime": "2026-03-02T09:00:00Z"},
        "end": {"dateTime": "2026-03-02T10:00:00Z"},
        "organizer": {"email": "boss@corp.com"},
        "recurringEventId": "series-1",
        "transparency": "transparent",
        "attendees": [
            {"email": "me@corp.com", "self": True, "responseStatus": "declined"},
            {"email": "boss@corp.com", "responseStatus": "accepted"},
            {"email": "room-a@corp.com", "resource": True},
        ],
    }
    event = event_from_google(raw, TZ, calendar_id="primary")
    assert event is not None
    assert event.summary == "Sync"
    assert event.start == at(0, 9) and event.end == at(0, 10)
    assert event.attendees == ["me@corp.com", "boss@corp.com"]  # resource dropped
    assert event.self_email == "me@corp.com"
    assert event.is_declined is True
    assert event.is_free is True
    assert event.is_recurring is True
    assert event.all_day is False
    assert event.calendar_id == "primary"


def test_event_from_google_reads_an_all_day_event():
    raw = {"start": {"date": "2026-03-02"}, "end": {"date": "2026-03-03"}}
    event = event_from_google(raw, TZ)
    assert event is not None and event.all_day is True
    assert event.start == MONDAY and event.end == MONDAY + timedelta(days=1)


def test_event_from_google_rejects_unusable_events():
    assert event_from_google({"start": {"dateTime": "nonsense"}, "end": {}}, TZ) is None
    assert event_from_google({}, TZ) is None
    # end before start
    assert event_from_google(
        {"start": {"dateTime": "2026-03-02T10:00:00Z"}, "end": {"dateTime": "2026-03-02T09:00:00Z"}},
        TZ,
    ) is None


def test_event_from_google_accepts_a_pydantic_style_object():
    raw = SimpleNamespace(
        summary="Object event",
        start=SimpleNamespace(dateTime=at(0, 11)),
        end=SimpleNamespace(dateTime=at(0, 12)),
        attendees=[SimpleNamespace(email="alice@corp.com", self=None, responseStatus="accepted")],
        organizer=SimpleNamespace(email="alice@corp.com", self=None),
        recurring_event_id=None,
    )
    event = event_from_google(raw, TZ)
    assert event is not None and event.attendees == ["alice@corp.com"]


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_declined_free_and_all_day_events_are_excluded():
    events = [
        meeting(0, 9, 10),
        meeting(0, 10, 11, self_response_status="declined"),
        meeting(0, 11, 12, transparency="transparent"),
        AuditEvent(start=MONDAY, end=MONDAY + timedelta(days=1), all_day=True),
        meeting(20, 9, 10),  # far outside the window
    ]
    result = build_audit(events, WEEK, prefs(), TZ)
    assert result.total_meeting_count == 1
    assert result.total_meeting_hours == 1.0
    assert result.excluded.declined == 1
    assert result.excluded.marked_free == 1
    assert result.excluded.all_day == 1
    assert result.excluded.outside_window == 1


def test_include_flags_bring_the_excluded_events_back():
    events = [
        meeting(0, 9, 10, self_response_status="declined"),
        AuditEvent(start=at(0, 0), end=at(1, 0), all_day=True),
    ]
    result = build_audit(events, WEEK, prefs(), TZ, include_all_day=True, include_declined=True)
    assert result.total_meeting_count == 2
    assert result.total_meeting_hours == 25.0
    assert result.excluded.declined == 0


# ---------------------------------------------------------------------------
# Totals, periods and shares
# ---------------------------------------------------------------------------


def test_totals_and_share_of_working_hours():
    # Two hours of meetings on Monday, out of a 40-hour Mon-Fri week.
    events = [meeting(0, 9, 10), meeting(0, 14, 15)]
    result = build_audit(events, WEEK, prefs(), TZ)
    assert result.total_meeting_hours == 2.0
    assert result.working_hours_available == 40.0
    assert result.share_of_working_hours == 0.05


def test_meeting_time_outside_working_hours_does_not_inflate_the_share():
    # 07:00-09:00 is before the working day: counted as meeting time, but only
    # the part inside working hours counts towards the share.
    result = build_audit([meeting(0, 7, 9)], WEEK, prefs(), TZ)
    assert result.total_meeting_hours == 2.0
    assert result.meeting_hours_in_working_hours == 0.0
    assert result.share_of_working_hours == 0.0


def test_overlapping_meetings_double_count_hours_but_not_the_share():
    events = [meeting(0, 9, 11), meeting(0, 9, 11)]
    result = build_audit(events, MONDAY_ONLY, prefs(), TZ)
    assert result.total_meeting_hours == 4.0
    assert result.meeting_hours_in_working_hours == 2.0
    assert result.share_of_working_hours == 0.25  # 2h of an 8h day


def test_events_are_clipped_to_the_window():
    long_event = AuditEvent(start=at(0, 8), end=at(2, 8), self_email="me@corp.com")
    result = build_audit([long_event], MONDAY_ONLY, prefs(), TZ)
    assert result.total_meeting_hours == 16.0  # 08:00 Monday to midnight


def test_group_by_day_gives_one_period_per_day():
    result = build_audit([meeting(0, 9, 10)], WEEK, prefs(), TZ, group_by="day")
    assert result.group_by == "day"
    assert len(result.periods) == 7
    assert result.periods[0].period == "2026-03-02"
    assert result.periods[0].meeting_hours == 1.0
    assert result.periods[0].working_hours == 8.0
    assert result.periods[1].meeting_hours == 0.0


def test_group_by_week_gives_one_period_per_iso_week():
    events = [meeting(0, 9, 10), meeting(7, 9, 11)]
    window = (MONDAY, MONDAY + timedelta(days=14))
    result = build_audit(events, window, prefs(), TZ, group_by="week")
    assert [period.period for period in result.periods] == ["2026-W10", "2026-W11"]
    assert result.periods[0].meeting_hours == 1.0
    assert result.periods[1].meeting_hours == 2.0
    assert result.busiest_period is not None
    assert result.busiest_period.period == "2026-W11"


def test_longest_meeting_day_is_a_day_even_when_grouped_by_week():
    events = [meeting(0, 9, 10), meeting(1, 9, 13)]
    result = build_audit(events, WEEK, prefs(), TZ, group_by="week")
    assert result.longest_meeting_day is not None
    assert result.longest_meeting_day.period == "2026-03-03"  # Tuesday
    assert result.longest_meeting_day.meeting_hours == 4.0


def test_empty_calendar_reports_no_longest_day():
    result = build_audit([], WEEK, prefs(), TZ)
    assert result.longest_meeting_day is None
    assert result.busiest_period is None
    assert result.total_meeting_hours == 0.0
    assert result.insights == [
        "No meetings in this window -- all of your working time was unbooked."
    ]


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------


def test_breakdown_by_size():
    events = [
        meeting(0, 9, 10, ["me@corp.com", "alice@corp.com"]),                      # 1:1
        meeting(0, 10, 12, ["me@corp.com", "alice@corp.com", "bob@corp.com"]),     # small
        meeting(1, 9, 12, ["me@corp.com"] + [f"p{i}@corp.com" for i in range(9)]),  # large
        meeting(2, 9, 10),                                                          # solo
    ]
    result = build_audit(events, WEEK, prefs(), TZ)
    hours = {bucket.label: bucket.hours for bucket in result.by_size}
    assert hours == {"1:1": 1.0, "small": 2.0, "large": 3.0, "solo": 1.0}
    shares = {bucket.label: bucket.share_of_meeting_hours for bucket in result.by_size}
    assert shares["large"] == pytest.approx(3.0 / 7.0, abs=1e-4)


def test_breakdown_by_domain_counts_a_mixed_meeting_in_both_domains():
    events = [meeting(0, 9, 11, ["me@corp.com", "alice@corp.com", "dan@vendor.io"])]
    result = build_audit(events, WEEK, prefs(), TZ)
    hours = {bucket.label: bucket.hours for bucket in result.by_domain}
    assert hours == {"corp.com": 2.0, "vendor.io": 2.0}


def test_top_people_are_ranked_by_hours_and_capped():
    events = [
        meeting(0, 9, 12, ["me@corp.com", "alice@corp.com"]),
        meeting(1, 9, 10, ["me@corp.com", "alice@corp.com", "bob@corp.com"]),
    ]
    result = build_audit(events, WEEK, prefs(), TZ)
    assert [(p.email, p.hours, p.meeting_count) for p in result.top_people] == [
        ("alice@corp.com", 4.0, 2),
        ("bob@corp.com", 1.0, 1),
    ]

    many = [
        meeting(0, 9 + i * 0.25, 9.25 + i * 0.25, ["me@corp.com", f"p{i:02d}@corp.com"])
        for i in range(15)
    ]
    capped = build_audit(many, WEEK, prefs(), TZ)
    assert len(capped.top_people) == 10


def test_breakdown_by_recurrence():
    events = [
        meeting(0, 9, 11, recurring_event_id="series-1"),
        meeting(1, 9, 10),
    ]
    result = build_audit(events, WEEK, prefs(), TZ)
    hours = {bucket.label: bucket.hours for bucket in result.by_recurrence}
    assert hours == {"recurring": 2.0, "one-off": 1.0}


# ---------------------------------------------------------------------------
# Back-to-back stretches
# ---------------------------------------------------------------------------


def test_three_touching_meetings_are_one_back_to_back_stretch():
    events = [meeting(0, 9, 10), meeting(0, 10, 11), meeting(0, 11, 12)]
    result = build_audit(events, WEEK, prefs(buffer_minutes=15), TZ)
    assert result.back_to_back_count == 1
    assert result.back_to_back_hours == 3.0
    stretch = result.back_to_back_stretches[0]
    assert stretch.meeting_count == 3
    assert stretch.date == "2026-03-02"
    assert stretch.start == at(0, 9).isoformat()
    assert stretch.end == at(0, 12).isoformat()


def test_two_touching_meetings_are_not_a_stretch():
    events = [meeting(0, 9, 10), meeting(0, 10, 11)]
    result = build_audit(events, WEEK, prefs(buffer_minutes=15), TZ)
    assert result.back_to_back_count == 0
    assert result.back_to_back_stretches == []


def test_a_gap_at_least_as_wide_as_the_buffer_breaks_the_stretch():
    # 30-minute gaps with a 15-minute buffer: comfortable, so no stretch.
    events = [meeting(0, 9, 10), meeting(0, 10.5, 11.5), meeting(0, 12, 13)]
    assert build_audit(events, WEEK, prefs(buffer_minutes=15), TZ).back_to_back_count == 0
    # The same day judged against a one-hour buffer is one long stretch.
    assert build_audit(events, WEEK, prefs(buffer_minutes=60), TZ).back_to_back_count == 1


def test_back_to_back_gap_can_be_overridden():
    events = [meeting(0, 9, 10), meeting(0, 10.5, 11.5), meeting(0, 12, 13)]
    result = build_audit(events, WEEK, prefs(), TZ, back_to_back_gap_minutes=45)
    assert result.back_to_back_count == 1


# ---------------------------------------------------------------------------
# Focus time
# ---------------------------------------------------------------------------


def test_focus_time_on_an_empty_working_day():
    result = build_audit([], MONDAY_ONLY, prefs(), TZ)
    assert result.focus_block_count == 1
    assert result.focus_hours_available == 8.0
    assert result.largest_focus_blocks[0].start == at(0, 9).isoformat()


def test_lunch_splits_the_focus_blocks():
    result = build_audit([], MONDAY_ONLY, prefs(lunch=("12:00", "13:00")), TZ)
    assert [block.hours for block in result.largest_focus_blocks] == [4.0, 3.0]
    assert result.focus_hours_available == 7.0


def test_short_gaps_do_not_count_as_focus_time():
    # 09:00-12:00 and 12:30-17:00 booked leaves a 30-minute gap, below the
    # 60-minute default minimum.
    events = [meeting(0, 9, 12), meeting(0, 12.5, 17)]
    result = build_audit(events, MONDAY_ONLY, prefs(), TZ)
    assert result.focus_block_count == 0
    assert result.focus_hours_available == 0.0


def test_the_buffer_shrinks_the_focus_blocks():
    events = [meeting(0, 9, 10)]
    without = build_audit(events, MONDAY_ONLY, prefs(), TZ)
    withbuf = build_audit(events, MONDAY_ONLY, prefs(buffer_minutes=30), TZ)
    assert without.focus_hours_available == 7.0
    assert withbuf.focus_hours_available == 6.5


def test_min_focus_block_is_respected():
    events = [meeting(0, 11, 12)]
    relaxed = build_audit(events, MONDAY_ONLY, prefs(min_focus_block_minutes=60), TZ)
    strict = build_audit(events, MONDAY_ONLY, prefs(min_focus_block_minutes=180), TZ)
    assert [block.hours for block in relaxed.largest_focus_blocks] == [5.0, 2.0]
    assert [block.hours for block in strict.largest_focus_blocks] == [5.0]


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def test_insights_are_three_to_five_readable_lines():
    events = [
        meeting(0, 9, 10, ["me@corp.com", "alice@corp.com"]),
        meeting(1, 9, 13, ["me@corp.com", "alice@corp.com"]),
        meeting(1, 13, 14, ["me@corp.com", "alice@corp.com"]),
        meeting(1, 14, 15, ["me@corp.com", "bob@corp.com"]),
    ]
    result = build_audit(events, WEEK, prefs(buffer_minutes=15), TZ)
    assert 3 <= len(result.insights) <= 5
    joined = " ".join(result.insights)
    assert "% of your working hours went to meetings" in joined
    assert "Tuesdays are your heaviest day" in joined
    assert "Most time with alice@corp.com" in joined
    assert "back-to-back stretch" in joined


def test_insights_mention_recurring_share_when_nothing_is_back_to_back():
    events = [meeting(0, 9, 11, ["me@corp.com", "alice@corp.com"], recurring_event_id="s1")]
    result = build_audit(events, WEEK, prefs(), TZ)
    assert any("recurring" in line for line in result.insights)


# ---------------------------------------------------------------------------
# Timezone handling
# ---------------------------------------------------------------------------


def test_days_are_grouped_in_the_reporting_timezone():
    berlin = ZoneInfo("Europe/Berlin")
    # 23:30 UTC on Monday is 00:30 on Tuesday in Berlin.
    event = AuditEvent(start=at(0, 23, 30), end=at(1, 0, 30), self_email="me@corp.com")
    result = build_audit([event], WEEK, prefs(timezone="Europe/Berlin"), berlin, group_by="day")
    by_period = {period.period: period.meeting_hours for period in result.periods}
    assert by_period["2026-03-03"] == 1.0
    assert by_period["2026-03-02"] == 0.0
    assert result.timezone == "Europe/Berlin"


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _google_event(start: datetime, end: datetime, **extra):
    payload = {
        "summary": "Sync",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    payload.update(extra)
    return payload


@pytest.mark.anyio
async def test_time_audit_tool_reports_over_the_fetched_events(monkeypatch):
    from calendar_mcp.tools.audit import time_audit

    calls = []

    def fake_find_events(credentials, calendar_id="primary", **kwargs):
        calls.append(calendar_id)
        return SimpleNamespace(
            items=[
                _google_event(
                    at(0, 9),
                    at(0, 10),
                    attendees=[
                        {"email": "me@corp.com", "self": True, "responseStatus": "accepted"},
                        {"email": "alice@corp.com"},
                    ],
                ),
                _google_event(
                    at(0, 10),
                    at(0, 11),
                    attendees=[{"email": "me@corp.com", "self": True, "responseStatus": "declined"}],
                ),
            ]
        )

    monkeypatch.setattr(
        server_module.calendar_actions, "find_events", fake_find_events, raising=False
    )
    monkeypatch.setattr(
        server_module.CredentialProvider, "get", lambda self, account=None: object()
    )
    monkeypatch.setattr(server_module, "_default_tzinfo", lambda creds, calendar_id: TZ)

    result = await time_audit(
        time_min=MONDAY.isoformat(),
        time_max=(MONDAY + timedelta(days=7)).isoformat(),
        calendar_ids=["primary", "team@corp.com"],
        group_by="day",
    )

    assert calls == ["primary", "team@corp.com"]
    # The same two events come back from both calendars; one of each is declined.
    assert result.total_meeting_count == 2
    assert result.total_meeting_hours == 2.0
    assert result.excluded.declined == 2
    assert result.group_by == "day"
    assert result.calendar_ids == ["primary", "team@corp.com"]
    assert result.top_people[0].email == "alice@corp.com"


@pytest.mark.anyio
async def test_time_audit_tool_rejects_an_inverted_window(monkeypatch):
    from calendar_mcp.tools.audit import time_audit

    monkeypatch.setattr(
        server_module.CredentialProvider, "get", lambda self, account=None: object()
    )
    monkeypatch.setattr(server_module, "_default_tzinfo", lambda creds, calendar_id: TZ)

    with pytest.raises(server_module.CalendarToolError):
        await time_audit(
            time_min=(MONDAY + timedelta(days=1)).isoformat(),
            time_max=MONDAY.isoformat(),
        )


@pytest.mark.anyio
async def test_time_audit_tool_fails_when_no_calendar_can_be_read(monkeypatch):
    from calendar_mcp.tools.audit import time_audit

    monkeypatch.setattr(
        server_module.calendar_actions,
        "find_events",
        lambda credentials, calendar_id="primary", **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        server_module.CredentialProvider, "get", lambda self, account=None: object()
    )
    monkeypatch.setattr(server_module, "_default_tzinfo", lambda creds, calendar_id: TZ)

    with pytest.raises(server_module.CalendarToolError):
        await time_audit(
            time_min=MONDAY.isoformat(),
            time_max=(MONDAY + timedelta(days=1)).isoformat(),
        )
