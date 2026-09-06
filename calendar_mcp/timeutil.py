"""Timezone- and interval-arithmetic helpers shared by the scheduling tools.

Everything in this module is pure: no Google client, no credentials, no disk.
That makes it safe to import from anywhere (including
:mod:`calendar_mcp.preferences`) and cheap to unit-test.

The credential-aware timestamp helpers -- :func:`calendar_mcp.server.parse_datetime`
and :func:`calendar_mcp.server._default_tzinfo` -- stay in
:mod:`calendar_mcp.server`, because they need the calendar-timezone lookup and
its per-process cache. Import them from there; import the interval maths from
here.
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

__all__ = [
    "Interval",
    "parse_clock",
    "format_clock",
    "merge_intervals",
    "subtract_intervals",
    "clip_intervals",
    "free_windows",
    "iter_days",
    "combine",
]

#: An aware (start, end) pair. Every helper here assumes ``start <= end`` and
#: that both ends carry a tzinfo; comparisons across zones are fine.
Interval = Tuple[datetime, datetime]


# ---------------------------------------------------------------------------
# Clock strings
# ---------------------------------------------------------------------------


def parse_clock(value: Optional[str], field: str = "time") -> Optional[dt_time]:
    """Parses an ``'HH:MM'`` wall-clock string into a :class:`datetime.time`.

    Args:
        value: The string to parse. ``None`` or ``''`` returns ``None``.
        field: Name used in the error message.

    Returns:
        The parsed time, or ``None`` when ``value`` is empty.

    Raises:
        ValueError: If the string is not ``HH:MM`` in range.
    """
    if not value:
        return None
    text = str(value).strip()
    hour_text, sep, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text) if sep else 0
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must look like 'HH:MM'; got {value!r}.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field} must be between '00:00' and '23:59'; got {value!r}.")
    return dt_time(hour, minute)


def format_clock(value: dt_time) -> str:
    """Renders a :class:`datetime.time` back as ``'HH:MM'``."""
    return f"{value.hour:02d}:{value.minute:02d}"


# ---------------------------------------------------------------------------
# Interval arithmetic
# ---------------------------------------------------------------------------


def merge_intervals(intervals: Iterable[Interval]) -> List[Interval]:
    """Sorts and coalesces overlapping or touching intervals.

    Zero-length and inverted intervals are dropped.
    """
    ordered = sorted(
        ((start, end) for start, end in intervals if end > start),
        key=lambda pair: pair[0],
    )
    merged: List[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(base: Interval, cuts: Iterable[Interval]) -> List[Interval]:
    """Removes ``cuts`` from the single interval ``base``.

    Returns the remaining pieces in chronological order (possibly empty).
    """
    start, end = base
    if end <= start:
        return []
    remaining: List[Interval] = []
    cursor = start
    for cut_start, cut_end in merge_intervals(cuts):
        if cut_end <= cursor:
            continue
        if cut_start >= end:
            break
        if cut_start > cursor:
            remaining.append((cursor, min(cut_start, end)))
        cursor = max(cursor, cut_end)
        if cursor >= end:
            break
    if cursor < end:
        remaining.append((cursor, end))
    return [(a, b) for a, b in remaining if b > a]


def clip_intervals(intervals: Iterable[Interval], window: Interval) -> List[Interval]:
    """Trims every interval to ``window``, dropping the ones that fall outside."""
    low, high = window
    clipped: List[Interval] = []
    for start, end in intervals:
        new_start, new_end = max(start, low), min(end, high)
        if new_end > new_start:
            clipped.append((new_start, new_end))
    return clipped


def free_windows(
    busy: Sequence[Interval],
    window: Interval,
    buffer_minutes: int = 0,
) -> List[Interval]:
    """Returns the gaps inside ``window`` that no busy interval covers.

    Each busy interval is first grown by ``buffer_minutes`` on both sides, so a
    caller that wants breathing room around meetings gets free windows that
    already respect it. The result is clipped to ``window`` and merged, so the
    pieces are disjoint and in chronological order.

    Args:
        busy: Busy ``(start, end)`` pairs, in any order; overlaps are fine.
        window: The ``(start, end)`` range to search inside.
        buffer_minutes: Minimum gap to leave before and after each busy block.

    Returns:
        The free ``(start, end)`` pairs, earliest first. Empty when the window
        is fully booked (or inverted).
    """
    pad = timedelta(minutes=max(0, int(buffer_minutes or 0)))
    padded = [(start - pad, end + pad) for start, end in busy if end > start]
    return subtract_intervals(window, padded)


# ---------------------------------------------------------------------------
# Day iteration
# ---------------------------------------------------------------------------


def iter_days(time_min: datetime, time_max: datetime, tzinfo) -> Iterator[date]:
    """Yields every local calendar date touched by ``[time_min, time_max)``.

    Both bounds are converted into ``tzinfo`` first, so "which days does this
    window cover" is answered in the user's own zone rather than in UTC.
    """
    if time_max <= time_min:
        return
    start = time_min.astimezone(tzinfo).date()
    end = time_max.astimezone(tzinfo).date()
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def combine(day: date, clock: dt_time, tzinfo) -> datetime:
    """Builds an aware datetime for ``clock`` on ``day`` in ``tzinfo``."""
    return datetime.combine(day, clock).replace(tzinfo=tzinfo)
