"""User scheduling preferences: working hours, buffers, lunch, focus time.

One JSON file, ``<config dir>/preferences.json`` (see
:func:`calendar_mcp.accounts.config_dir`), holds the answers to the questions
every scheduling tool otherwise has to ask again: when does this person work,
how much air do they want around a meeting, when do they eat, how long does a
block have to be before it counts as focus time.

Preferences are global rather than per-account on purpose: they describe the
human, not the mailbox.

Reading is cheap and always succeeds -- a missing or corrupt file yields
:class:`Preferences` defaults (Mon-Fri 09:00-17:00) rather than an error, so no
tool has to special-case a first run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from . import accounts as accounts_module
from .timeutil import Interval, combine, format_clock, iter_days, parse_clock, subtract_intervals

logger = logging.getLogger(__name__)

PREFERENCES_FILE = "preferences.json"

#: A wall-clock ``('HH:MM', 'HH:MM')`` span, interpreted in the user's timezone.
TimeRange = Tuple[str, str]

#: Weekday keys, indexed by :meth:`datetime.date.weekday` (0 = Monday).
WEEKDAYS: Tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_ALIASES = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
    "m": "mon", "tu": "tue", "w": "wed", "th": "thu", "f": "fri",
    "sa": "sat", "su": "sun",
}

_write_lock = threading.Lock()


def default_working_hours() -> Dict[str, List[TimeRange]]:
    """The out-of-the-box schedule: Mon-Fri 09:00-17:00, weekends off."""
    return {
        "mon": [("09:00", "17:00")],
        "tue": [("09:00", "17:00")],
        "wed": [("09:00", "17:00")],
        "thu": [("09:00", "17:00")],
        "fri": [("09:00", "17:00")],
        "sat": [],
        "sun": [],
    }


def normalise_weekday(key: str) -> str:
    """Maps ``'Monday'``/``'MON'``/``'m'`` onto the canonical ``'mon'``.

    Raises:
        ValueError: If the key names no weekday.
    """
    text = str(key).strip().lower()
    if text in WEEKDAYS:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    raise ValueError(
        f"Unknown weekday {key!r}. Use one of: {', '.join(WEEKDAYS)}."
    )


def _clean_ranges(value, weekday: str) -> List[TimeRange]:
    """Validates one day's list of ``('HH:MM', 'HH:MM')`` spans."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"working_hours['{weekday}'] must be a list of ['HH:MM', 'HH:MM'] pairs."
        )
    cleaned: List[TimeRange] = []
    for item in value:
        if isinstance(item, (str, bytes)) or len(tuple(item)) != 2:
            raise ValueError(
                f"working_hours['{weekday}'] entries must be ['HH:MM', 'HH:MM'] pairs; "
                f"got {item!r}."
            )
        raw_start, raw_end = tuple(item)
        start = parse_clock(str(raw_start), f"working_hours['{weekday}'] start")
        end = parse_clock(str(raw_end), f"working_hours['{weekday}'] end")
        if start is None or end is None:
            raise ValueError(f"working_hours['{weekday}'] entries need both a start and an end.")
        if end <= start:
            raise ValueError(
                f"working_hours['{weekday}'] end must be after start; got "
                f"{format_clock(start)}-{format_clock(end)}."
            )
        cleaned.append((format_clock(start), format_clock(end)))
    cleaned.sort()
    return cleaned


class Preferences(BaseModel):
    """How this user wants their time scheduled."""

    timezone: Optional[str] = Field(
        None,
        description="IANA timezone the working hours are expressed in, e.g. 'Europe/Berlin'. "
        "Omit to use the calendar's own timezone.",
    )
    working_hours: Dict[str, List[TimeRange]] = Field(
        default_factory=default_working_hours,
        description="Per-weekday working spans, keyed 'mon'..'sun', each a list of "
        "['HH:MM', 'HH:MM'] pairs. An empty list means the day is off.",
    )
    buffer_minutes: int = Field(
        0,
        ge=0,
        le=240,
        description="Minimum gap to leave before and after each meeting when scheduling.",
    )
    min_focus_block_minutes: int = Field(
        60,
        ge=0,
        le=1440,
        description="Shortest free stretch that still counts as usable focus time.",
    )
    lunch: Optional[TimeRange] = Field(
        None,
        description="Daily lunch break as ['HH:MM', 'HH:MM'], carved out of the working hours. "
        "Omit for no lunch break.",
    )
    focus_calendar_id: str = Field(
        "primary",
        description="Calendar that focus blocks are booked on.",
    )

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        name = str(value).strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(
                f"timezone {name!r} is not a known IANA zone (e.g. 'America/New_York')."
            ) from exc
        return name

    @field_validator("working_hours", mode="before")
    @classmethod
    def _check_working_hours(cls, value):
        if value is None:
            return default_working_hours()
        if not isinstance(value, dict):
            raise ValueError("working_hours must be an object keyed by weekday ('mon'..'sun').")
        cleaned: Dict[str, List[TimeRange]] = {day: [] for day in WEEKDAYS}
        for key, spans in value.items():
            day = normalise_weekday(key)
            cleaned[day] = _clean_ranges(spans, day)
        return cleaned

    @field_validator("lunch", mode="before")
    @classmethod
    def _check_lunch(cls, value):
        if value is None:
            return None
        if isinstance(value, (str, bytes)) or len(tuple(value)) != 2:
            raise ValueError("lunch must be a ['HH:MM', 'HH:MM'] pair, or omitted.")
        raw_start, raw_end = tuple(value)
        start = parse_clock(str(raw_start), "lunch start")
        end = parse_clock(str(raw_end), "lunch end")
        if start is None or end is None:
            raise ValueError("lunch needs both a start and an end.")
        if end <= start:
            raise ValueError("lunch end must be after lunch start.")
        return (format_clock(start), format_clock(end))

    @field_validator("focus_calendar_id")
    @classmethod
    def _check_focus_calendar(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("focus_calendar_id cannot be empty; use 'primary' for the main calendar.")
        return text

    # -- convenience -------------------------------------------------------

    def tzinfo(self, fallback=None):
        """The configured timezone as a tzinfo, or ``fallback`` when unset."""
        if self.timezone:
            try:
                return ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - validated on write
                logger.warning("Preferred timezone %r is not resolvable.", self.timezone)
        return fallback

    def spans_for(self, weekday_index: int) -> List[TimeRange]:
        """The working spans for a ``date.weekday()`` index (0 = Monday)."""
        return list(self.working_hours.get(WEEKDAYS[weekday_index % 7], []))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def preferences_path() -> str:
    """Absolute path of ``preferences.json``."""
    return str(accounts_module.config_dir() / PREFERENCES_FILE)


def load() -> Preferences:
    """Reads the saved preferences, falling back to defaults.

    A missing file is normal. An unreadable or invalid one is logged and
    ignored rather than raised, so a bad edit cannot wedge every tool.
    """
    path = preferences_path()
    if not os.path.exists(path):
        return Preferences()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s (%s); using default preferences.", path, exc)
        return Preferences()
    try:
        return Preferences.model_validate(data)
    except Exception as exc:
        logger.warning("%s is not valid (%s); using default preferences.", path, exc)
        return Preferences()


def save(prefs: Preferences) -> str:
    """Writes ``prefs`` to ``preferences.json`` and returns the path.

    The write is atomic: a temp file in the same directory is replaced into
    position, so a crash mid-write cannot leave a half-written file behind.
    """
    directory = accounts_module.ensure_config_dir()
    target = directory / PREFERENCES_FILE
    payload = json.dumps(prefs.model_dump(mode="json"), indent=2, sort_keys=True)
    with _write_lock:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(directory), prefix=".preferences-", suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(payload + "\n")
            os.replace(handle.name, target)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:  # pragma: no cover - best effort
                pass
            raise
    logger.info("Saved preferences to %s", target)
    return str(target)


def merge(current: Preferences, **updates) -> Preferences:
    """Returns a copy of ``current`` with the non-``None`` ``updates`` applied.

    Validation runs on the merged whole, so a rejected update leaves the saved
    preferences untouched.

    Raises:
        ValueError: If the merged result is not valid (pydantic's message says
            which field).
    """
    data = current.model_dump(mode="json")
    for key, value in updates.items():
        if value is None:
            continue
        data[key] = value
    return Preferences.model_validate(data)


# ---------------------------------------------------------------------------
# Working windows
# ---------------------------------------------------------------------------


def working_windows(
    prefs: Preferences,
    time_min: datetime,
    time_max: datetime,
    tzinfo=None,
) -> List[Interval]:
    """The user's working time inside ``[time_min, time_max)``, lunch removed.

    Each day in the range contributes its configured spans, expressed in
    ``tzinfo`` (or ``prefs.timezone``, or the timezone of ``time_min``), minus
    the lunch break. The result is clipped to the requested window, so a caller
    can hand it straight to :func:`calendar_mcp.timeutil.free_windows`.

    Args:
        prefs: The preferences to read working hours and lunch from.
        time_min: Inclusive start of the range, aware.
        time_max: Exclusive end of the range, aware.
        tzinfo: Zone the wall-clock hours are read in. Defaults to
            ``prefs.timezone`` when set, else ``time_min``'s own zone.

    Returns:
        Aware ``(start, end)`` pairs, earliest first. Empty when the range holds
        no working time.
    """
    zone = tzinfo or prefs.tzinfo(time_min.tzinfo)
    if zone is None:  # pragma: no cover - callers pass aware datetimes
        raise ValueError("working_windows needs a timezone: pass tzinfo or set prefs.timezone.")

    lunch_clocks: Optional[Tuple[dt_time, dt_time]] = None
    if prefs.lunch:
        lunch_start = parse_clock(prefs.lunch[0], "lunch start")
        lunch_end = parse_clock(prefs.lunch[1], "lunch end")
        if lunch_start and lunch_end:
            lunch_clocks = (lunch_start, lunch_end)

    windows: List[Interval] = []
    for day in iter_days(time_min, time_max, zone):
        for raw_start, raw_end in prefs.spans_for(day.weekday()):
            start_clock = parse_clock(raw_start, "working hours start")
            end_clock = parse_clock(raw_end, "working hours end")
            if start_clock is None or end_clock is None:  # pragma: no cover - validated
                continue
            span = (combine(day, start_clock, zone), combine(day, end_clock, zone))
            pieces = [span]
            if lunch_clocks is not None:
                cut = (combine(day, lunch_clocks[0], zone), combine(day, lunch_clocks[1], zone))
                pieces = subtract_intervals(span, [cut])
            for piece_start, piece_end in pieces:
                clipped_start = max(piece_start, time_min)
                clipped_end = min(piece_end, time_max)
                if clipped_end > clipped_start:
                    windows.append((clipped_start, clipped_end))

    windows.sort(key=lambda pair: pair[0])
    return windows


__all__ = [
    "PREFERENCES_FILE",
    "Preferences",
    "TimeRange",
    "WEEKDAYS",
    "default_working_hours",
    "load",
    "merge",
    "normalise_weekday",
    "preferences_path",
    "save",
    "working_windows",
]
