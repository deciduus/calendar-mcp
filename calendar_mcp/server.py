"""The calendar-mcp MCP server: Google Calendar as MCP tools.

Single process, no HTTP hop. Every tool calls :mod:`calendar_mcp.calendar_actions`
directly, and returns a pydantic model so the MCP client gets structured output.

Google credentials are loaded lazily, on the first tool call that needs them, so
the MCP handshake answers instantly and a missing token surfaces as a readable
tool error rather than a hung startup.

**This module holds the plumbing, not the tools.** The tools themselves live in
:mod:`calendar_mcp.tools`, one module per subject area, and register themselves
against the ``server`` instance created here when that package is imported (the
last line of this file). To add a tool, write it in a ``calendar_mcp/tools/``
module -- there is no need to touch this file:

.. code-block:: python

    # calendar_mcp/tools/my_area.py
    from typing import Optional

    from calendar_mcp import server as srv
    from calendar_mcp.models import EventListResult


    @srv.server.tool(name="my_tool", title="My tool", annotations=srv.READ_ONLY)
    async def my_tool(
        calendar_id: str = "primary",
        account: Optional[str] = None,
        ctx: Optional[srv.Context] = None,
    ) -> EventListResult:
        \"\"\"One-line summary the model sees.

        Args:
            calendar_id: Calendar to read.
            account: Account name from 'calendar-mcp accounts'; omit for the default.
        \"\"\"
        provider = srv._provider(ctx)

        def work() -> EventListResult:
            creds = provider.get(account)
            ...

        return await srv._run(work)

...then add the module to the import list in ``calendar_mcp/tools/__init__.py``.
Reach Google through ``srv.calendar_actions`` rather than importing
``calendar_actions`` directly: the test suite patches that attribute on this
module, and going through ``srv`` keeps every tool mockable from one place.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from typing import Any, AsyncIterator, Callable, Dict, Optional, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from dateutil import parser as date_parser
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import accounts, auth, calendar_actions, preferences
from .auth import AuthError
from .timeutil import (
    Interval,
    clip_intervals,
    combine,
    format_clock,
    free_windows,
    iter_days,
    merge_intervals,
    parse_clock,
    subtract_intervals,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

SERVER_NAME = "calendar-mcp"
SERVER_VERSION = "1.1.0"

#: Shared wording for the ``account`` argument every tool accepts, so the
#: description a client sees is identical everywhere.
ACCOUNT_ARG_DOC = "Account name from 'calendar-mcp accounts'; omit for the default."

INSTRUCTIONS = """\
Read and manage the user's Google Calendar.

Calendars are addressed by `calendar_id`. Use `primary` for the user's own main
calendar (the default everywhere); call `list_calendars` to discover the IDs of
secondary and shared calendars.

Times are ISO 8601 strings. Always include a UTC offset when you know the user's
timezone -- `2026-03-14T15:00:00-04:00` or `2026-03-14T19:00:00Z`. A timestamp
without an offset is interpreted in the target calendar's own timezone, which is
usually what the user means but is worth stating back to them. All-day events
use a plain `YYYY-MM-DD` date instead.

Workflow notes:
  * `find_events` first when the user refers to an event by description -- the
    write tools need the `id` it returns.
  * `quick_add_event` is the fastest path for a one-line natural-language event
    ("lunch with Dana Friday 1pm"); `create_event` when you need attendees,
    a description, a location or exact control of the times.
  * `move_event` reschedules and keeps the original duration when you supply
    only `new_start`; prefer it over `update_event` for "push this back an hour".
  * `respond_to_event` sets the user's own RSVP on an invitation.
  * `delete_event` is irreversible and may ask the user to confirm.
  * `query_free_busy` and `schedule_mutual` read other people's calendars by
    email address; they return busy intervals only, never event details.

Several Google accounts can be signed in at once. Every tool takes an optional
`account` name; omit it for the user's default. `list_accounts` shows which
accounts exist and whether each one is still authorized.

`get_preferences` reports the user's working hours, lunch break, meeting buffer
and focus-block settings. Read them before proposing times, rather than assuming
a 9-to-5; `set_preferences` records a correction the user gives you.
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CalendarToolError(ToolError):
    """A tool-level failure reported back to the MCP client as an error string."""


def _http_error_message(error: HttpError) -> str:
    """Renders a googleapiclient HttpError as one concise, actionable line."""
    status = getattr(getattr(error, "resp", None), "status", None) or "unknown"
    detail = ""
    try:
        payload = error.content.decode("utf-8") if error.content else ""
        parsed = json.loads(payload)
        detail = (parsed.get("error") or {}).get("message") or ""
        if not detail:
            errors = (parsed.get("error") or {}).get("errors") or []
            if errors:
                detail = errors[0].get("message", "")
    except Exception:
        detail = ""
    if not detail:
        detail = getattr(error, "reason", "") or str(error)

    hint = ""
    if status == 401:
        hint = " Run 'calendar-mcp auth' to sign in again."
    elif status == 403:
        hint = " The account may lack access to this calendar, or the API quota is exhausted."
    elif status == 404:
        hint = " Check the calendar_id and event_id."

    return f"Google Calendar API error {status}: {detail}{hint}"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class CredentialProvider:
    """Lazily loads, caches and refreshes Google credentials, per account.

    Nothing touches the network until :meth:`get` is first called, which happens
    on the first tool invocation rather than at server startup. Credentials are
    cached per resolved account name, so a session that talks to both ``work``
    and ``personal`` reads each token once.
    """

    def __init__(self) -> None:
        self._creds: Dict[str, Credentials] = {}
        self._lock = threading.Lock()

    def get(self, account: Optional[str] = None) -> Credentials:
        """Returns valid credentials for ``account``, reloading as needed.

        Args:
            account: Account name, or ``None`` for the resolved default.

        Raises:
            AuthError: When no usable token exists (the message says what to do).
        """
        name = accounts.resolve_account(account)
        with self._lock:
            cached = self._creds.get(name)
            if cached is not None and cached.valid:
                return cached
            token_path = accounts.token_path_for(name)
            try:
                # auth.get_credentials refreshes an expired token itself and will
                # not open a browser unless CALENDAR_MCP_ALLOW_BROWSER_AUTH is set.
                creds = auth.get_credentials(path=token_path)
            except AuthError as exc:
                raise AuthError(self._auth_hint(name, exc)) from exc
            # Tag the credentials with the account they belong to, so
            # account-agnostic helpers (the calendar-timezone cache) can tell
            # one account's 'primary' from another's without every tool having
            # to thread the account name through.
            try:
                creds._calendar_mcp_account = name
            except AttributeError:  # pragma: no cover - exotic Credentials subclass
                pass
            self._creds[name] = creds
            return creds

    @staticmethod
    def _auth_hint(name: str, exc: AuthError) -> str:
        """Adds the account name to an auth failure when it is not the default."""
        if name == accounts.DEFAULT_ACCOUNT:
            return str(exc)
        return (
            f"{exc} (account '{name}'; run 'calendar-mcp auth --account {name}'. "
            f"Known accounts: {accounts.describe_known_accounts()}.)"
        )

    def reset(self, account: Optional[str] = None) -> None:
        """Drops cached credentials so the next call reloads from disk.

        Args:
            account: Only forget this account. ``None`` forgets all of them.
        """
        with self._lock:
            if account is None:
                self._creds.clear()
            else:
                self._creds.pop(accounts.resolve_account(account), None)


credential_provider = CredentialProvider()

# Calendar timezones are stable; cache them so naive timestamps do not cost an
# extra API round trip on every call. The key is (account, calendar_id), not
# calendar_id alone: 'primary' names a different calendar -- in a different
# timezone -- for every signed-in account.
_timezone_cache: dict[tuple, Optional[str]] = {}
_timezone_lock = threading.Lock()


def _creds_account(creds: Optional[Credentials]) -> str:
    """The account name :class:`CredentialProvider` tagged these credentials with."""
    return getattr(creds, "_calendar_mcp_account", None) or accounts.DEFAULT_ACCOUNT


def _calendar_timezone(creds: Credentials, calendar_id: str) -> Optional[str]:
    """Returns the IANA timezone of ``calendar_id``, cached per account."""
    key = (_creds_account(creds), calendar_id)
    with _timezone_lock:
        if key in _timezone_cache:
            return _timezone_cache[key]
    tz = calendar_actions.get_calendar_timezone(creds, calendar_id)
    with _timezone_lock:
        _timezone_cache[key] = tz
    return tz


def _forget_calendar_timezone(calendar_id: Optional[str]) -> None:
    """Drops a cached calendar timezone (e.g. after creating that calendar).

    Every account's entry for ``calendar_id`` goes, which is the conservative
    choice: re-reading a timezone costs one API call, serving a stale one costs
    a wrongly-placed event.
    """
    if not calendar_id:
        return
    with _timezone_lock:
        for key in [k for k in _timezone_cache if k[1] == calendar_id]:
            _timezone_cache.pop(key, None)


def _default_tzinfo(creds: Optional[Credentials], calendar_id: Optional[str]):
    """The tzinfo naive timestamps are interpreted in: the calendar's, else local."""
    if creds is not None and calendar_id:
        name = _calendar_timezone(creds, calendar_id)
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning("Calendar '%s' reports unknown timezone '%s'.", calendar_id, name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_datetime(
    value: Optional[str],
    field: str,
    creds: Optional[Credentials] = None,
    calendar_id: Optional[str] = None,
) -> Optional[datetime]:
    """Parses an ISO 8601 string into an aware datetime.

    A value without a UTC offset is interpreted in ``calendar_id``'s timezone
    when credentials are available, and otherwise in the server's local zone.

    Raises:
        CalendarToolError: If the string is not a usable ISO 8601 timestamp.
    """
    if value is None or value == "":
        return None
    try:
        parsed = date_parser.isoparse(value)
    except (ValueError, TypeError):
        try:
            parsed = date_parser.parse(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise CalendarToolError(
                f"{field} is not a valid ISO 8601 timestamp: {value!r}. "
                "Use e.g. '2026-03-14T15:00:00-04:00' or '2026-03-14T19:00:00Z'."
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_default_tzinfo(creds, calendar_id))
    return parsed


def _require(value: Optional[datetime], field: str) -> datetime:
    if value is None:
        raise CalendarToolError(f"{field} is required.")
    return value


def _parse_clock(value: Optional[str], field: str) -> Optional[dt_time]:
    """Parses an 'HH:MM' working-hours bound, as a tool-level error on failure."""
    try:
        return parse_clock(value, field)
    except ValueError as exc:
        raise CalendarToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Execution helper
# ---------------------------------------------------------------------------


async def _run(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Runs blocking Google API work off the event loop and normalises errors."""
    call = functools.partial(func, *args, **kwargs)
    try:
        return await anyio.to_thread.run_sync(call)
    except CalendarToolError:
        raise
    except HttpError as exc:
        raise CalendarToolError(_http_error_message(exc)) from exc
    except AuthError as exc:
        raise CalendarToolError(str(exc)) from exc
    except ValueError as exc:
        raise CalendarToolError(str(exc)) from exc


def _provider(ctx: Optional[Context] = None) -> CredentialProvider:
    """The CredentialProvider for this request (lifespan-scoped, else global)."""
    if ctx is not None:
        try:
            state = ctx.request_context.lifespan_context
        except (ValueError, AttributeError):
            state = None
        if isinstance(state, AppContext):
            return state.credentials
    return credential_provider


async def _warn(ctx: Optional[Context], message: str) -> None:
    """Best-effort warning to the client; always recorded in the server log."""
    logger.warning(message)
    if ctx is None:
        return
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await ctx.warning(message)
    except Exception:  # client without logging capability, or no live session
        pass


def _no_result(action: str) -> CalendarToolError:
    return CalendarToolError(
        f"{action} did not return a usable result. The server log has the details."
    )


# ---------------------------------------------------------------------------
# Elicitation helpers
# ---------------------------------------------------------------------------


class DeleteConfirmation(BaseModel):
    """Elicitation schema for a destructive tool's confirmation prompt."""

    confirm: bool = Field(
        default=False,
        description="Confirm permanently deleting this event.",
    )


async def _confirm_delete(ctx: Optional[Context], label: str) -> Optional[bool]:
    """Asks the client to confirm a deletion.

    Returns True/False when the client answered, and None when the client does
    not support elicitation (in which case the caller proceeds unprompted).
    """
    if ctx is None:
        return None
    try:
        result = await ctx.elicit(
            f"Permanently delete {label}? This cannot be undone.",
            DeleteConfirmation,
        )
    except Exception as exc:  # client without elicitation support
        logger.info("Skipping delete confirmation (client cannot elicit): %s", exc)
        return None

    action = getattr(result, "action", None)
    if action == "accept":
        data = getattr(result, "data", None)
        return bool(getattr(data, "confirm", False))
    if action in ("decline", "cancel"):
        return False
    return None


# ---------------------------------------------------------------------------
# Server + lifespan
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    """Lifespan state shared by every request."""

    credentials: CredentialProvider


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    """Prepares shared state. Deliberately does no network or disk I/O."""
    logger.info("calendar-mcp %s starting (credentials load on first tool call)", SERVER_VERSION)
    try:
        yield AppContext(credentials=credential_provider)
    finally:
        logger.info("calendar-mcp shutting down")


server = MCPServer(
    name=SERVER_NAME,
    title="Google Calendar",
    version=SERVER_VERSION,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)


READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

#: A local-only write (preferences on disk): not read-only, but it opens no
#: world and re-applying it is harmless.
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
#: A local-only read (accounts, preferences): no Google call at all.
LOCAL_READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_stdio() -> None:
    """Serves MCP over stdio (the default transport)."""
    logger.info("Serving calendar-mcp over stdio")
    server.run(transport="stdio")


def run_http(host: str = "127.0.0.1", port: int = 8000, path: str = "/mcp") -> None:
    """Serves MCP over streamable HTTP."""
    logger.info("Serving calendar-mcp over streamable-http at http://%s:%s%s", host, port, path)
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
    )


def http_app(path: str = "/mcp", host: str = "127.0.0.1"):
    """Returns the Starlette app, for mounting behind an existing ASGI server."""
    return server.streamable_http_app(streamable_http_path=path, host=host)


# ---------------------------------------------------------------------------
# Tool registration
#
# Importing the package runs every tool module, each of which decorates its
# functions with ``@server.tool(...)``. This is the last statement in the file
# on purpose: by the time the tool modules do ``from calendar_mcp import server
# as srv``, everything above already exists. Python's module cache guarantees
# each tool is registered exactly once, however the package is first imported.
# ---------------------------------------------------------------------------

from calendar_mcp import tools as _tools  # noqa: E402  (registers the tools)


def __getattr__(name: str):
    """Resolves ``server.<tool>`` to the tool function in ``calendar_mcp.tools``.

    Every tool stays reachable from this module -- ``server.find_events`` still
    works, as do the tests that call the tool functions directly -- without
    listing them here. The lookup is lazy on purpose: importing
    ``calendar_mcp.tools`` first would otherwise hit this module mid-import and
    fail on a half-built tools module. Resolved names are cached in globals, so
    the scan happens at most once per tool.

    Only functions defined inside ``calendar_mcp.tools`` are resolvable, so a
    typo cannot silently hand back some helper a tool module imported.
    """
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    package = importlib.import_module("calendar_mcp.tools")
    for submodule in getattr(package, "__all__", ()):
        module = importlib.import_module(f"calendar_mcp.tools.{submodule}")
        value = getattr(module, name, None)
        if callable(value) and getattr(value, "__module__", "").startswith("calendar_mcp.tools"):
            globals()[name] = value
            return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # server plumbing
    "server",
    "AppContext",
    "CalendarToolError",
    "CredentialProvider",
    "credential_provider",
    "lifespan",
    "run_stdio",
    "run_http",
    "http_app",
    "INSTRUCTIONS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "ACCOUNT_ARG_DOC",
    # annotations
    "READ_ONLY",
    "WRITE",
    "UPDATE",
    "DESTRUCTIVE",
    "LOCAL_READ",
    "LOCAL_WRITE",
    # time helpers (credential-aware here, pure ones in calendar_mcp.timeutil)
    "parse_datetime",
    "Interval",
    "clip_intervals",
    "combine",
    "format_clock",
    "free_windows",
    "iter_days",
    "merge_intervals",
    "parse_clock",
    "subtract_intervals",
    # modules the tools reach Google and config through
    "accounts",
    "auth",
    "calendar_actions",
    "preferences",
    # tools
    "add_attendee",
    "analyze_busyness",
    "block_focus_time",
    "check_attendee_status",
    "create_calendar",
    "create_event",
    "delete_event",
    "detect_conflicts",
    "find_events",
    "find_focus_time",
    "get_preferences",
    "list_accounts",
    "list_calendars",
    "move_event",
    "project_recurring_events",
    "query_free_busy",
    "quick_add_event",
    "respond_to_event",
    "schedule_mutual",
    "set_preferences",
    "suggest_reschedule",
    "time_audit",
    "update_event",
]
