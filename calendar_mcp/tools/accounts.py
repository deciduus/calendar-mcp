"""Tools for the local configuration: signed-in accounts and scheduling preferences.

Nothing here calls Google. ``list_accounts`` reads the token directory,
``get_preferences``/``set_preferences`` read and write one JSON file, so all
three answer instantly and work even when every token has expired.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import ValidationError

from calendar_mcp import accounts as accounts_module
from calendar_mcp import preferences as preferences_module
from calendar_mcp import server as srv
from calendar_mcp.models import AccountListResult
from calendar_mcp.preferences import Preferences, TimeRange


def _readable_validation_error(exc: ValidationError) -> str:
    """Turns a pydantic ValidationError into one line per bad field."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "value"
        message = str(error.get("msg", "is invalid"))
        # pydantic prefixes custom validator messages with "Value error, ".
        if message.startswith("Value error, "):
            message = message[len("Value error, "):]
        parts.append(f"{location}: {message}")
    return "; ".join(parts) or str(exc)


@srv.server.tool(
    name="list_accounts",
    title="List signed-in accounts",
    annotations=srv.LOCAL_READ,
)
async def list_accounts(
    ctx: Optional[srv.Context] = None,
) -> AccountListResult:
    """List the Google accounts calendar-mcp can use, and which is the default.

    Every other tool takes an optional `account` argument naming one of these.
    An account whose `valid` is false needs `calendar-mcp auth --account <name>`
    run once in a terminal before its calendars can be read.
    """

    def work() -> AccountListResult:
        infos = accounts_module.list_accounts()
        return AccountListResult(
            count=len(infos),
            default_account=accounts_module.resolve_account(None),
            config_dir=str(accounts_module.config_dir()),
            accounts=infos,
        )

    return await srv._run(work)


@srv.server.tool(
    name="get_preferences",
    title="Get scheduling preferences",
    annotations=srv.LOCAL_READ,
)
async def get_preferences(
    ctx: Optional[srv.Context] = None,
) -> Preferences:
    """Read the user's scheduling preferences: working hours, lunch, buffers, focus.

    Consult this before proposing meeting times or hunting for focus blocks,
    rather than assuming a nine-to-five. Never configured returns the defaults
    (Mon-Fri 09:00-17:00, no lunch break, no buffer), which is a reasonable
    guess but worth confirming with the user before you rely on it.
    """

    def work() -> Preferences:
        return preferences_module.load()

    return await srv._run(work)


@srv.server.tool(
    name="set_preferences",
    title="Set scheduling preferences",
    annotations=srv.LOCAL_WRITE,
)
async def set_preferences(
    timezone: Optional[str] = None,
    working_hours: Optional[Dict[str, List[TimeRange]]] = None,
    buffer_minutes: Optional[int] = None,
    min_focus_block_minutes: Optional[int] = None,
    lunch: Optional[TimeRange] = None,
    focus_calendar_id: Optional[str] = None,
    clear_lunch: bool = False,
    ctx: Optional[srv.Context] = None,
) -> Preferences:
    """Update the user's scheduling preferences and save them. Returns the merged result.

    Only the arguments you pass change; everything else keeps its current value,
    so a single correction ("I actually start at 8") does not have to restate
    the whole schedule. The merged result is validated before it is written, so
    a rejected change leaves the saved preferences untouched.

    Args:
        timezone: IANA timezone the working hours are expressed in, e.g.
            'Europe/Berlin'. Must be a real zone name.
        working_hours: Whole-schedule replacement, keyed by weekday
            ('mon'..'sun'), each value a list of ['HH:MM', 'HH:MM'] pairs. Days
            you omit become non-working, so pass every working day at once, e.g.
            {"mon": [["09:00", "12:00"], ["13:00", "18:00"]], "fri": [["09:00", "15:00"]]}.
        buffer_minutes: Minimum gap to leave before and after each meeting when
            proposing times. 0 disables it.
        min_focus_block_minutes: Shortest free stretch that still counts as
            usable focus time.
        lunch: Daily lunch break as ['HH:MM', 'HH:MM'], carved out of the
            working hours.
        focus_calendar_id: Calendar that focus blocks are booked on; 'primary'
            for the user's own.
        clear_lunch: Set true to remove an existing lunch break (pass this
            instead of `lunch` when the user says they no longer want one).
    """

    def work() -> Preferences:
        updates = {
            "timezone": timezone,
            "working_hours": working_hours,
            "buffer_minutes": buffer_minutes,
            "min_focus_block_minutes": min_focus_block_minutes,
            "lunch": list(lunch) if lunch is not None else None,
            "focus_calendar_id": focus_calendar_id,
        }
        if not clear_lunch and all(value is None for value in updates.values()):
            raise srv.CalendarToolError(
                "Nothing to change: pass at least one of timezone, working_hours, "
                "buffer_minutes, min_focus_block_minutes, lunch, focus_calendar_id "
                "or clear_lunch."
            )
        if clear_lunch and lunch is not None:
            raise srv.CalendarToolError("Pass either lunch or clear_lunch, not both.")

        current = preferences_module.load()
        try:
            merged = preferences_module.merge(current, **updates)
            if clear_lunch:
                merged = merged.model_copy(update={"lunch": None})
        except ValidationError as exc:
            raise srv.CalendarToolError(
                f"Those preferences are not valid -- {_readable_validation_error(exc)}"
            ) from exc
        except ValueError as exc:
            raise srv.CalendarToolError(f"Those preferences are not valid -- {exc}") from exc

        preferences_module.save(merged)
        return merged

    return await srv._run(work)
