"""Shared fail-closed validation before reasoning about calendar availability."""

from datetime import datetime
from typing import Any, Dict, List, Optional


def require_complete_availability(
    raw: Optional[Dict[str, Dict[str, Any]]], calendar_ids: List[str]
) -> None:
    """Reject unknown availability instead of interpreting it as an empty calendar.

    An empty busy list is a successful free response. Missing calendars, API
    errors and malformed intervals are not. Validate all requested calendars
    before any caller selects a slot or writes an event.
    """
    problems = []
    if raw is None:
        problems.append("free/busy query returned no result")
    else:
        for calendar_id in dict.fromkeys(calendar_ids):
            data = raw.get(calendar_id)
            if not isinstance(data, dict):
                problems.append(f"{calendar_id}: missing free/busy response")
                continue
            for error in data.get("errors") or []:
                reason = error.get("reason", str(error)) if isinstance(error, dict) else str(error)
                problems.append(f"{calendar_id}: {reason}")
            intervals = data.get("busy")
            if not isinstance(intervals, list):
                problems.append(f"{calendar_id}: missing or malformed busy intervals")
                continue
            for interval in intervals:
                start = interval.get("start") if isinstance(interval, dict) else None
                end = interval.get("end") if isinstance(interval, dict) else None
                if (
                    not isinstance(start, datetime) or not isinstance(end, datetime)
                    or start.utcoffset() is None or end.utcoffset() is None
                    or end <= start
                ):
                    problems.append(f"{calendar_id}: malformed busy interval")
                    break
    if problems:
        raise ValueError(
            "Availability is unknown: " + "; ".join(problems)
            + ". No scheduling changes were made. Restore calendar access or retry the query."
        )
