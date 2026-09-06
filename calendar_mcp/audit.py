"""Pure analysis behind the ``time_audit`` tool: where did the week go?

Everything here is deterministic and offline. The Google layer is responsible
for fetching raw events; :func:`event_from_google` (itself pure) reduces one of
those to an :class:`AuditEvent`, and :func:`build_audit` turns a list of those
plus the user's :class:`calendar_mcp.preferences.Preferences` into the report.

Conventions used throughout:

* Every interval is aware and half-open, ``start <= t < end``.
* Meeting time is clipped to the requested window before it is counted, so an
  event that straddles the edge only contributes the part inside.
* Overlapping meetings are each counted in the totals (double-booking really
  does inflate "meeting hours"), but the *share of working hours* is computed
  from merged intervals, so it can never exceed 100%.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    AuditBucket,
    AuditExclusions,
    AuditFocusBlock,
    AuditPeriod,
    AuditPerson,
    AuditStretch,
    TimeAuditResult,
)
from .preferences import Preferences, working_windows
from .timeutil import Interval, clip_intervals, free_windows, iter_days, merge_intervals

__all__ = [
    "AuditEvent",
    "DECLINED_STATUSES",
    "SIZE_ONE_ON_ONE",
    "SIZE_SMALL",
    "SIZE_LARGE",
    "SIZE_SOLO",
    "build_audit",
    "event_from_google",
    "filter_events",
    "hours_between",
    "size_bucket",
    "split_email_domain",
]

#: Response statuses that mean "this meeting does not cost me any time".
DECLINED_STATUSES = frozenset({"declined"})

SIZE_SOLO = "solo"
SIZE_ONE_ON_ONE = "1:1"
SIZE_SMALL = "small"
SIZE_LARGE = "large"

_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


# ---------------------------------------------------------------------------
# The normalised event
# ---------------------------------------------------------------------------


@dataclass
class AuditEvent:
    """One calendar entry, reduced to the fields the audit actually reads."""

    start: datetime
    end: datetime
    summary: str = ""
    attendees: List[str] = field(default_factory=list)
    organizer: Optional[str] = None
    self_email: Optional[str] = None
    self_response_status: Optional[str] = None
    transparency: Optional[str] = None
    all_day: bool = False
    recurring_event_id: Optional[str] = None
    calendar_id: Optional[str] = None

    @property
    def is_declined(self) -> bool:
        """True when the user turned this meeting down."""
        return (self.self_response_status or "").strip().lower() in DECLINED_STATUSES

    @property
    def is_free(self) -> bool:
        """True when the event is marked 'free' (transparent) and blocks nothing."""
        return (self.transparency or "").strip().lower() == "transparent"

    @property
    def is_recurring(self) -> bool:
        """True for an instance of a recurring series."""
        return bool(self.recurring_event_id)

    @property
    def interval(self) -> Interval:
        """The event as a ``(start, end)`` pair."""
        return (self.start, self.end)

    def others(self) -> List[str]:
        """Attendee addresses other than the user's own: lower-cased, unique."""
        mine = (self.self_email or "").strip().lower()
        seen: List[str] = []
        for raw in self.attendees:
            email = (raw or "").strip().lower()
            if not email or email == mine or email in seen:
                continue
            seen.append(email)
        return seen


def hours_between(start: datetime, end: datetime) -> float:
    """Length of ``[start, end)`` in hours; never negative."""
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _hours(intervals: Iterable[Interval]) -> float:
    return sum(hours_between(start, end) for start, end in intervals)


def _round(value: float, places: int = 2) -> float:
    return round(float(value), places)


def split_email_domain(email: str) -> str:
    """The domain part of an address, lower-cased; ``''`` when there is none."""
    _, _, domain = (email or "").strip().lower().partition("@")
    return domain


def size_bucket(attendee_count: int) -> str:
    """Maps a headcount onto ``solo`` / ``1:1`` / ``small`` / ``large``.

    ``attendee_count`` includes the user. Up to four people is small; more than
    that is large. An event with no attendee list at all is a solo block.
    """
    if attendee_count <= 1:
        return SIZE_SOLO
    if attendee_count == 2:
        return SIZE_ONE_ON_ONE
    if attendee_count <= 4:
        return SIZE_SMALL
    return SIZE_LARGE


# ---------------------------------------------------------------------------
# Translation from the Google event shape
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, *aliases: str):
    """Reads ``name`` off a pydantic model or a plain dict, trying aliases."""
    for key in (name,) + aliases:
        if isinstance(obj, dict):
            if obj.get(key) is not None:
                return obj[key]
        else:
            value = getattr(obj, key, None)
            if value is not None:
                return value
    return None


def _as_datetime(node: Any, tzinfo) -> Tuple[Optional[datetime], bool]:
    """Turns a Google ``start``/``end`` node into ``(aware datetime, all_day)``."""
    if node is None:
        return None, False
    stamp = _get(node, "dateTime", "date_time")
    if stamp is not None:
        if isinstance(stamp, str):
            try:
                stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                return None, False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=tzinfo)
        return stamp, False
    day = _get(node, "date")
    if day is None:
        return None, False
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            return None, False
    if isinstance(day, datetime):
        day = day.date()
    return datetime.combine(day, datetime.min.time()).replace(tzinfo=tzinfo), True


def event_from_google(raw: Any, tzinfo, calendar_id: Optional[str] = None) -> Optional[AuditEvent]:
    """Reduces one Google event (pydantic model or API dict) to an :class:`AuditEvent`.

    Args:
        raw: A ``GoogleCalendarEvent`` or the equivalent API dict.
        tzinfo: Zone used for all-day events and for naive timestamps.
        calendar_id: Calendar the event came from, recorded on the result.

    Returns:
        The normalised event, or ``None`` when it has no usable start/end.
    """
    start, start_all_day = _as_datetime(_get(raw, "start"), tzinfo)
    end, end_all_day = _as_datetime(_get(raw, "end"), tzinfo)
    if start is None or end is None or end <= start:
        return None

    attendees: List[str] = []
    self_email: Optional[str] = None
    self_status: Optional[str] = None
    for attendee in _get(raw, "attendees") or []:
        email = _get(attendee, "email")
        is_resource = _get(attendee, "resource") is True
        if email and not is_resource:
            attendees.append(str(email))
        if _get(attendee, "self") is True:
            if email:
                self_email = str(email)
            status = _get(attendee, "responseStatus", "response_status")
            if status:
                self_status = str(status)

    organizer_node = _get(raw, "organizer")
    organizer = _get(organizer_node, "email") if organizer_node is not None else None
    if self_email is None and organizer_node is not None and _get(organizer_node, "self") is True:
        self_email = str(organizer) if organizer else None

    transparency = _get(raw, "transparency")
    recurring = _get(raw, "recurringEventId", "recurring_event_id")

    return AuditEvent(
        start=start,
        end=end,
        summary=str(_get(raw, "summary") or ""),
        attendees=attendees,
        organizer=str(organizer) if organizer else None,
        self_email=self_email,
        self_response_status=self_status,
        transparency=str(transparency) if transparency else None,
        all_day=bool(start_all_day or end_all_day),
        recurring_event_id=str(recurring) if recurring else None,
        calendar_id=calendar_id,
    )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_events(
    events: Sequence[AuditEvent],
    window: Interval,
    include_all_day: bool = False,
    include_declined: bool = False,
    include_free: bool = False,
) -> Tuple[List[AuditEvent], AuditExclusions]:
    """Drops the events that do not count as meeting time.

    Returns the kept events (still carrying their original, unclipped times)
    alongside a tally of what was dropped and why. Reasons are tested in order,
    so every dropped event is counted exactly once.
    """
    low, high = window
    kept: List[AuditEvent] = []
    dropped_all_day = dropped_declined = dropped_free = dropped_outside = 0
    for event in events:
        if event.end <= low or event.start >= high:
            dropped_outside += 1
            continue
        if event.all_day and not include_all_day:
            dropped_all_day += 1
            continue
        if event.is_declined and not include_declined:
            dropped_declined += 1
            continue
        if event.is_free and not include_free:
            dropped_free += 1
            continue
        kept.append(event)
    kept.sort(key=lambda item: (item.start, item.end))
    return kept, AuditExclusions(
        all_day=dropped_all_day,
        declined=dropped_declined,
        marked_free=dropped_free,
        outside_window=dropped_outside,
    )


# ---------------------------------------------------------------------------
# Period grouping
# ---------------------------------------------------------------------------


def _day_spans(window: Interval, tzinfo) -> List[Tuple[date, Interval]]:
    """Every local day the window touches, with its clipped ``(start, end)``."""
    low, high = window
    spans: List[Tuple[date, Interval]] = []
    for day in iter_days(low, high, tzinfo):
        day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=tzinfo)
        day_end = day_start + timedelta(days=1)
        start, end = max(day_start, low), min(day_end, high)
        if end > start:
            spans.append((day, (start, end)))
    return spans


def _period_key(day: date, group_by: str) -> Tuple[Any, str]:
    """Sort key and human label for the period ``day`` belongs to."""
    if group_by == "day":
        return (day, day.isoformat())
    year, week = day.isocalendar()[0], day.isocalendar()[1]
    return ((year, week), f"{year}-W{week:02d}")


def _measure_period(
    label: str,
    span: Interval,
    events: Sequence[AuditEvent],
    prefs: Preferences,
    tzinfo,
) -> AuditPeriod:
    """Meeting and working hours for one day or week."""
    booked = clip_intervals((event.interval for event in events), span)
    merged = merge_intervals(booked)
    windows = working_windows(prefs, span[0], span[1], tzinfo=tzinfo)
    working = _hours(windows)
    in_working = sum(_hours(clip_intervals(merged, window)) for window in windows)
    share = (in_working / working) if working > 0 else 0.0
    return AuditPeriod(
        period=label,
        start=span[0].isoformat(),
        end=span[1].isoformat(),
        meeting_count=len(booked),
        meeting_hours=_round(_hours(booked)),
        working_hours=_round(working),
        meeting_hours_in_working_hours=_round(in_working),
        share_of_working_hours=_round(min(share, 1.0), 4),
    )


def _buckets(table: Dict[str, Tuple[int, float]], total_hours: float) -> List[AuditBucket]:
    """Turns ``{label: (count, hours)}`` into sorted, share-annotated buckets."""
    buckets = [
        AuditBucket(
            label=label,
            meeting_count=count,
            hours=_round(hours),
            share_of_meeting_hours=_round((hours / total_hours) if total_hours > 0 else 0.0, 4),
        )
        for label, (count, hours) in table.items()
    ]
    buckets.sort(key=lambda bucket: (-bucket.hours, bucket.label))
    return buckets


def _back_to_back(
    events: Sequence[AuditEvent],
    window: Interval,
    gap_minutes: int,
    min_run: int = 3,
) -> List[AuditStretch]:
    """Runs of ``min_run`` or more meetings separated by less than ``gap_minutes``."""
    spans = sorted(clip_intervals((event.interval for event in events), window))
    if not spans:
        return []
    gap = timedelta(minutes=max(0, int(gap_minutes)))
    stretches: List[AuditStretch] = []
    run: List[Interval] = [spans[0]]

    def close(current: List[Interval]) -> None:
        if len(current) < min_run:
            return
        start = current[0][0]
        end = max(item[1] for item in current)
        stretches.append(
            AuditStretch(
                date=start.date().isoformat(),
                start=start.isoformat(),
                end=end.isoformat(),
                meeting_count=len(current),
                hours=_round(hours_between(start, end)),
            )
        )

    for span in spans[1:]:
        previous_end = max(item[1] for item in run)
        if span[0] - previous_end < gap or span[0] <= previous_end:
            run.append(span)
        else:
            close(run)
            run = [span]
    close(run)
    return stretches


def _focus_blocks(
    events: Sequence[AuditEvent],
    windows: Sequence[Interval],
    buffer_minutes: int,
    min_block_minutes: int,
) -> List[Interval]:
    """Free stretches inside the working windows that are long enough to use."""
    busy = merge_intervals(event.interval for event in events)
    minimum = timedelta(minutes=max(0, int(min_block_minutes)))
    blocks: List[Interval] = []
    for window in windows:
        for free in free_windows(busy, window, buffer_minutes=buffer_minutes):
            if free[1] - free[0] >= minimum:
                blocks.append(free)
    blocks.sort(key=lambda pair: pair[0])
    return blocks


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def _insights(
    share: float,
    total_hours: float,
    heaviest_weekday: Optional[Tuple[str, float]],
    top_person: Optional[AuditPerson],
    recurring_share: float,
    back_to_back_count: int,
    focus_hours: float,
    longest_focus: Optional[AuditFocusBlock],
) -> List[str]:
    """Three to five plain-English takeaways, most actionable first."""
    if total_hours <= 0:
        return ["No meetings in this window -- all of your working time was unbooked."]
    lines = [
        f"{share * 100:.0f}% of your working hours went to meetings "
        f"({total_hours:.1f}h in total)."
    ]
    if heaviest_weekday and heaviest_weekday[1] > 0:
        name, hours = heaviest_weekday
        lines.append(f"{name}s are your heaviest day ({hours:.1f}h of meetings).")
    if top_person is not None:
        lines.append(
            f"Most time with {top_person.email}: {top_person.hours:.1f}h "
            f"across {top_person.meeting_count} meetings."
        )
    if back_to_back_count:
        lines.append(
            f"{back_to_back_count} back-to-back stretch"
            f"{'' if back_to_back_count == 1 else 'es'} of three or more meetings "
            "with no real gap."
        )
    elif recurring_share > 0:
        lines.append(f"{recurring_share * 100:.0f}% of your meeting time is recurring.")
    if len(lines) < 5:
        if longest_focus is not None:
            lines.append(
                f"{focus_hours:.1f}h of focus time is left, the longest single block "
                f"being {longest_focus.hours:.1f}h."
            )
        else:
            lines.append("No free block survived long enough to count as focus time.")
    return lines[:5]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def build_audit(
    events: Sequence[AuditEvent],
    window: Interval,
    prefs: Preferences,
    tzinfo,
    group_by: str = "week",
    calendar_ids: Optional[Sequence[str]] = None,
    include_all_day: bool = False,
    include_declined: bool = False,
    back_to_back_gap_minutes: Optional[int] = None,
    top_people: int = 10,
) -> TimeAuditResult:
    """Computes the whole time audit from already-normalised events.

    Args:
        events: Normalised events; anything outside ``window`` is ignored.
        window: The aware ``(time_min, time_max)`` range being audited.
        prefs: Working hours, lunch, buffer and focus-block minimum.
        tzinfo: Zone the report's days and working hours are expressed in.
        group_by: ``'day'`` or ``'week'`` for the per-period breakdown.
        calendar_ids: Calendars the events came from, echoed in the result.
        include_all_day: Count all-day events as meeting time.
        include_declined: Count meetings the user declined.
        back_to_back_gap_minutes: Gap below which two meetings count as
            back-to-back. Defaults to ``prefs.buffer_minutes``, or 5 minutes
            when no buffer is configured.
        top_people: How many people to list in ``top_people``.

    Returns:
        The populated :class:`calendar_mcp.models.TimeAuditResult`.
    """
    group = "day" if str(group_by).lower() == "day" else "week"
    kept, exclusions = filter_events(
        events, window, include_all_day=include_all_day, include_declined=include_declined
    )

    booked = clip_intervals((event.interval for event in kept), window)
    total_hours = _hours(booked)
    merged = merge_intervals(booked)

    windows = working_windows(prefs, window[0], window[1], tzinfo=tzinfo)
    working_hours = _hours(windows)
    in_working = sum(_hours(clip_intervals(merged, window_)) for window_ in windows)
    overall_share = min(in_working / working_hours, 1.0) if working_hours > 0 else 0.0

    # -- per day, then per requested period --------------------------------
    spans = _day_spans(window, tzinfo)
    day_periods = [
        _measure_period(day.isoformat(), span, kept, prefs, tzinfo) for day, span in spans
    ]

    if group == "day":
        periods = day_periods
    else:
        grouped: Dict[Any, Tuple[str, datetime, datetime]] = {}
        for day, span in spans:
            key, label = _period_key(day, group)
            if key in grouped:
                _, start, end = grouped[key]
                grouped[key] = (label, min(start, span[0]), max(end, span[1]))
            else:
                grouped[key] = (label, span[0], span[1])
        periods = [
            _measure_period(label, (start, end), kept, prefs, tzinfo)
            for _, (label, start, end) in sorted(grouped.items(), key=lambda item: item[0])
        ]

    longest_day = max(day_periods, key=lambda period: period.meeting_hours, default=None)
    if longest_day is not None and longest_day.meeting_hours <= 0:
        longest_day = None
    busiest_period = max(periods, key=lambda period: period.meeting_hours, default=None)
    if busiest_period is not None and busiest_period.meeting_hours <= 0:
        busiest_period = None

    weekday_hours: Dict[int, float] = {}
    for day, span in spans:
        clipped = clip_intervals((event.interval for event in kept), span)
        weekday_hours[day.weekday()] = weekday_hours.get(day.weekday(), 0.0) + _hours(clipped)
    heaviest_weekday: Optional[Tuple[str, float]] = None
    if weekday_hours:
        index = max(weekday_hours, key=lambda key: weekday_hours[key])
        if weekday_hours[index] > 0:
            heaviest_weekday = (_WEEKDAY_NAMES[index], weekday_hours[index])

    # -- breakdowns --------------------------------------------------------
    by_size: Dict[str, Tuple[int, float]] = {}
    by_domain: Dict[str, Tuple[int, float]] = {}
    by_recurrence: Dict[str, Tuple[int, float]] = {}
    people: Dict[str, Tuple[int, float]] = {}

    def add(table: Dict[str, Tuple[int, float]], key: str, hours: float) -> None:
        count, total = table.get(key, (0, 0.0))
        table[key] = (count + 1, total + hours)

    for event in kept:
        hours = _hours(clip_intervals([event.interval], window))
        if hours <= 0:
            continue
        headcount = len({email.strip().lower() for email in event.attendees if email})
        add(by_size, size_bucket(headcount), hours)
        add(by_recurrence, "recurring" if event.is_recurring else "one-off", hours)
        others = event.others()
        for email in others:
            add(people, email, hours)
        for domain in sorted({split_email_domain(email) for email in others if "@" in email}):
            add(by_domain, domain, hours)

    ranked = sorted(people.items(), key=lambda item: (-item[1][1], item[0]))
    top = [
        AuditPerson(email=email, meeting_count=count, hours=_round(hours))
        for email, (count, hours) in ranked[: max(0, int(top_people))]
    ]

    # -- back-to-back and focus -------------------------------------------
    gap = back_to_back_gap_minutes
    if gap is None:
        gap = prefs.buffer_minutes or 5
    stretches = _back_to_back(kept, window, int(gap))

    blocks = _focus_blocks(kept, windows, prefs.buffer_minutes, prefs.min_focus_block_minutes)
    focus = [
        AuditFocusBlock(
            start=start.isoformat(),
            end=end.isoformat(),
            hours=_round(hours_between(start, end)),
        )
        for start, end in blocks
    ]
    largest = sorted(focus, key=lambda block: -block.hours)[:5]

    recurring_hours = by_recurrence.get("recurring", (0, 0.0))[1]
    recurring_share = (recurring_hours / total_hours) if total_hours > 0 else 0.0

    return TimeAuditResult(
        time_min=window[0].isoformat(),
        time_max=window[1].isoformat(),
        timezone=str(getattr(tzinfo, "key", tzinfo)),
        group_by=group,
        calendar_ids=list(calendar_ids or []),
        total_meeting_hours=_round(total_hours),
        total_meeting_count=len(kept),
        working_hours_available=_round(working_hours),
        meeting_hours_in_working_hours=_round(in_working),
        share_of_working_hours=_round(overall_share, 4),
        periods=periods,
        by_size=_buckets(by_size, total_hours),
        by_domain=_buckets(by_domain, total_hours),
        by_recurrence=_buckets(by_recurrence, total_hours),
        top_people=top,
        longest_meeting_day=longest_day,
        busiest_period=busiest_period,
        back_to_back_count=len(stretches),
        back_to_back_hours=_round(sum(item.hours for item in stretches)),
        back_to_back_stretches=stretches,
        focus_hours_available=_round(sum(block.hours for block in focus)),
        focus_block_count=len(focus),
        largest_focus_blocks=largest,
        excluded=exclusions,
        insights=_insights(
            overall_share,
            total_hours,
            heaviest_weekday,
            top[0] if top else None,
            recurring_share,
            len(stretches),
            _round(sum(block.hours for block in focus)),
            largest[0] if largest else None,
        ),
    )
