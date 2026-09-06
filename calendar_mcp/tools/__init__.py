"""Every MCP tool calendar-mcp exposes, one module per subject area.

Importing this package registers all of them against the ``MCPServer``
instance in :mod:`calendar_mcp.server`; :mod:`calendar_mcp.server` imports it
as its very last statement, so ``import calendar_mcp.server`` is all any entry
point needs.

Layout:

===========================  ================================================
``calendar_mcp.tools``       tools
===========================  ================================================
:mod:`~calendar_mcp.tools.accounts`     ``list_accounts``, ``get_preferences``,
                                        ``set_preferences``
:mod:`~calendar_mcp.tools.analysis`     ``analyze_busyness``,
                                        ``project_recurring_events``
:mod:`~calendar_mcp.tools.calendars`    ``list_calendars``, ``create_calendar``
:mod:`~calendar_mcp.tools.conflicts`    ``detect_conflicts``, ``suggest_reschedule``
:mod:`~calendar_mcp.tools.events`       ``find_events``, ``check_attendee_status``,
                                        ``create_event``, ``quick_add_event``,
                                        ``update_event``, ``move_event``,
                                        ``add_attendee``, ``respond_to_event``,
                                        ``delete_event``
:mod:`~calendar_mcp.tools.focus`        ``find_focus_time``, ``block_focus_time``
:mod:`~calendar_mcp.tools.scheduling`   ``query_free_busy``, ``schedule_mutual``
===========================  ================================================

**Adding a tool.** Put it in whichever module fits (or add a new one and list
it in the import below -- that is the only edit outside your own file). The
pattern every tool follows:

.. code-block:: python

    from typing import Optional

    from calendar_mcp import server as srv


    @srv.server.tool(name="my_tool", title="My tool", annotations=srv.READ_ONLY)
    async def my_tool(
        calendar_id: str = "primary",
        account: Optional[str] = None,
        ctx: Optional[srv.Context] = None,
    ) -> SomeResultModel:
        \"\"\"What the tool does, as the model will read it.

        Args:
            calendar_id: Calendar to read.
            account: Account name from 'calendar-mcp accounts'; omit for the default.
        \"\"\"
        provider = srv._provider(ctx)

        def work() -> SomeResultModel:
            creds = provider.get(account)
            response = srv.calendar_actions.something(creds, ...)
            if response is None:
                raise srv._no_result("Doing the thing")
            return SomeResultModel(...)

        return await srv._run(work)

Three rules keep the suite green and the tools consistent:

1. Blocking Google work goes inside a ``work()`` closure handed to
   ``srv._run``, which moves it off the event loop and turns ``HttpError`` /
   ``AuthError`` / ``ValueError`` into :class:`~calendar_mcp.server.CalendarToolError`.
2. Reach Google through ``srv.calendar_actions``, never by importing
   ``calendar_actions`` directly -- the tests patch that attribute.
3. Give the tool a trailing ``account: Optional[str] = None`` and pass it to
   ``provider.get(account)``, then document it with
   :data:`~calendar_mcp.server.ACCOUNT_ARG_DOC`'s wording.
"""

from calendar_mcp.tools import (  # noqa: F401
    accounts,
    analysis,
    audit,
    calendars,
    conflicts,
    events,
    focus,
    scheduling,
)

__all__ = [
    "accounts",
    "analysis",
    "audit",
    "calendars",
    "conflicts",
    "events",
    "focus",
    "scheduling",
]
