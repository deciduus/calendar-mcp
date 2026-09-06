"""Pure scheduling logic: focus blocks, conflict detection and slot ranking.

Everything here is a function of its arguments: no credentials, no Google
client, no disk, no clock. The tools in :mod:`calendar_mcp.tools.focus` and
:mod:`calendar_mcp.tools.conflicts` fetch the raw material (working windows from
:mod:`calendar_mcp.preferences`, busy intervals from Google) and hand it to
these helpers, which is what makes the interesting behaviour unit-testable
without a network.

The vocabulary is the one :mod:`calendar_mcp.timeutil` established: an
*interval* is an aware ``(start, end)`` pair with ``start <= end``. Events are
addressed by *index* into the caller's own list -- these functions never see an
event, only its span -- so the caller keeps ownership of titles, IDs and
calendars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .timeutil import Interval, free_windows, merge_intervals

__all__ = [
    "ScoredSlot",
    "BASE_SCORE",
    "CONFLICT_PENALTY",
    "SAME_DAY_BONUS",
    "BUFFER_PENALTY",
    "interval_minutes",
    "total_minutes",
    "total_hours",
    "overlap_minutes",
    "candidate_blocks",
    "rank_blocks",
    "select_blocks",
    "overlapping_pairs",
    "tight_pairs",
    "align_up",
    "generate_slots",
    "conflicting_keys",
    "violates_buffer",
    "rank_slots",
]


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


def interval_minutes(interval: Interval) -> float:
    """Length of one interval in minutes (0.0 when inverted)."""
    start, end = interval
    return max(0.0, (end - start).total_seconds() / 60.0)


def total_minutes(intervals: Iterable[Interval]) -> float:
    """Summed length of ``intervals`` in minutes. Overlaps are merged away first."""
    return sum(interval_minutes(item) for item in merge_intervals(intervals))


def total_hours(intervals: Iterable[Interval]) -> float:
    """Summed length of ``intervals`` in hours, rounded to two decimals."""
    return round(total_minutes(intervals) / 60.0, 2)


def overlap_minutes(first: Interval, second: Interval) -> float:
    """Minutes the two intervals share. 0.0 when they merely touch."""
    start = max(first[0], second[0])
    end = min(first[1], second[1])
    return max(0.0, (end - start).total_seconds() / 60.0)


# ---------------------------------------------------------------------------
# Focus blocks
# ---------------------------------------------------------------------------


def candidate_blocks(
    windows: Sequence[Interval],
    busy: Sequence[Interval],
    min_block_minutes: int = 0,
    buffer_minutes: int = 0,
) -> List[Interval]:
    """The usable free stretches inside ``windows``.

    Each working window has the busy intervals cut out of it -- grown by
    ``buffer_minutes`` on both sides first, so a block never starts the instant
    a meeting ends -- and whatever survives is kept when it is at least
    ``min_block_minutes`` long.

    Args:
        windows: Working windows to search inside, e.g. from
            :func:`calendar_mcp.preferences.working_windows`.
        busy: Busy intervals across every calendar that matters, in any order.
        min_block_minutes: Shortest stretch that still counts as focus time.
        buffer_minutes: Breathing room to leave around each busy interval.

    Returns:
        Disjoint ``(start, end)`` pairs in chronological order.
    """
    blocks: List[Interval] = []
    for window in windows:
        if window[1] <= window[0]:
            continue
        for piece in free_windows(busy, window, buffer_minutes):
            if interval_minutes(piece) + 1e-9 >= float(min_block_minutes):
                blocks.append(piece)
    blocks.sort(key=lambda pair: pair[0])
    return blocks


def rank_blocks(blocks: Iterable[Interval]) -> List[Interval]:
    """Orders blocks the way a person picks focus time: longest first, then earliest."""
    return sorted(blocks, key=lambda pair: (-interval_minutes(pair), pair[0]))


def select_blocks(
    blocks: Sequence[Interval],
    hours_needed: float,
    min_block_minutes: int = 0,
) -> List[Interval]:
    """Takes blocks, longest first, until ``hours_needed`` is covered.

    The last block taken is trimmed to what is still needed, rather than booking
    a whole afternoon for a final twenty minutes -- but never trimmed below
    ``min_block_minutes``, so every block handed back is still a usable one.

    Args:
        blocks: Candidate blocks, e.g. from :func:`candidate_blocks`.
        hours_needed: How much focus time to gather. ``0`` selects nothing.
        min_block_minutes: Floor for the trimmed final block.

    Returns:
        The chosen blocks in chronological order. Adds up to less than
        ``hours_needed`` when the candidates simply do not stretch that far.
    """
    remaining = max(0.0, float(hours_needed)) * 60.0
    chosen: List[Interval] = []
    for start, end in rank_blocks(blocks):
        if remaining <= 1e-9:
            break
        available = interval_minutes((start, end))
        take = min(available, max(remaining, float(min_block_minutes)))
        chosen.append((start, start + timedelta(minutes=take)))
        remaining -= take
    chosen.sort(key=lambda pair: pair[0])
    return chosen


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def overlapping_pairs(intervals: Sequence[Interval]) -> List[Tuple[int, int, float]]:
    """Finds every pair of intervals that genuinely overlap.

    Args:
        intervals: Event spans, in any order. Indices into this sequence are
            what the result refers to.

    Returns:
        ``(earlier_index, later_index, overlap_minutes)`` triples, ordered by
        the earlier event's start. Touching intervals -- one ending exactly when
        the next begins -- are not overlaps.
    """
    order = sorted(range(len(intervals)), key=lambda i: (intervals[i][0], intervals[i][1]))
    pairs: List[Tuple[int, int, float]] = []
    for position, index in enumerate(order):
        start, end = intervals[index]
        for other in order[position + 1:]:
            # Starts are non-decreasing, so once one begins at or after this
            # event ends, every later one does too.
            if intervals[other][0] >= end:
                break
            shared = overlap_minutes((start, end), intervals[other])
            if shared > 0:
                pairs.append((index, other, shared))
    return pairs


def tight_pairs(
    intervals: Sequence[Interval],
    buffer_minutes: int,
) -> List[Tuple[int, int, float]]:
    """Finds back-to-back pairs that leave less than ``buffer_minutes`` between them.

    These are not conflicts -- nothing is double-booked -- but they are the
    transitions that make a day feel impossible, so they are worth reporting
    separately.

    Returns:
        ``(earlier_index, later_index, gap_minutes)`` triples, ordered by the
        earlier event's start. Empty when ``buffer_minutes`` is zero or less.
    """
    if not buffer_minutes or buffer_minutes <= 0:
        return []
    order = sorted(range(len(intervals)), key=lambda i: (intervals[i][0], intervals[i][1]))
    pairs: List[Tuple[int, int, float]] = []
    for position, index in enumerate(order):
        end = intervals[index][1]
        for other in order[position + 1:]:
            gap = (intervals[other][0] - end).total_seconds() / 60.0
            if gap < 0:
                continue  # overlapping: a conflict, not a tight transition
            if gap >= buffer_minutes:
                break  # starts only get later, so every later gap is bigger
            pairs.append((index, other, gap))
    return pairs


# ---------------------------------------------------------------------------
# Slot generation and ranking
# ---------------------------------------------------------------------------


def align_up(moment: datetime, step_minutes: int) -> datetime:
    """Rounds ``moment`` up to the next ``step_minutes`` mark past the hour.

    Keeps proposed times on the boundaries people expect -- :00, :15, :30, :45
    for a 15-minute step -- instead of at 09:07 because that is when a meeting
    happened to end.
    """
    if step_minutes <= 0:
        return moment
    trimmed = moment.replace(second=0, microsecond=0)
    if trimmed < moment:
        trimmed += timedelta(minutes=1)
    remainder = trimmed.minute % step_minutes
    if remainder:
        trimmed += timedelta(minutes=step_minutes - remainder)
    return trimmed


def generate_slots(
    windows: Sequence[Interval],
    duration_minutes: int,
    step_minutes: int = 15,
    limit: Optional[int] = None,
) -> List[Interval]:
    """Every ``duration_minutes`` slot that fits inside ``windows``, on a grid.

    Args:
        windows: Windows the slot has to sit inside (typically working hours).
        duration_minutes: Length of the slot to place.
        step_minutes: Spacing of the candidate start times.
        limit: Stop after this many candidates. ``None`` for all of them.

    Returns:
        Candidate ``(start, end)`` pairs in chronological order.
    """
    if duration_minutes <= 0:
        return []
    length = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=max(1, step_minutes))
    slots: List[Interval] = []
    for window_start, window_end in sorted(windows, key=lambda pair: pair[0]):
        cursor = align_up(window_start, step_minutes)
        while cursor + length <= window_end:
            slots.append((cursor, cursor + length))
            if limit is not None and len(slots) >= limit:
                return slots
            cursor += step
    return slots


def conflicting_keys(
    slot: Interval,
    busy_by_key: Mapping[str, Sequence[Interval]],
) -> List[str]:
    """The keys of ``busy_by_key`` whose intervals overlap ``slot``, sorted."""
    clashing = [
        key
        for key, intervals in busy_by_key.items()
        if any(overlap_minutes(slot, interval) > 0 for interval in intervals)
    ]
    return sorted(clashing)


def violates_buffer(
    slot: Interval,
    busy: Iterable[Interval],
    buffer_minutes: int,
) -> bool:
    """True when ``slot`` sits closer than ``buffer_minutes`` to a busy interval.

    Overlaps do not count here: an overlap is a conflict, which the caller
    reports separately and weighs far more heavily.
    """
    if not buffer_minutes or buffer_minutes <= 0:
        return False
    start, end = slot
    for busy_start, busy_end in busy:
        if overlap_minutes(slot, (busy_start, busy_end)) > 0:
            continue
        if busy_end <= start:
            gap = (start - busy_end).total_seconds() / 60.0
        elif busy_start >= end:
            gap = (busy_start - end).total_seconds() / 60.0
        else:  # pragma: no cover - an overlap, already skipped above
            continue
        if gap < buffer_minutes:
            return True
    return False


@dataclass(frozen=True)
class ScoredSlot:
    """One ranked candidate time, with the reasoning that produced its score."""

    start: datetime
    end: datetime
    score: float
    conflicts: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()


#: Score every candidate starts from, before penalties and bonuses.
BASE_SCORE = 100.0
#: Cost of one attendee being busy during the slot.
CONFLICT_PENALTY = 25.0
#: Reward for landing on the day the meeting is already on.
SAME_DAY_BONUS = 10.0
#: Cost of sitting closer to another meeting than the user's buffer allows.
BUFFER_PENALTY = 8.0


def rank_slots(
    candidates: Sequence[Interval],
    busy_by_key: Optional[Mapping[str, Sequence[Interval]]] = None,
    unavailable: Sequence[Interval] = (),
    original_start: Optional[datetime] = None,
    buffer_minutes: int = 0,
    tzinfo=None,
    limit: Optional[int] = None,
) -> List[ScoredSlot]:
    """Scores candidate slots and returns the best ones, best first.

    Ranking, in the order a person would argue it: fewest attendee conflicts
    first, then the day the meeting is already on, then earliest. A slot that
    leaves less than the user's buffer against an adjacent meeting is still
    offered, but penalised.

    Args:
        candidates: Slots to consider, e.g. from :func:`generate_slots`.
        busy_by_key: Busy intervals per attendee. The key is whatever the caller
            wants to read back in ``conflicts`` -- usually an email address.
        unavailable: Hard blocks: a slot overlapping one of these is dropped
            entirely. Use it for the organiser's own calendar.
        original_start: Where the meeting currently sits, for the same-day bonus.
        buffer_minutes: The user's preferred gap around meetings.
        tzinfo: Zone the "same day" comparison is made in. Defaults to
            ``original_start``'s own zone.
        limit: Keep only this many results.

    Returns:
        :class:`ScoredSlot` values, highest score first and earliest within a
        score.
    """
    busy_map: Dict[str, Sequence[Interval]] = dict(busy_by_key or {})
    blocked = list(unavailable)
    zone = tzinfo or (original_start.tzinfo if original_start else None)
    original_day = original_start.astimezone(zone).date() if original_start and zone else None

    neighbours: List[Interval] = list(blocked)
    for intervals in busy_map.values():
        neighbours.extend(intervals)

    scored: List[ScoredSlot] = []
    for slot in candidates:
        if any(overlap_minutes(slot, block) > 0 for block in blocked):
            continue
        conflicts = conflicting_keys(slot, busy_map)
        score = BASE_SCORE - CONFLICT_PENALTY * len(conflicts)
        reasons: List[str] = []

        if busy_map and not conflicts:
            reasons.append(f"all {len(busy_map)} attendee(s) free")
        elif len(conflicts) == 1:
            reasons.append(f"busy: {conflicts[0]}")
        elif conflicts:
            reasons.append(f"{len(conflicts)} attendees busy")

        if original_day is not None and zone is not None:
            if slot[0].astimezone(zone).date() == original_day:
                score += SAME_DAY_BONUS
                reasons.append("same day as the original")

        if violates_buffer(slot, neighbours, buffer_minutes):
            score -= BUFFER_PENALTY
            reasons.append(f"less than {buffer_minutes} min from another meeting")

        scored.append(
            ScoredSlot(
                start=slot[0],
                end=slot[1],
                score=round(score, 2),
                conflicts=tuple(conflicts),
                reasons=tuple(reasons),
            )
        )

    scored.sort(key=lambda item: (-item.score, item.start))
    return scored[:limit] if limit is not None else scored
